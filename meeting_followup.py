#!/usr/bin/env python3
"""
Automatic Meeting Follow-Up Email Drafter
==========================================
Triggered by LaunchAgent when Granola cache updates.

Architecture:
- Cache file:    Detect meetings + get meeting ID & attendees
- Granola API:   Fetch AI-generated panels (notes/summary) via local auth token
- Claude API:    Generate follow-up email draft
- Gmail API:     Push draft to Gmail

v2 improvements:
- Processes ALL unprocessed recent external meetings (not just the latest)
- Refreshes expired Granola tokens automatically
- Lock file prevents duplicate runs from concurrent triggers
- Deferred retry queue for meetings whose panels aren't ready yet
- Uses Claude Sonnet for higher-quality email output
- macOS notifications on success and failure
"""

import json
import os
import sys
import time
import gzip
import fcntl
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
GRANOLA_CACHE = Path.home() / "Library" / "Application Support" / "Granola" / "cache-v3.json"
GRANOLA_AUTH = Path.home() / "Library" / "Application Support" / "Granola" / "supabase.json"
GRANOLA_PANELS_URL = "https://api.granola.ai/v1/get-document-panels"

STATE_FILE = Path.home() / ".meeting-followup" / "state.json"
LOG_FILE = Path.home() / ".meeting-followup" / "followup.log"
LOCK_FILE = Path.home() / ".meeting-followup" / "run.lock"
GMAIL_CREDENTIALS = Path.home() / ".gmail-mcp" / "credentials.json"
GMAIL_TOKEN = Path.home() / ".gmail-mcp" / "token.json"

INTERNAL_DOMAIN = "rokt.com"
MY_EMAIL = "matthew.divincenzo@rokt.com"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Retry settings for panel fetching
PANEL_POLL_INTERVAL = 30   # seconds between retries
PANEL_POLL_MAX_WAIT = 300  # max seconds to wait (5 minutes)
PANEL_MIN_CHARS = 50       # minimum content length to consider panels "ready"

# Meeting age window
MEETING_MAX_AGE_HOURS = 3  # process meetings up to this old

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# macOS NOTIFICATIONS
# ---------------------------------------------------------------------------
def notify(title, message, sound="default"):
    """Send a macOS notification via osascript."""
    try:
        # Escape double quotes in message and title
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"')
        script = (
            f'display notification "{safe_msg}" with title "{safe_title}"'
            + (f' sound name "{sound}"' if sound else "")
        )
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=5,
        )
    except Exception as e:
        log.debug("Notification failed: %s", e)


# ---------------------------------------------------------------------------
# LOCK FILE (prevents concurrent runs)
# ---------------------------------------------------------------------------
class LockFile:
    """File-based lock using flock to prevent concurrent script execution."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = None

    def acquire(self):
        self._fd = open(self.path, "w")
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(str(os.getpid()))
            self._fd.flush()
            return True
        except OSError:
            self._fd.close()
            self._fd = None
            return False

    def release(self):
        if self._fd:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                self._fd.close()
            except Exception:
                pass
            self._fd = None


# ---------------------------------------------------------------------------
# STATE MANAGEMENT
# ---------------------------------------------------------------------------
def load_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return _default_state()
    return _default_state()


def _default_state():
    return {
        "processed_meeting_ids": [],
        "deferred_meeting_ids": [],
    }


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def already_processed(meeting_id):
    state = load_state()
    return meeting_id in state["processed_meeting_ids"]


def mark_processed(meeting_id):
    state = load_state()
    if meeting_id not in state["processed_meeting_ids"]:
        state["processed_meeting_ids"].append(meeting_id)
    state["processed_meeting_ids"] = state["processed_meeting_ids"][-200:]
    # Remove from deferred if present
    if meeting_id in state.get("deferred_meeting_ids", []):
        state["deferred_meeting_ids"].remove(meeting_id)
    state["last_run"] = datetime.now().isoformat()
    save_state(state)


def defer_meeting(meeting_id):
    """Add meeting to deferred queue for retry on next trigger."""
    state = load_state()
    if "deferred_meeting_ids" not in state:
        state["deferred_meeting_ids"] = []
    if meeting_id not in state["deferred_meeting_ids"]:
        state["deferred_meeting_ids"].append(meeting_id)
        state["deferred_meeting_ids"] = state["deferred_meeting_ids"][-20:]
    save_state(state)


def get_deferred_meetings():
    state = load_state()
    return state.get("deferred_meeting_ids", [])


# ---------------------------------------------------------------------------
# GRANOLA AUTH: Read + refresh local token
# ---------------------------------------------------------------------------
def get_granola_token():
    """Read the Granola access token from the local supabase.json file."""
    if not GRANOLA_AUTH.exists():
        log.error("Granola auth file not found at %s", GRANOLA_AUTH)
        return None
    try:
        data = json.loads(GRANOLA_AUTH.read_text())
        tokens = json.loads(data.get("workos_tokens", "{}"))
        token = tokens.get("access_token")
        if not token:
            log.error("No access_token in Granola auth file")
            return None
        return token
    except (json.JSONDecodeError, KeyError) as e:
        log.error("Failed to read Granola auth: %s", e)
        return None


def refresh_granola_token():
    """Attempt to refresh the Granola token using the stored refresh_token.

    Granola uses WorkOS under the hood. If direct refresh fails, we notify
    the user to open Granola (which triggers a token refresh automatically).
    """
    if not GRANOLA_AUTH.exists():
        return None
    try:
        data = json.loads(GRANOLA_AUTH.read_text())
        tokens = json.loads(data.get("workos_tokens", "{}"))
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            log.warning("No refresh_token available")
            return None

        # Attempt WorkOS-style token refresh
        req = Request(
            "https://api.workos.com/user_management/authenticate",
            data=json.dumps({
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": tokens.get("client_id", ""),
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = urlopen(req, timeout=15)
        raw = resp.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        new_tokens = json.loads(raw)

        if "access_token" in new_tokens:
            tokens["access_token"] = new_tokens["access_token"]
            if "refresh_token" in new_tokens:
                tokens["refresh_token"] = new_tokens["refresh_token"]
            data["workos_tokens"] = json.dumps(tokens)
            GRANOLA_AUTH.write_text(json.dumps(data))
            log.info("Granola token refreshed successfully")
            return new_tokens["access_token"]

    except Exception as e:
        log.warning("Token refresh failed: %s", e)

    return None


def get_valid_granola_token():
    """Get a valid Granola token, refreshing if needed."""
    token = get_granola_token()
    if not token:
        return None

    # Test the token with a lightweight request
    try:
        req = Request(
            GRANOLA_PANELS_URL,
            data=json.dumps({"document_id": "00000000-0000-0000-0000-000000000000"}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        urlopen(req, timeout=10)
        return token
    except HTTPError as e:
        if e.code == 401:
            log.warning("Granola token expired (401). Attempting refresh...")
            new_token = refresh_granola_token()
            if new_token:
                return new_token
            notify(
                "Meeting Follow-Up",
                "Granola token expired. Open Granola to re-authenticate.",
                sound="Basso",
            )
            log.error("Granola token expired and refresh failed. Open Granola to re-authenticate.")
            return None
        else:
            # Non-auth error (404, 400, etc.) means token is fine
            return token
    except Exception:
        # Network error — assume token is ok, let actual request handle it
        return token


# ---------------------------------------------------------------------------
# GRANOLA API: Fetch panels
# ---------------------------------------------------------------------------
def fetch_panels(meeting_id, token):
    """Fetch AI-generated panels from Granola API for a given meeting ID."""
    try:
        req = Request(
            GRANOLA_PANELS_URL,
            data=json.dumps({"document_id": meeting_id}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",
            },
        )
        resp = urlopen(req, timeout=30)
        raw = resp.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        panels = json.loads(raw)
        return panels
    except HTTPError as e:
        log.error("Granola API HTTP error %d: %s", e.code, e.reason)
        return None
    except URLError as e:
        log.error("Granola API connection error: %s", e.reason)
        return None
    except Exception as e:
        log.error("Granola API unexpected error: %s", e)
        return None


def extract_panel_text(content):
    """Recursively extract text from ProseMirror JSON content blocks."""
    if not content or not isinstance(content, dict):
        return ""
    parts = []
    for block in content.get("content", []):
        block_type = block.get("type", "")

        if block_type == "heading":
            level = block.get("attrs", {}).get("level", 3)
            heading_text = _inline_text(block)
            if heading_text.strip():
                prefix = "#" * level + " "
                parts.append(prefix + heading_text.strip() + "\n")

        elif block_type == "bulletList":
            for li in block.get("content", []):
                li_text = _inline_text(li)
                if li_text.strip():
                    parts.append("- " + li_text.strip() + "\n")

        elif block_type == "orderedList":
            for i, li in enumerate(block.get("content", []), 1):
                li_text = _inline_text(li)
                if li_text.strip():
                    parts.append(f"{i}. " + li_text.strip() + "\n")

        elif block_type == "paragraph":
            para_text = _inline_text(block)
            if para_text.strip():
                parts.append(para_text.strip() + "\n")

        elif "content" in block:
            nested = extract_panel_text(block)
            if nested.strip():
                parts.append(nested + "\n")

    return "\n".join(parts).strip()


def _inline_text(block):
    """Extract inline text from a block's content array."""
    if not block or not isinstance(block, dict):
        return ""
    parts = []
    for item in block.get("content", []):
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
        elif "content" in item:
            parts.append(_inline_text(item))
    return "".join(parts)


def panels_to_notes(panels):
    """Convert a list of Granola panels into readable notes text."""
    all_notes = []
    for panel in panels:
        title = panel.get("title") or "Notes"
        content = panel.get("content", {})
        text = extract_panel_text(content)
        if text.strip():
            all_notes.append(f"{title}:\n{text}")
    return "\n\n".join(all_notes)


def fetch_panels_with_retry(meeting_id, token):
    """Poll Granola API until panels are ready or timeout is reached."""
    elapsed = 0
    while elapsed < PANEL_POLL_MAX_WAIT:
        panels = fetch_panels(meeting_id, token)
        if panels:
            notes_text = panels_to_notes(panels)
            if len(notes_text.strip()) >= PANEL_MIN_CHARS:
                log.info("Panels ready: %d chars after %ds", len(notes_text), elapsed)
                return notes_text
            else:
                log.info("Panels exist but too short (%d chars) — waiting...",
                         len(notes_text.strip()))
        else:
            log.info("No panels yet — waiting...")

        time.sleep(PANEL_POLL_INTERVAL)
        elapsed += PANEL_POLL_INTERVAL

    # One final attempt
    panels = fetch_panels(meeting_id, token)
    if panels:
        notes_text = panels_to_notes(panels)
        if len(notes_text.strip()) >= PANEL_MIN_CHARS:
            log.info("Panels ready on final attempt: %d chars", len(notes_text))
            return notes_text

    log.warning("Panels not ready after %ds", PANEL_POLL_MAX_WAIT)
    return None


# ---------------------------------------------------------------------------
# CACHE: Detect meetings + get metadata
# ---------------------------------------------------------------------------
def parse_cache():
    if not GRANOLA_CACHE.exists():
        log.error("Granola cache not found at %s", GRANOLA_CACHE)
        return None
    try:
        raw = json.loads(GRANOLA_CACHE.read_text())
        inner = json.loads(raw["cache"])
        return inner["state"]
    except (json.JSONDecodeError, KeyError) as e:
        log.error("Failed to parse Granola cache: %s", e)
        return None


def get_meeting_date(doc):
    """Extract the meeting start datetime, falling back to created_at."""
    try:
        gcal = doc.get("google_calendar_event") or {}
        start = gcal.get("start", {}).get("dateTime", "")
        if start:
            return datetime.fromisoformat(start)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.fromisoformat((doc.get("created_at") or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def get_recent_meetings(cache_state):
    """Return ALL recent unprocessed meetings within the age window."""
    documents = cache_state.get("documents", {})
    if not documents:
        log.warning("No documents in cache")
        return []

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MEETING_MAX_AGE_HOURS)
    recent = []

    for doc in documents.values():
        if doc.get("deleted_at"):
            continue
        meeting_time = get_meeting_date(doc)
        if meeting_time.tzinfo is None:
            meeting_time = meeting_time.replace(tzinfo=timezone.utc)
        if meeting_time < cutoff:
            continue
        # Only include if meeting time is in the past (meeting has ended)
        if meeting_time > now:
            continue
        doc_id = doc.get("id", "")
        if doc_id and already_processed(doc_id):
            continue
        recent.append(doc)

    recent.sort(key=get_meeting_date, reverse=True)
    log.info("Found %d recent unprocessed meetings within %d-hour window",
             len(recent), MEETING_MAX_AGE_HOURS)
    return recent


def get_deferred_meeting_docs(cache_state):
    """Retrieve cache docs for any deferred meetings that still need processing."""
    deferred_ids = get_deferred_meetings()
    if not deferred_ids:
        return []

    documents = cache_state.get("documents", {})
    docs = []
    for mid in deferred_ids:
        if already_processed(mid):
            continue
        # Search by id field
        for d in documents.values():
            if d.get("id") == mid:
                docs.append(d)
                break

    log.info("Found %d deferred meetings to retry", len(docs))
    return docs


# ---------------------------------------------------------------------------
# MEETING DATA EXTRACTION
# ---------------------------------------------------------------------------
def extract_meeting_metadata(doc):
    """Extract meeting ID, title, date, and attendees from cache document."""
    gcal = doc.get("google_calendar_event") or {}
    start = gcal.get("start", {}).get("dateTime", doc.get("created_at", ""))

    attendees = []
    for att in gcal.get("attendees", []):
        email = att.get("email", "")
        if email.startswith("c_") and "@resource.calendar.google.com" in email:
            continue
        attendees.append({
            "name": att.get("displayName", email.split("@")[0]),
            "email": email,
            "self": att.get("self", False),
        })

    return {
        "id": doc.get("id", ""),
        "title": doc.get("title") or gcal.get("summary", "Untitled Meeting"),
        "date": start,
        "attendees": attendees,
    }


# ---------------------------------------------------------------------------
# CLASSIFICATION
# ---------------------------------------------------------------------------
def is_external_meeting(attendees):
    for att in attendees:
        email = att.get("email", "").lower().strip()
        if email and not email.endswith("@" + INTERNAL_DOMAIN):
            return True
    return False


def get_recipients(attendees):
    to_list = []
    cc_list = []
    for att in attendees:
        email = att.get("email", "").lower().strip()
        if not email:
            continue
        if email == MY_EMAIL.lower():
            continue
        if email.endswith("@" + INTERNAL_DOMAIN):
            cc_list.append(email)
        else:
            to_list.append(email)
    return {"to": to_list, "cc": cc_list}


# ---------------------------------------------------------------------------
# CLAUDE API
# ---------------------------------------------------------------------------
def generate_followup_email(meeting_data, recipients, sender_name="Matthew"):
    try:
        import anthropic
    except ImportError:
        log.error("anthropic not installed. Run: pip3 install anthropic --break-system-packages")
        return None

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set")
        return None

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    try:
        dt = datetime.fromisoformat(meeting_data["date"])
        date_str = dt.strftime("%B %d, %Y")
    except (ValueError, TypeError):
        date_str = meeting_data["date"]

    prompt = """You are {sender_name}, a senior sales leader at Rokt. You just got off a call and are writing a follow-up email.

MEETING DETAILS:
- Title: {title}
- Date: {date}
- To (external): {to}
- CC (internal Rokt): {cc}

MEETING CONTENT:
{notes}

INSTRUCTIONS:

Using the full meeting transcript and notes, draft a follow-up email. The email should read like something a sharp, senior seller would actually send. It should lock in commitments, demonstrate you listened, and move the deal or relationship forward.

OVERALL LENGTH: The entire email body should be 4-8 sentences. That's it. If you've written more than 8 sentences, you've written too much. Cut aggressively. A great follow-up is a tight note, not a memo.

Subject line: Always use "re: our call today (Rokt)" as the subject line. No exceptions.

Opening: Lead with what matters to them.
Always begin the email with "Hi [first name]," on its own line, followed by a blank line. The next line should begin with "Great speaking earlier" then transition into reflecting back the most important problem, goal, or priority they articulated during the call. Use labeling where it fits naturally. Phrases like "it sounds like," "it seems like," or "the sense I got" signal that you were listening and invite them to confirm or correct. This builds trust and pulls them into the email.
Adapt the tone to the relationship. For a first or early-stage conversation, the opening should demonstrate that you understood their situation. For an ongoing client relationship, skip the "prove I was listening" framing and get to the point. Not every email needs to open the same way.
Do not add any additional greeting or thank-you beyond "Hi [first name]," and "Great speaking earlier." No "thanks for your time" or "great chatting today."

Recap: What we aligned on.
ONE sentence max. Reference the decision or direction, do not re-explain it. Both parties were on the call. If you discussed a deal structure, say "glad we landed on the draw-against-commission approach" not a full paragraph re-teaching how it works. The recap should feel like a nod, not a briefing document. Only include things that were actually discussed and agreed upon. Do not invent commitments, deliverables, or workstreams that were not explicitly mentioned in the transcript.

Commitments: Who is doing what.
Pick only the 1-2 most important next steps. Skip anything both parties will obviously remember from the conversation. A follow-up email is not a meeting transcript or CRM log. If there were five action items discussed, include the one or two that actually need to be written down to move things forward, and drop the rest. Assign every action to the correct person based on what was actually said in the transcript. This is critical: if the prospect said "send me some times," the action belongs to Rokt, not the prospect. Misattributing an action item destroys credibility. Read the transcript carefully and get this right. If no clear commitments were made by either side, propose a reasonable next action rather than fabricating one.
Write commitments inline in prose. No headers like "From our side" / "From your side." No bullet points for action items. Weave them into the body naturally.

Value add (when appropriate): One thing they didn't ask for.
If the conversation surfaced a challenge, question, or priority where a relevant insight, resource, case study, or data point would genuinely help, include it. This should deepen their thinking or give your champion ammunition to sell internally, not be a product pitch.
CRITICAL: You may only reference real, specific resources, articles, case studies, or data points that were explicitly mentioned during the call or that you know with certainty to be real. Never fabricate a case study, statistic, or anecdote. If you cannot cite something specific and real, skip this section entirely.
This is not mandatory for every email. Skip it on routine syncs, quick check-ins, or calls where the next steps are purely operational. Forcing a value-add into an email that doesn't need one makes the email feel bloated and performative. Default to skipping it. Only include it when you have something genuinely useful and real to offer.

Close: Make it easy to move forward.
End with a concrete next step. Default to calibrated, open-ended questions that give the recipient ownership over the answer. "What does your schedule look like early next week?" pulls a better response than "Does Tuesday at 2pm work?" because it invites collaboration instead of demanding a yes or no.
The closing call to action MUST always be on its own line, separated by a line break from the preceding text. Never append it to the end of another sentence or paragraph.
Avoid vague closers like "let me know your thoughts," "happy to chat further," or "looking forward to your availability."

Tone and style rules:
Write like a human being talking to another human being. The email should sound like it was written by the person who was actually on the call, not generated by software.
Conversational and confident. Peer to peer, not vendor to buyer.
Use their name, their company name, and the names of people referenced on the call.
Keep it concise. If a sentence doesn't move the conversation forward, cut it.
Use labeling ("it sounds like," "it seems like") naturally where it reinforces a key point. Do not overuse it. One label per email is usually enough.
Never use emdashes. Use commas, periods, or restructure the sentence instead.
Never use AI-style contrasting syntax like "it's not X, it's Y" or "this isn't a summary, it's a commitment device." Just say what it is. Don't define things by what they're not.
Never use pandering or performative language like "honestly, I was genuinely impressed" or "I have to say, that was a really great point." No flattery that sounds like it came from a chatbot. If something from the call was noteworthy, reference it with specificity, not adjectives.
Avoid jargon, filler, and anything that sounds templated.

Formatting rules:
Minimize bullet points. Default to prose. Use bullets only when listing 3+ concrete action items or deliverables, never for recapping discussion topics.
Minimize line breaks and white space. The email should feel like a tight, well-written note, not a document with sections and headers.
No section headers in the email itself (no "Key Discussion Points:" or "Action Items:" labels). These make the email feel like a template, not a message.
No bolding within the email body unless absolutely necessary for a date or deadline.
The entire email should be scannable in under 60 seconds. If it feels long, it is long. Cut it.

Accuracy rules:
Only reference things that were actually said in the transcript. Do not infer, assume, or embellish.
Attribute action items to the correct person. Re-read the relevant portion of the transcript before assigning ownership.
Do not invent deliverables, frameworks, or workstreams that were not discussed. If the agreed next step is simply "have another call," that's the next step. Don't manufacture additional commitments to make the email look more substantive.
If the transcript is vague or ambiguous about who owns a next step, default to Rokt owning it. It is always better to offer to do something than to incorrectly tell the prospect they volunteered for it.

Sign off with "Best," then "{sender_name}".

Respond with ONLY a JSON object (no markdown, no backticks). Use \\n for line breaks in the body:
{{"subject": "...", "body": "Hi [name],\\n\\nGreat speaking earlier...\\n\\nBest,\\n{sender_name}"}}""".format(
        title=meeting_data["title"],
        date=date_str,
        to=", ".join(recipients["to"]),
        cc=", ".join(recipients["cc"]),
        notes=meeting_data["notes"][:20000],
        sender_name=sender_name,
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        result = json.loads(text)
        log.info("Email generated: %s", result["subject"])
        return result
    except Exception as e:
        log.error("Claude API error: %s", e)
        return None


# ---------------------------------------------------------------------------
# GMAIL API
# ---------------------------------------------------------------------------
def get_gmail_sender_name():
    """Get the user's display name from Gmail sendAs settings."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        return None

    creds = None
    if GMAIL_TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN))

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                GMAIL_TOKEN.write_text(creds.to_json())
            except Exception:
                return None
        else:
            return None

    try:
        service = build("gmail", "v1", credentials=creds)
        send_as = service.users().settings().sendAs().list(userId="me").execute()
        for alias in send_as.get("sendAs", []):
            if alias.get("isPrimary"):
                full_name = alias.get("displayName", "")
                if full_name:
                    return full_name.split()[0]  # First name only
        return None
    except Exception as e:
        log.warning("Could not get sender name from Gmail: %s", e)
        return None


def create_gmail_draft(subject, body, to, cc):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        import base64
        from email.mime.text import MIMEText
    except ImportError:
        log.error("Google API packages not installed.")
        return False

    creds = None
    if GMAIL_TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN))

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                GMAIL_TOKEN.write_text(creds.to_json())
            except Exception as e:
                log.error("Failed to refresh Gmail token: %s", e)
                return False
        else:
            log.error("No valid Gmail token.")
            return False

    try:
        service = build("gmail", "v1", credentials=creds)
        message = MIMEText(body)
        message["to"] = ", ".join(to)
        if cc:
            message["cc"] = ", ".join(cc)
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}},
        ).execute()
        log.info("Draft created: ID %s", draft["id"])
        return True
    except Exception as e:
        log.error("Gmail API error: %s", e)
        return False


# ---------------------------------------------------------------------------
# PROCESS A SINGLE MEETING
# ---------------------------------------------------------------------------
def process_meeting(doc, token):
    """Process one meeting: fetch notes, generate email, create draft.

    Returns: 'success', 'deferred', 'skipped', or 'failed'
    """
    meeting = extract_meeting_metadata(doc)
    log.info("Processing: %s | Date: %s | Attendees: %d",
             meeting["title"], meeting["date"], len(meeting["attendees"]))

    # Check external
    if not is_external_meeting(meeting["attendees"]):
        log.info("  Internal meeting — skipping")
        if meeting["id"]:
            mark_processed(meeting["id"])
        return "skipped"

    recipients = get_recipients(meeting["attendees"])
    if not recipients["to"]:
        log.warning("  No external email addresses — skipping")
        if meeting["id"]:
            mark_processed(meeting["id"])
        return "skipped"

    log.info("  External meeting. To: %s, CC: %s", recipients["to"], recipients["cc"])

    # Fetch panels from Granola API (with retry)
    log.info("  Fetching panels from Granola API...")
    notes_text = fetch_panels_with_retry(meeting["id"], token)

    if not notes_text:
        log.warning("  Panels not ready — deferring for next trigger")
        defer_meeting(meeting["id"])
        notify(
            "Meeting Follow-Up",
            f"Notes not ready yet for: {meeting['title']}. Will retry.",
            sound="Purr",
        )
        return "deferred"

    meeting["notes"] = notes_text
    log.info("  Notes: %d chars", len(notes_text))

    # Generate email
    sender_name = get_gmail_sender_name() or "Matthew"
    email = generate_followup_email(meeting, recipients, sender_name=sender_name)
    if not email:
        log.error("  Failed to generate email")
        notify(
            "Meeting Follow-Up Failed",
            f"Could not generate email for: {meeting['title']}",
            sound="Basso",
        )
        return "failed"

    # Create Gmail draft
    success = create_gmail_draft(
        subject=email["subject"],
        body=email["body"],
        to=recipients["to"],
        cc=recipients["cc"],
    )

    if success:
        log.info("  Draft created in Gmail")
        mark_processed(meeting["id"])
        notify(
            "Meeting Follow-Up",
            f"Draft ready: {meeting['title']}",
            sound="Glass",
        )
        return "success"
    else:
        log.error("  Failed to create Gmail draft")
        notify(
            "Meeting Follow-Up Failed",
            f"Gmail draft failed for: {meeting['title']}",
            sound="Basso",
        )
        return "failed"


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 50)
    log.info("Meeting follow-up triggered")

    # --- Acquire lock (prevent concurrent runs) ---
    lock = LockFile(LOCK_FILE)
    if not lock.acquire():
        log.info("Another instance is running — exiting")
        return
    try:
        _run()
    finally:
        lock.release()


def _run():
    # Short initial delay to let Granola finish writing cache metadata
    time.sleep(10)

    # --- Step 1: Parse cache ---
    cache_state = parse_cache()
    if not cache_state:
        log.info("Could not parse cache — exiting")
        return

    # --- Step 2: Get valid Granola token ---
    token = get_valid_granola_token()
    if not token:
        log.error("Cannot get valid Granola token — exiting")
        return

    # --- Step 3: Collect meetings to process ---
    meetings_to_process = get_recent_meetings(cache_state)

    # Add previously deferred meetings (panels weren't ready last time)
    deferred_docs = get_deferred_meeting_docs(cache_state)
    recent_ids = {d.get("id") for d in meetings_to_process}
    for doc in deferred_docs:
        if doc.get("id") not in recent_ids:
            meetings_to_process.append(doc)

    if not meetings_to_process:
        log.info("No meetings to process — exiting")
        return

    log.info("Processing %d meeting(s)", len(meetings_to_process))

    # --- Step 4: Process each meeting ---
    results = {"success": 0, "deferred": 0, "skipped": 0, "failed": 0}
    for doc in meetings_to_process:
        result = process_meeting(doc, token)
        results[result] = results.get(result, 0) + 1

    log.info("Done. Results: %s", results)

    if results["failed"] > 0:
        notify(
            "Meeting Follow-Up",
            f"{results['failed']} email(s) failed. Check logs.",
            sound="Basso",
        )


if __name__ == "__main__":
    main()
