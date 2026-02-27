#!/usr/bin/env python3
"""
Automatic Meeting Follow-Up Email Drafter
==========================================
Triggered by LaunchAgent when Granola cache updates.

Architecture:
- Cache file:    TRIGGER ONLY + meeting metadata (IDs, attendees, dates)
- Granola API:   Fetch AI-generated panels (notes/summary) + transcript
- Claude API:    Generate follow-up email draft
- Gmail API:     Push draft to Gmail

v3 improvements over v2:
- Resilient cache parsing: handles v3 (JSON string), v4+ (native dict), and
  auto-discovers cache files so Granola version upgrades don't break the script
- Cache is treated as a metadata source only — all content from API
- Better None-safety throughout calendar event parsing
"""

import json
import os
import sys
import time
import glob
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
GRANOLA_DATA_DIR = Path.home() / "Library" / "Application Support" / "Granola"
GRANOLA_AUTH = GRANOLA_DATA_DIR / "supabase.json"
GRANOLA_PANELS_URL = "https://api.granola.ai/v1/get-document-panels"
GRANOLA_TRANSCRIPT_URL = "https://api.granola.ai/v1/get-document-transcript"

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
MEETING_MAX_AGE_HOURS = 8  # process meetings up to this old

# Gmail context (Layer 2): pull recent email history with external contacts
GMAIL_LOOKBACK_DAYS = 365  # search up to 12 months back
GMAIL_MAX_MESSAGES = 20    # cap at 20 messages per contact

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
        wt = data.get("workos_tokens", "{}")
        # Handle both string (older) and dict (newer) formats
        if isinstance(wt, str):
            tokens = json.loads(wt)
        else:
            tokens = wt
        token = tokens.get("access_token")
        if not token:
            log.error("No access_token in Granola auth file")
            return None
        return token
    except (json.JSONDecodeError, KeyError) as e:
        log.error("Failed to read Granola auth: %s", e)
        return None


def refresh_granola_token():
    """Attempt to refresh the Granola token using the stored refresh_token."""
    if not GRANOLA_AUTH.exists():
        return None
    try:
        data = json.loads(GRANOLA_AUTH.read_text())
        wt = data.get("workos_tokens", "{}")
        if isinstance(wt, str):
            tokens = json.loads(wt)
        else:
            tokens = wt
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            log.warning("No refresh_token available")
            return None

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
            # Write back in whatever format it was stored
            if isinstance(data.get("workos_tokens"), str):
                data["workos_tokens"] = json.dumps(tokens)
            else:
                data["workos_tokens"] = tokens
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
                "Authorization": "Bearer %s" % token,
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
            return token
    except Exception:
        return token


# ---------------------------------------------------------------------------
# GRANOLA API: Fetch panels + transcript
# ---------------------------------------------------------------------------
def fetch_panels(meeting_id, token):
    """Fetch AI-generated panels from Granola API for a given meeting ID."""
    try:
        req = Request(
            GRANOLA_PANELS_URL,
            data=json.dumps({"document_id": meeting_id}).encode(),
            headers={
                "Authorization": "Bearer %s" % token,
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",
            },
        )
        resp = urlopen(req, timeout=30)
        raw = resp.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw)
    except HTTPError as e:
        log.error("Granola API HTTP error %d: %s", e.code, e.reason)
        return None
    except URLError as e:
        log.error("Granola API connection error: %s", e.reason)
        return None
    except Exception as e:
        log.error("Granola API unexpected error: %s", e)
        return None


def fetch_transcript(meeting_id, token):
    """Fetch the full meeting transcript from Granola API."""
    try:
        req = Request(
            GRANOLA_TRANSCRIPT_URL,
            data=json.dumps({"document_id": meeting_id}).encode(),
            headers={
                "Authorization": "Bearer %s" % token,
                "Content-Type": "application/json",
                "Accept-Encoding": "gzip",
            },
        )
        resp = urlopen(req, timeout=30)
        raw = resp.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw)
    except HTTPError as e:
        log.error("Transcript API HTTP error %d: %s", e.code, e.reason)
        return None
    except URLError as e:
        log.error("Transcript API connection error: %s", e.reason)
        return None
    except Exception as e:
        log.error("Transcript API unexpected error: %s", e)
        return None


def format_transcript(segments, my_name="Me", their_name="Them"):
    """Convert transcript segments into readable text with real speaker names.

    Requires two audio sources (microphone + system) for accurate labeling.
    Returns empty string if only one source is detected (e.g. speakerphone).

    Args:
        segments: list of transcript segments from Granola API
        my_name: name for microphone source (the Granola user)
        their_name: name for system source (the other participant)
    """
    if not segments or not isinstance(segments, list):
        return ""

    # Require two audio sources — single source means we can't label speakers
    sources = set(seg.get("source", "") for seg in segments if seg.get("text", "").strip())
    if len(sources) < 2:
        log.warning("Only one audio source detected (%s) — likely speakerphone. "
                     "Cannot label speakers accurately.", sources)
        return ""

    lines = []
    last_speaker = None
    current_text = []

    for seg in segments:
        speaker = my_name if seg.get("source") == "microphone" else their_name
        text = seg.get("text", "").strip()
        if not text:
            continue
        if speaker == last_speaker:
            current_text.append(text)
        else:
            if current_text and last_speaker:
                lines.append("%s: %s" % (last_speaker, " ".join(current_text)))
            current_text = [text]
            last_speaker = speaker

    if current_text and last_speaker:
        lines.append("%s: %s" % (last_speaker, " ".join(current_text)))

    return "\n".join(lines)


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
                    parts.append("%d. %s\n" % (i, li_text.strip()))

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
            all_notes.append("%s:\n%s" % (title, text))
    return "\n\n".join(all_notes)


def fetch_panels_with_retry(meeting_id, token, my_name="Me", their_name="Them"):
    """Poll Granola API until meeting is processed, then fetch transcript.

    Panels are used ONLY as a readiness signal (Granola has finished processing).
    Only the labeled transcript is returned. If speaker separation is unavailable
    (e.g. speakerphone), returns None to skip the meeting.

    Returns:
        transcript text string, or None if unavailable/unlabeled
    """
    elapsed = 0
    while elapsed < PANEL_POLL_MAX_WAIT:
        panels = fetch_panels(meeting_id, token)
        if panels:
            notes_text = panels_to_notes(panels)
            if len(notes_text.strip()) >= PANEL_MIN_CHARS:
                log.info("Panels ready (readiness signal): %d chars after %ds",
                         len(notes_text), elapsed)
                segments = fetch_transcript(meeting_id, token)
                if segments:
                    transcript_text = format_transcript(segments, my_name, their_name)
                    if transcript_text.strip():
                        log.info("Transcript fetched: %d chars", len(transcript_text))
                        return "TRANSCRIPT:\n" + transcript_text
                    else:
                        log.warning("Transcript has no speaker separation "
                                    "(speakerphone?) — skipping")
                        return None
                else:
                    log.warning("Transcript not available")
                    return None
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
            log.info("Panels ready on final attempt")
            segments = fetch_transcript(meeting_id, token)
            if segments:
                transcript_text = format_transcript(segments, my_name, their_name)
                if transcript_text.strip():
                    return "TRANSCRIPT:\n" + transcript_text

    log.warning("Meeting not ready after %ds", PANEL_POLL_MAX_WAIT)
    return None


# ---------------------------------------------------------------------------
# CACHE: Auto-discover + resilient parsing (metadata only)
# ---------------------------------------------------------------------------
def find_granola_cache():
    """Auto-discover the latest Granola cache file (cache-v3, v4, v5, etc.)."""
    pattern = str(GRANOLA_DATA_DIR / "cache-v*.json")
    candidates = glob.glob(pattern)
    if not candidates:
        log.error("No Granola cache file found matching %s", pattern)
        return None
    # Sort by version number descending — pick the highest
    def version_key(path):
        try:
            fname = os.path.basename(path)
            # Extract number from "cache-v4.json" -> 4
            return int(fname.replace("cache-v", "").replace(".json", ""))
        except (ValueError, IndexError):
            return 0
    candidates.sort(key=version_key, reverse=True)
    chosen = candidates[0]
    log.info("Using cache file: %s", chosen)
    return Path(chosen)


def parse_cache():
    """Parse the Granola cache file, handling both string and dict formats.

    This is ONLY used for meeting metadata (IDs, attendees, dates).
    All actual content (notes, transcript) comes from the Granola API.
    """
    cache_path = find_granola_cache()
    if not cache_path or not cache_path.exists():
        log.error("Granola cache not found")
        return None
    try:
        raw = json.loads(cache_path.read_text())
        cache_val = raw.get("cache")
        if cache_val is None:
            log.error("No 'cache' key in cache file")
            return None

        # v3 format: cache is a JSON string that needs double-parsing
        # v4+ format: cache is already a dict
        if isinstance(cache_val, str):
            inner = json.loads(cache_val)
        elif isinstance(cache_val, dict):
            inner = cache_val
        else:
            log.error("Unexpected cache format: %s", type(cache_val).__name__)
            return None

        # The state can be at inner["state"] or directly in inner
        if "state" in inner:
            return inner["state"]
        elif "documents" in inner:
            # Fallback: documents directly in inner
            return inner
        else:
            log.error("No 'state' or 'documents' key in cache. Keys: %s",
                      list(inner.keys())[:10])
            return None

    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log.error("Failed to parse Granola cache: %s", e)
        return None


def _safe_get_nested(d, *keys, default=None):
    """Safely traverse nested dicts where any value could be None."""
    current = d
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def get_meeting_date(doc):
    """Extract the meeting start datetime, falling back to created_at."""
    try:
        start = _safe_get_nested(doc, "google_calendar_event", "start", "dateTime",
                                 default="")
        if start:
            return datetime.fromisoformat(start)
    except (ValueError, TypeError):
        pass
    try:
        created = (doc.get("created_at") or "").replace("Z", "+00:00")
        if created:
            return datetime.fromisoformat(created)
    except (ValueError, TypeError):
        pass
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
    start_dt = _safe_get_nested(gcal, "start", "dateTime", default="")
    start = start_dt or doc.get("created_at", "")

    attendees = []
    for att in (gcal.get("attendees") or []):
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
def generate_followup_email(meeting_data, recipients, sender_name="Matthew",
                            gmail_context=""):
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

    gmail_section = ""
    if gmail_context:
        gmail_section = (
            "EMAIL HISTORY (recent correspondence with this contact):\n"
            "{gmail_ctx}\n\n"
            "Use the email history to:\n"
            "- Adapt tone to the relationship stage (many emails = established, few = newer)\n"
            "- Reference real follow-ups that happened between meetings (not guessed)\n"
            "- Note threads where you sent something and got no reply (stalled)\n"
            "- NEVER claim an email was sent if it's not in the history\n"
            "- NEVER claim someone didn't reply if there's no outbound to begin with\n"
            "- Do NOT rehash email content in the follow-up. The history is context for YOU, "
            "not material to quote.\n\n"
        ).format(gmail_ctx=gmail_context)

    # Determine if there's recent email history (for subject line logic)
    has_recent_history = False
    if gmail_context:
        # Check if there's email activity in last 30 days
        import re
        # Look for date patterns in gmail context like "Feb 15" — crude but effective
        month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        now = datetime.now()
        thirty_days_ago = now - timedelta(days=30)
        for month in month_names:
            if month in gmail_context:
                has_recent_history = True
                break

    prompt = (
        "You are {sender_name}, a senior sales leader at Rokt. You just got off a call "
        "and are writing a follow-up email.\n\n"
        "MEETING DETAILS:\n"
        "- Title: {title}\n"
        "- Date: {date}\n"
        "- To (external): {to}\n"
        "- CC (internal Rokt): {cc}\n\n"
        "MEETING CONTENT:\n"
        "{notes}\n\n"
        "The transcript is labeled with real speaker names ({sender_name} = you, "
        "the other name = the external attendee). It is the sole source of truth "
        "for who said what.\n\n"
        "{gmail_section}"
        "RULES:\n\n"
        "1. LENGTH: 4-8 sentences total. No exceptions.\n\n"
        "2. NO RESTATING. Both parties were on the call. Reference decisions and "
        "commitments by name, don't re-explain them.\n\n"
        "3. COMMITMENTS: Identify the single most important next step. You may add "
        "one more if critical. Everything else gets cut. If there's a soft commitment "
        "like meeting up in person, reconnecting for an intro, or a networking favor, "
        "weave it in naturally as a brief mention, not a formal action item.\n\n"
        "4. NO FLATTERY, NO THANK-YOUS. Zero. No \"appreciate you,\" no \"great effort,\" "
        "no \"thanks for your time.\" Reference specifics, not adjectives.\n\n"
        "5. TENSE: Future tense for things not yet done (\"I'll send\" not \"I sent\"). "
        "Past tense only for things confirmed complete.\n\n"
        "6. CONFIRMED PLANS: If a specific time was agreed on the call, state it as "
        "confirmed (\"Talk Wednesday at 1pm\"). Do NOT re-ask.\n\n"
        "7. NEVER FABRICATE. No case studies, stats, company names, or resources that "
        "weren't explicitly said on the call. Do not invent commitments to share work, "
        "get feedback, or loop someone in that weren't explicitly stated. If a next step "
        "wasn't said out loud on the call, it doesn't go in the email. When in doubt, "
        "leave it out.\n\n"
        "8. NO EMDASHES. Use commas or periods.\n\n"
        "SUBJECT LINE:\n"
        "{subject_instruction}\n\n"
        "STRUCTURE (flexible, not a rigid template):\n\n"
        "- Open with \"Hi [first name],\" then a blank line. Default to \"Great speaking "
        "earlier\" as your opener, then follow with something that reflects back their "
        "situation or priority. You can occasionally vary the opener for long-standing "
        "relationships where it would feel stale, but \"Great speaking earlier\" is your "
        "go-to.\n"
        "- Reference the key decision or agreement in one sentence.\n"
        "- State your commitment(s) and any soft threads.\n"
        "- Close with a concrete next step. If a time is confirmed, just confirm it. "
        "If nothing was set, propose something specific.\n\n"
        "TONE: Write like you're texting a business friend, not drafting a memo. "
        "Short sentences. Conversational. Confident. Use first names and company "
        "names from the call. If something was casual on the call, keep it casual "
        "in the email.\n\n"
        'Sign off with "Best," then "{sender_name}" on the next line.\n\n'
        "Respond with ONLY a JSON object (no markdown, no backticks). Use \\n for "
        "line breaks in the body:\n"
        '{{"subject": "...", "body": "Hi [name],\\n\\n...\\n\\nBest,\\n{sender_name}"}}'
    ).format(
        title=meeting_data["title"],
        date=date_str,
        to=", ".join(recipients["to"]),
        cc=", ".join(recipients["cc"]),
        notes=meeting_data["notes"][:20000],
        sender_name=sender_name,
        gmail_section=gmail_section,
        subject_instruction=(
            "Adapt the subject line to the relationship and topic. Reference the "
            "company name or topic discussed. Example: \"re: Rokt x [Company]\" or "
            "\"re: [topic] follow-up\". Keep it short and natural."
            if has_recent_history else
            "Use \"re: our call today (Rokt)\" as the subject line."
        ),
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
def get_gmail_service():
    """Build and return an authenticated Gmail API service object."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest
        from googleapiclient.discovery import build
    except ImportError:
        log.error("Google API packages not installed.")
        return None

    creds = None
    if GMAIL_TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN))

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(GRequest())
                GMAIL_TOKEN.write_text(creds.to_json())
            except Exception as e:
                log.error("Failed to refresh Gmail token: %s", e)
                return None
        else:
            log.error("No valid Gmail token.")
            return None

    try:
        return build("gmail", "v1", credentials=creds)
    except Exception as e:
        log.error("Failed to build Gmail service: %s", e)
        return None


def fetch_gmail_context(external_emails, days_back=GMAIL_LOOKBACK_DAYS,
                        max_messages=GMAIL_MAX_MESSAGES):
    """Fetch recent email activity with external contacts from Gmail."""
    if not external_emails:
        return ""

    service = get_gmail_service()
    if not service:
        log.warning("Gmail service unavailable — skipping email context")
        return ""

    all_messages = []

    for email_addr in external_emails:
        query = "from:{e} OR to:{e} newer_than:{d}d -in:drafts".format(
            e=email_addr, d=days_back
        )
        try:
            results = service.users().messages().list(
                userId="me",
                q=query,
                maxResults=max_messages,
            ).execute()
            message_ids = results.get("messages", [])
        except Exception as e:
            log.warning("Gmail search failed for %s: %s", email_addr, e)
            continue

        if not message_ids:
            log.info("  No email history with %s (last %d days)", email_addr, days_back)
            continue

        log.info("  Found %d emails with %s", len(message_ids), email_addr)

        for msg_ref in message_ids:
            try:
                msg = service.users().messages().get(
                    userId="me",
                    id=msg_ref["id"],
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                ).execute()

                headers = {
                    h["name"]: h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                snippet = msg.get("snippet", "")

                from_addr = headers.get("From", "")
                from_lower = from_addr.lower()
                if MY_EMAIL.lower() in from_lower:
                    direction = "YOU \u2192"
                    other = email_addr
                else:
                    direction = email_addr.split("@")[0] + " \u2192"
                    other = "YOU"

                date_str = headers.get("Date", "")
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_str)
                    date_display = dt.strftime("%b %d")
                except Exception:
                    date_display = date_str[:10] if date_str else "?"

                subject = headers.get("Subject", "(no subject)")

                cal_prefixes = ("Invitation:", "Updated invitation:",
                                "Accepted:", "Declined:", "Tentative:")
                if subject.startswith(cal_prefixes):
                    cal_noise = ("Join with Google Meet", "Join by phone",
                                 "has accepted", "has declined", "has tentatively",
                                 "meet.google.com", "zoom.us/j")
                    if any(n in snippet for n in cal_noise):
                        continue

                all_messages.append({
                    "timestamp": msg.get("internalDate", "0"),
                    "date_display": date_display,
                    "direction": direction,
                    "other": other,
                    "subject": subject,
                    "snippet": snippet[:160],
                    "email": email_addr,
                })
            except Exception as e:
                log.debug("Failed to fetch message %s: %s", msg_ref["id"], e)
                continue

    if not all_messages:
        return ""

    all_messages.sort(key=lambda m: int(m["timestamp"]))

    lines = []
    for m in all_messages:
        lines.append(
            '{date} \u2014 {direction} {other}: "{subject}" \u2014 {snippet}'.format(
                date=m["date_display"],
                direction=m["direction"],
                other=m["other"],
                subject=m["subject"],
                snippet=m["snippet"],
            )
        )

    if all_messages:
        last = all_messages[-1]
        if "YOU \u2192" in last["direction"]:
            lines.append("[last outbound message has no reply]")

    header = (
        "RECENT EMAIL ACTIVITY WITH {emails} (last {days} days, up to {n} messages):"
    ).format(
        emails=", ".join(external_emails),
        days=days_back,
        n=max_messages,
    )
    return header + "\n" + "\n".join(lines)


def get_gmail_sender_name():
    """Get the user's display name from Gmail sendAs settings."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest
        from googleapiclient.discovery import build
    except ImportError:
        return None

    creds = None
    if GMAIL_TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN))

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(GRequest())
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
                    return full_name.split()[0]
        return None
    except Exception as e:
        log.warning("Could not get sender name from Gmail: %s", e)
        return None


def create_gmail_draft(subject, body, to, cc):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest
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
                creds.refresh(GRequest())
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
    """Process one meeting: fetch notes via API, generate email, create draft.

    Returns: 'success', 'deferred', 'skipped', or 'failed'
    """
    meeting = extract_meeting_metadata(doc)
    log.info("Processing: %s | Date: %s | Attendees: %d",
             meeting["title"], meeting["date"], len(meeting["attendees"]))

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

    # Determine speaker names for transcript labeling
    # "microphone" = the Granola user (you), "system" = other participant(s)
    sender_name_for_transcript = get_gmail_sender_name() or "Matthew"
    external_names = [a["name"] for a in meeting["attendees"]
                      if a.get("email", "").lower() not in (MY_EMAIL.lower(), "")
                      and not a.get("email", "").endswith("@" + INTERNAL_DOMAIN)]
    if len(external_names) == 1:
        their_name = external_names[0]
    elif external_names:
        their_name = " / ".join(external_names)
    else:
        their_name = "Them"

    # Fetch panels + transcript from Granola API (with retry)
    log.info("  Fetching panels from Granola API...")
    notes_text = fetch_panels_with_retry(
        meeting["id"], token,
        my_name=sender_name_for_transcript,
        their_name=their_name)

    if not notes_text:
        log.warning("  Panels/transcript not ready or no speaker separation — deferring")
        defer_meeting(meeting["id"])
        notify(
            "Meeting Follow-Up",
            "Notes not ready yet for: %s. Will retry." % meeting["title"],
            sound="Purr",
        )
        return "deferred"

    meeting["notes"] = notes_text
    log.info("  Notes: %d chars", len(notes_text))

    # Fetch Gmail context (Layer 2)
    log.info("  Fetching Gmail context for external contacts...")
    gmail_context = fetch_gmail_context(recipients["to"])
    if gmail_context:
        log.info("  Gmail context: %d chars", len(gmail_context))
    else:
        log.info("  No Gmail history found with these contacts")

    # Generate email
    sender_name = get_gmail_sender_name() or "Matthew"
    email = generate_followup_email(meeting, recipients, sender_name=sender_name,
                                    gmail_context=gmail_context)
    if not email:
        log.error("  Failed to generate email")
        notify(
            "Meeting Follow-Up Failed",
            "Could not generate email for: %s" % meeting["title"],
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
            "Draft ready: %s" % meeting["title"],
            sound="Glass",
        )
        return "success"
    else:
        log.error("  Failed to create Gmail draft")
        notify(
            "Meeting Follow-Up Failed",
            "Gmail draft failed for: %s" % meeting["title"],
            sound="Basso",
        )
        return "failed"


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 50)
    log.info("Meeting follow-up triggered")

    lock = LockFile(LOCK_FILE)
    if not lock.acquire():
        log.info("Another instance is running — exiting")
        return
    try:
        _run()
    finally:
        lock.release()


def _run():
    # Short delay to let Granola finish writing cache metadata
    time.sleep(10)

    # --- Step 1: Parse cache for metadata ---
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
            "%d email(s) failed. Check logs." % results["failed"],
            sound="Basso",
        )


if __name__ == "__main__":
    main()
