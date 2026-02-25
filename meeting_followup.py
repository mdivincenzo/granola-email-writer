#!/usr/bin/env python3
"""
Automatic Meeting Follow-Up Email Drafter
Triggered by LaunchAgent when Granola cache updates.
"""

import json
import os
import sys
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
GRANOLA_CACHE = Path.home() / "Library" / "Application Support" / "Granola" / "cache-v3.json"
STATE_FILE = Path.home() / ".meeting-followup" / "state.json"
LOG_FILE = Path.home() / ".meeting-followup" / "followup.log"
GMAIL_CREDENTIALS = Path.home() / ".gmail-mcp" / "credentials.json"
GMAIL_TOKEN = Path.home() / ".gmail-mcp" / "token.json"

INTERNAL_DOMAIN = "rokt.com"
MY_EMAIL = "matthew.divincenzo@rokt.com"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

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
# STATE MANAGEMENT
# ---------------------------------------------------------------------------
def load_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_meeting_ids": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def already_processed(meeting_id):
    state = load_state()
    return meeting_id in state["processed_meeting_ids"]


def mark_processed(meeting_id):
    state = load_state()
    state["processed_meeting_ids"].append(meeting_id)
    state["processed_meeting_ids"] = state["processed_meeting_ids"][-100:]
    state["last_run"] = datetime.now().isoformat()
    save_state(state)


# ---------------------------------------------------------------------------
# GRANOLA: Read and parse cache
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


def get_latest_meeting(state):
    documents = state.get("documents", {})
    if not documents:
        log.warning("No documents in cache")
        return None

    doc_list = list(documents.values())
    log.info("Found %d documents in cache", len(doc_list))
    doc_list = [d for d in doc_list if not d.get("deleted_at")]

    def get_date(d):
        try:
            gcal = d.get("google_calendar_event") or {}
            start = gcal.get("start", {}).get("dateTime", "")
            if start:
                return datetime.fromisoformat(start)
        except (ValueError, TypeError):
            pass
        try:
            return datetime.fromisoformat(d.get("created_at", "").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    doc_list.sort(key=get_date, reverse=True)
    latest = doc_list[0]

    log.info("Latest document: %s (%s)", latest.get("title", "Untitled"), latest.get("created_at", ""))

    meeting_time = get_date(latest)
    now = datetime.now(timezone.utc)
    if meeting_time.tzinfo is None:
        meeting_time = meeting_time.replace(tzinfo=timezone.utc)
    age = now - meeting_time
    if age > timedelta(hours=2):
        log.info("Latest meeting is %.1f hours old — skipping (must be <2 hours)",
                 age.total_seconds() / 3600)
        return None

    return latest


# ---------------------------------------------------------------------------
# PANEL EXTRACTION
# ---------------------------------------------------------------------------
def extract_panel_content(content):
    """Recursively extract text from Granola panel content blocks."""
    if not content or not isinstance(content, dict):
        return ""
    parts = []
    for block in content.get("content", []):
        block_type = block.get("type", "")

        if block_type == "bulletList":
            for li in block.get("content", []):
                li_text = extract_panel_content(li)
                if li_text.strip():
                    parts.append("- " + li_text.strip() + "\n")
        elif "content" in block:
            for item in block.get("content", []):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif "content" in item:
                    parts.append(extract_panel_content(item))
            if block_type in ("heading", "paragraph"):
                parts.append("\n")

    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# MEETING DATA EXTRACTION
# ---------------------------------------------------------------------------
def extract_meeting_data(doc, state):
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

    # IMPORTANT: Use `or ""` not `get(key, "")` because Granola stores
    # explicit null/None values (not missing keys). get(key, "") returns
    # None when the key exists with value None.
    notes = doc.get("notes_markdown") or doc.get("notes_plain") or ""
    summary = doc.get("summary") or ""

    full_notes = ""
    if summary:
        full_notes += "SUMMARY:\n" + summary + "\n\n"
    if notes:
        full_notes += "NOTES:\n" + notes + "\n\n"

    # AI-generated notes live in documentPanels, NOT in the document itself.
    # This is a separate top-level dict in state, keyed by document ID.
    doc_id = doc.get("id", "")
    panels = state.get("documentPanels", {})
    if doc_id in panels:
        doc_panels = panels[doc_id]
        if isinstance(doc_panels, dict):
            for panel in doc_panels.values():
                if isinstance(panel, dict):
                    ptitle = panel.get("title") or ""
                    pcontent = panel.get("content", {})
                    extracted = extract_panel_content(pcontent)
                    if extracted:
                        full_notes += ptitle + ":\n" + extracted + "\n\n"
                        log.info("Found panel: %s (%d chars)", ptitle, len(extracted))

    # Also pull transcript if available
    transcripts = state.get("transcripts", {})
    if doc_id in transcripts:
        transcript_entries = transcripts[doc_id]
        if isinstance(transcript_entries, list) and transcript_entries:
            transcript_text = " ".join(
                entry.get("text", "") for entry in transcript_entries
                if isinstance(entry, dict)
            )
            if transcript_text.strip():
                full_notes += "TRANSCRIPT:\n" + transcript_text.strip() + "\n\n"
                log.info("Found transcript: %d chars", len(transcript_text))

    return {
        "id": doc_id,
        "title": doc.get("title") or gcal.get("summary", "Untitled Meeting"),
        "date": start,
        "attendees": attendees,
        "notes": full_notes.strip(),
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
def generate_followup_email(meeting_data, recipients):
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

    prompt = """You are Matthew, a senior sales leader at Rokt. You just got off a call and are writing a follow-up email.

MEETING DETAILS:
- Title: {title}
- Date: {date}
- To (external): {to}
- CC (internal Rokt): {cc}

MEETING CONTENT:
{notes}

INSTRUCTIONS:

Using the full meeting transcript and notes, draft a follow-up email. The email should read like something a sharp, senior seller would actually send. It should lock in commitments, demonstrate you listened, and move the deal or relationship forward.

Subject line: Always use "re: our call today (Rokt)" as the subject line. No exceptions.

Opening: Lead with what matters to them.
Always begin the email with "Hi [first name]," on its own line. The next line should begin with "Great speaking earlier" then transition into reflecting back the most important problem, goal, or priority they articulated during the call. Use labeling where it fits naturally. Phrases like "it sounds like," "it seems like," or "the sense I got" signal that you were listening and invite them to confirm or correct. This builds trust and pulls them into the email.
Adapt the tone to the relationship. For a first or early-stage conversation, the opening should demonstrate that you understood their situation. For an ongoing client relationship, skip the "prove I was listening" framing and get to the point. Not every email needs to open the same way.
Do not add any additional greeting or thank-you beyond "Hi [first name]," and "Great speaking earlier." No "thanks for your time" or "great chatting today."

Recap: What we aligned on.
Summarize the highest impact key points of alignment or decisions made during the conversation (max number of 3). Write these in flowing prose, not bullet points, but it should be TWO SENTENCES MAX. They should sound like you're recapping a conversation you had with another person, not logging notes into a CRM. Only include things that were actually discussed and agreed upon. Do not invent commitments, deliverables, or workstreams that were not explicitly mentioned in the transcript.

Commitments: Who is doing what.
State who committed to what, with timelines where mentioned. Assign every action to the correct person based on what was actually said in the transcript. This is critical: if the prospect said "send me some times," the action belongs to Rokt, not the prospect. Misattributing an action item destroys credibility. Read the transcript carefully and get this right. If no clear commitments were made by either side, propose a reasonable next action rather than fabricating one.
Write commitments inline or in a short, tight list. No headers like "From our side" / "From your side" unless there are 3+ action items per side. For one or two items total, weave them into the body of the email naturally.

Value add (when appropriate): One thing they didn't ask for.
If the conversation surfaced a challenge, question, or priority where a relevant insight, resource, case study, or data point would genuinely help, include it. This should deepen their thinking or give your champion ammunition to sell internally, not be a product pitch.
This is not mandatory for every email. Skip it on routine syncs, quick check-ins, or calls where the next steps are purely operational. Forcing a value-add into an email that doesn't need one makes the email feel bloated and performative. When in doubt, leave it out.

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

Sign off with "Best," then "Matthew".

Respond with ONLY a JSON object (no markdown, no backticks):
{{"subject": "...", "body": "..."}}""".format(
        title=meeting_data["title"],
        date=date_str,
        to=", ".join(recipients["to"]),
        cc=", ".join(recipients["cc"]),
        notes=meeting_data["notes"][:20000],
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
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
# MAIN
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 50)
    log.info("Meeting follow-up triggered")

    time.sleep(5)

    state = parse_cache()
    if not state:
        log.info("Could not parse cache — exiting")
        return

    meeting_raw = get_latest_meeting(state)
    if not meeting_raw:
        log.info("No recent meeting found — exiting")
        return

    meeting = extract_meeting_data(meeting_raw, state)
    log.info("Meeting: %s | Date: %s | Attendees: %d | Notes: %d chars",
             meeting["title"], meeting["date"], len(meeting["attendees"]), len(meeting["notes"]))

    if meeting["id"] and already_processed(meeting["id"]):
        log.info("Meeting %s already processed — skipping", meeting["id"])
        return

    if not is_external_meeting(meeting["attendees"]):
        log.info("Internal meeting — no follow-up needed")
        if meeting["id"]:
            mark_processed(meeting["id"])
        return

    recipients = get_recipients(meeting["attendees"])
    if not recipients["to"]:
        log.warning("No external email addresses found — skipping")
        if meeting["id"]:
            mark_processed(meeting["id"])
        return

    log.info("External meeting. To: %s, CC: %s", recipients["to"], recipients["cc"])

    if not meeting["notes"].strip():
        log.warning("No notes/summary/panels found for this meeting — skipping")
        return

    email = generate_followup_email(meeting, recipients)
    if not email:
        log.error("Failed to generate email — exiting")
        return

    success = create_gmail_draft(
        subject=email["subject"],
        body=email["body"],
        to=recipients["to"],
        cc=recipients["cc"],
    )

    if success:
        log.info("Follow-up draft created in Gmail")
        if meeting["id"]:
            mark_processed(meeting["id"])
    else:
        log.error("Failed to create Gmail draft")


if __name__ == "__main__":
    main()
