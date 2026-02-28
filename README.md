# Granola Email Writer

Automatically drafts follow-up emails in Gmail after external meetings, using Granola transcripts and the Claude API.

```
Meeting ends → Granola saves recording → cache file updates
→ macOS LaunchAgent detects change → script fetches transcript via API
→ labels speakers by name → Claude drafts email → saves to Gmail Drafts
```

Internal meetings (all @your-domain.com) are silently skipped. Speakerphone calls are skipped since speakers can't be identified. Nothing is ever sent — drafts sit in Gmail for your review.

Includes **Automatic.app**, a native macOS status viewer you can keep in your Dock to monitor pipeline health and activity.

## Architecture

```
┌─────────────────────────┐     ┌───────────────────────┐
│   Granola cache file    │     │    Granola API         │
│   (trigger + metadata)  │────▶│  transcript + panels   │
└─────────────────────────┘     └───────────┬───────────┘
         │ WatchPaths                       │
         ▼                                  ▼
┌─────────────────────────────────────────────────────────┐
│  meeting_followup.py                                    │
│                                                         │
│  1. Parse cache for meeting metadata + attendees        │
│  2. Fetch transcript from Granola API                   │
│  3. Label speakers (microphone → you, system → them)    │
│  4. Pull Gmail thread history for relationship context  │
│  5. Claude generates follow-up email                    │
│  6. Save as Gmail draft                                 │
│  7. Write status data for Automatic.app                 │
└─────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
┌─────────────────┐          ┌────────────────────┐
│   Gmail Drafts  │          │  Automatic.app     │
│   (your inbox)  │          │  (Dock status app)  │
└─────────────────┘          └────────────────────┘
```

The cache file is used only as a **trigger** and **metadata source** (meeting IDs, attendees, dates). All meeting content — transcripts and AI notes — comes from Granola's API. The script auto-discovers the highest-version cache file (`cache-v3.json`, `cache-v4.json`, etc.) and handles both JSON-string and native-dict formats, so Granola upgrades won't break anything.

## Requirements

- macOS
- [Granola](https://granola.ai) with at least one recorded meeting
- Python 3.8+
- [Anthropic API key](https://console.anthropic.com)
- Google Cloud project with Gmail API enabled (setup below)
- Node.js / npm (for Gmail OAuth helper)

Optional:
- [pywebview](https://pywebview.flowrl.com/) (for Automatic.app status viewer)

## Installation

### Step 1: Clone the repo

```bash
git clone https://github.com/mdivincenzo/granola-email-writer.git
cd granola-email-writer
```

### Step 2: Install Python dependencies

```bash
pip3 install anthropic google-auth google-auth-oauthlib google-api-python-client --break-system-packages
```

### Step 3: Set up Gmail API access

#### 3a. Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click the project dropdown → **New Project**
3. Name it (e.g. "Meeting Followup") → **Create**
4. Select your new project in the dropdown

#### 3b. Enable the Gmail API

1. Go to **APIs & Services → Library**
2. Search for **Gmail API** → click it → **Enable**

#### 3c. Configure the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**
2. Select **External** → **Create**
3. Fill in app name, support email, developer contact email
4. **Save and Continue**
5. On **Scopes**, click **Add or Remove Scopes**
6. Check `https://www.googleapis.com/auth/gmail.modify`
7. **Update** → **Save and Continue**
8. On **Test users**, add your Gmail address
9. **Save and Continue** → **Back to Dashboard**

#### 3d. Create OAuth credentials

1. Go to **APIs & Services → Credentials**
2. **+ Create Credentials → OAuth client ID**
3. Application type: **Desktop app** → **Create**
4. **Download the JSON file**

Move and rename it:
```bash
mkdir -p ~/.gmail-mcp
mv ~/Downloads/client_secret_*.json ~/.gmail-mcp/gcp-oauth.keys.json
```

#### 3e. Authenticate

```bash
npx @gongrzhe/server-gmail-autoauth-mcp auth
```

Sign in and authorize when your browser opens. Then build the token file:
```bash
python3 << 'PYEOF'
import json, os

home = os.path.expanduser("~")
keys = json.load(open(f"{home}/.gmail-mcp/gcp-oauth.keys.json"))
creds = json.load(open(f"{home}/.gmail-mcp/credentials.json"))
client = keys["installed"]

token = {
    "token": creds["access_token"],
    "refresh_token": creds["refresh_token"],
    "token_uri": client.get("token_uri", "https://oauth2.googleapis.com/token"),
    "client_id": client["client_id"],
    "client_secret": client["client_secret"],
    "scopes": creds["scope"].split(" ")
}

with open(f"{home}/.gmail-mcp/token.json", "w") as f:
    json.dump(token, f, indent=2)

print("Token saved to ~/.gmail-mcp/token.json")
PYEOF
```

### Step 4: Verify Granola is running

```bash
ls ~/Library/Application\ Support/Granola/cache-v*.json
```

You should see at least one cache file. The script auto-discovers the highest version.

### Step 5: Install the script

```bash
mkdir -p ~/.meeting-followup
cp meeting_followup.py ~/.meeting-followup/
```

Edit the config section at the top:
```bash
nano ~/.meeting-followup/meeting_followup.py
```

Update these values:
```python
INTERNAL_DOMAIN = "your-company.com"
MY_EMAIL = "you@your-company.com"
```

### Step 6: Test manually

```bash
export ANTHROPIC_API_KEY="your-key-here"
python3 ~/.meeting-followup/meeting_followup.py
```

Check the log:
```bash
cat ~/.meeting-followup/followup.log
```

You should see "Meeting follow-up triggered" and either a draft creation or "No meetings to process." To test on an older meeting, temporarily increase `MEETING_MAX_AGE_HOURS` (e.g. to 48), run, then set it back.

### Step 7: Set up the automatic trigger

Copy and configure the LaunchAgent plist:
```bash
cp com.matthew.meeting-followup.plist ~/Library/LaunchAgents/
```

Edit it to set your home directory and API key:
```bash
nano ~/Library/LaunchAgents/com.matthew.meeting-followup.plist
```

Replace all instances of `/Users/matthewdivincenzo` with your home directory, and replace `YOUR_ANTHROPIC_API_KEY` with your actual key. Save and exit.

Load the agent:
```bash
launchctl load ~/Library/LaunchAgents/com.matthew.meeting-followup.plist
```

Verify:
```bash
launchctl list | grep meeting-followup
```

First column is exit code: `-` means it hasn't run yet, `0` means success.

### Step 8: Install the Dock app (optional)

```bash
pip3 install pywebview --break-system-packages
cp -r app/Automatic.app /Applications/
```

First launch: right-click → **Open** → **Open** to bypass Gatekeeper. Then drag it to your Dock.

## How It Works

1. You finish an external meeting on Zoom, Meet, etc.
2. Granola processes the recording and updates its cache file
3. macOS LaunchAgent detects the change and fires the script
4. The script reads the cache for metadata (attendees, calendar info) and checks:
   - **All internal?** → Skipped
   - **Single audio source (speakerphone)?** → Skipped (can't identify speakers)
   - **External + two audio channels?** → Proceeds
5. Fetches the full transcript from Granola's API with named speakers (microphone = you, system audio = them)
6. Pulls recent Gmail thread history with the external attendees for relationship context
7. Claude generates a follow-up email using the labeled transcript as the sole source of truth
8. Draft saved to Gmail with To (external) and CC (internal colleagues)
9. You open Gmail → Drafts → review, tweak, send

Processed meeting IDs are tracked in `state.json` to prevent duplicate drafts. Deferred meetings (notes not ready yet) are retried on the next trigger.

## Automatic.app (Status Viewer)

A native macOS app that shows pipeline health and activity. Keep it in your Dock for at-a-glance monitoring.

**Activity tab** — Timeline of every meeting processed: drafts created (green), meetings deferred (yellow), failures (red), and internal skips (gray). Click any event to expand details like transcript size, generation time, and draft ID.

**Health tab** — System checks: LaunchAgent loaded, Granola cache found, Granola auth token valid, Gmail credentials OK, Anthropic API key present, no stale lock files.

Reads `~/.meeting-followup/status.json` which the pipeline writes after every run. Click ↻ to refresh.

The app is a pywebview wrapper around a single HTML file — no servers, no Electron, no background processes. It launches, reads the status file, and displays it in a native WebKit window.

## Configuration

| Setting | Variable | Default |
|---------|----------|---------|
| Internal domain | `INTERNAL_DOMAIN` | `rokt.com` |
| Your email | `MY_EMAIL` | `matthew.divincenzo@rokt.com` |
| Meeting window | `MEETING_MAX_AGE_HOURS` | `8` (hours) |
| Claude model | `MODEL` | `claude-sonnet-4-5-20250929` |
| Gmail lookback | `GMAIL_LOOKBACK_DAYS` | `365` (days) |
| Gmail max messages | `GMAIL_MAX_MESSAGES` | `20` (per contact) |
| Panel retry interval | `PANEL_POLL_INTERVAL` | `30` (seconds) |
| Panel max wait | `PANEL_POLL_MAX_WAIT` | `300` (seconds) |
| Throttle | plist `ThrottleInterval` | `30` (seconds) |

All settings are in the CONFIG section at the top of `meeting_followup.py`, except throttle which is in the plist.

## Email Prompt

The generation prompt enforces:

- **4-8 sentences.** No exceptions.
- **No re-explaining** what was discussed. Both parties were there.
- **1-2 action items** max. Soft threads (meeting in person, intros) woven in naturally.
- **No flattery, no thank-yous.**
- **Future tense** for uncommitted actions, past tense only for completed ones.
- **Confirmed plans stated as confirmed**, not re-asked.
- **Never fabricates** stats, commitments, case studies, or next steps not explicitly said on the call.
- **Subject line** adapts: references company/topic for established contacts, defaults to "re: our call today (Rokt)" for new ones.

The full prompt is in `generate_followup_email()`. Edit it to match your voice.

## File Layout

```
Repository:
├── meeting_followup.py              # pipeline script (copy to ~/.meeting-followup/)
├── com.matthew.meeting-followup.plist  # LaunchAgent config (copy to ~/Library/LaunchAgents/)
├── app/
│   ├── Automatic.app/               # pre-built macOS app (copy to /Applications/)
│   ├── app.py                       # app source (pywebview launcher)
│   ├── status.html                  # app UI (single self-contained file)
│   ├── generate_icon.py             # icon generation script
│   ├── icon.png                     # source icon (1024x1024)
│   └── icon.icns                    # macOS icon format

Runtime files (created automatically):
~/.meeting-followup/
├── meeting_followup.py     # installed copy of the script
├── state.json              # processed + deferred meeting IDs
├── events.jsonl            # structured event log (one JSON per line)
├── status.json             # status snapshot (read by Automatic.app)
├── followup.log            # human-readable log
├── run.lock                # prevents concurrent runs
└── launchagent-stderr.log  # LaunchAgent stderr

~/.gmail-mcp/
├── gcp-oauth.keys.json     # Google OAuth client credentials
├── credentials.json        # raw tokens from auth flow
└── token.json              # formatted token for Python Gmail client
```

## Troubleshooting

**"No meetings to process"** — The script only drafts for meetings within the last 8 hours that haven't already been processed.

**"Internal meeting — skipping"** — All attendees matched your internal domain. Expected.

**"Single audio source — cannot label speakers"** — Speakerphone call. The script needs mic + system audio to know who said what. Use Zoom/Meet/Teams with proper audio routing.

**"Panels/transcript not ready — deferring"** — Granola is still processing. The meeting is saved to the deferred list and retried on the next cache trigger.

**Claude API error** — Check your key and credits at [console.anthropic.com](https://console.anthropic.com). If you rotated the key, update the plist:
```bash
nano ~/Library/LaunchAgents/com.matthew.meeting-followup.plist
launchctl unload ~/Library/LaunchAgents/com.matthew.meeting-followup.plist
launchctl load ~/Library/LaunchAgents/com.matthew.meeting-followup.plist
```

**Gmail token expired** — Re-run `npx @gongrzhe/server-gmail-autoauth-mcp auth` and rebuild the token (Step 3e).

**LaunchAgent not firing:**
```bash
launchctl unload ~/Library/LaunchAgents/com.matthew.meeting-followup.plist
launchctl load ~/Library/LaunchAgents/com.matthew.meeting-followup.plist
launchctl list | grep meeting-followup
```

**Stale lock file** (script crashed mid-run):
```bash
rm -f ~/.meeting-followup/run.lock
```

**Status app shows old data** — Click ↻. If `status.json` doesn't exist yet, run the pipeline once manually.

## Logs

```bash
# Pipeline log
cat ~/.meeting-followup/followup.log

# Structured events (JSON)
cat ~/.meeting-followup/events.jsonl

# LaunchAgent errors
cat ~/.meeting-followup/launchagent-stderr.log
```
