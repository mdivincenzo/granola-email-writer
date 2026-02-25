# Granola Email Writer

Automatically drafts follow-up emails in Gmail after external meetings end in Granola.
```
Meeting ends → Granola saves notes → cache file updates
→ macOS LaunchAgent detects change → Python script runs
→ Checks if external → Claude drafts email → Saves to Gmail Drafts
```

Internal meetings (all @your-domain.com) are silently skipped. Nothing is ever sent automatically — drafts sit in Gmail for your review.

## Requirements

- macOS
- [Granola](https://granola.ai) installed with at least one recorded meeting
- Python 3.8+ (`python3 --version` to check)
- [Anthropic API key](https://console.anthropic.com)
- Google Cloud project with Gmail API enabled (setup below)
- Node.js / npm (for Gmail OAuth helper)

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
2. Click the project dropdown at the top → **New Project**
3. Name it (e.g. "Meeting Followup") → **Create**
4. Make sure your new project is selected in the dropdown

#### 3b. Enable the Gmail API

1. In the left sidebar, go to **APIs & Services → Library**
2. Search for **Gmail API**
3. Click it → **Enable**

#### 3c. Configure the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**
2. Select **External** → **Create**
3. Fill in app name, support email, developer contact email
4. Click **Save and Continue**
5. On the **Scopes** page, click **Add or Remove Scopes**
6. Find and check `https://www.googleapis.com/auth/gmail.modify`
7. Click **Update** → **Save and Continue**
8. On **Test users**, click **Add Users** → add your Gmail address
9. **Save and Continue** → **Back to Dashboard**

#### 3d. Create OAuth credentials

1. Go to **APIs & Services → Credentials**
2. Click **+ Create Credentials → OAuth client ID**
3. Application type: **Desktop app**
4. Name it anything → **Create**
5. **Download the JSON file**

Move and rename it:
```bash
mkdir -p ~/.gmail-mcp
mv ~/Downloads/client_secret_*.json ~/.gmail-mcp/gcp-oauth.keys.json
```

#### 3e. Authenticate
```bash
npx @gongrzhe/server-gmail-autoauth-mcp auth
```

This opens your browser. Sign in with your Google account and authorize the app. Once you see "Authentication successful" in the terminal, you're done.

Then build the token file the Python Google client can use:
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

### Step 4: Verify Granola cache
```bash
ls -la ~/Library/Application\ Support/Granola/cache-v3.json
```

If this file doesn't exist, make sure Granola is running and you've recorded at least one meeting.

### Step 5: Install the script
```bash
mkdir -p ~/.meeting-followup
cp meeting_followup.py ~/.meeting-followup/
```

Edit the config at the top of the script:
```bash
nano ~/.meeting-followup/meeting_followup.py
```

Update these lines:
```python
INTERNAL_DOMAIN = "your-company.com"      # emails matching this are internal
MY_EMAIL = "you@your-company.com"          # your email (excluded from CC)
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

You should see "Meeting follow-up triggered" and either a draft creation or "skipping" if the latest meeting is older than 2 hours. To force a test on an older meeting, temporarily change `timedelta(hours=2)` to `timedelta(hours=24)` in the script, run it, then change it back.

### Step 7: Set up the automatic trigger

Copy the LaunchAgent plist:
```bash
cp com.meeting-followup.plist ~/Library/LaunchAgents/
```

Replace placeholders:
```bash
# Set your home directory
sed -i '' "s|YOUR_HOME_DIR|$HOME|g" ~/Library/LaunchAgents/com.meeting-followup.plist
```

Set your API key (use nano because API keys contain characters that break sed):
```bash
nano ~/Library/LaunchAgents/com.meeting-followup.plist
# Find YOUR_ANTHROPIC_API_KEY and replace with your actual key
# Ctrl+O to save, Ctrl+X to exit
```

Load the agent:
```bash
launchctl load ~/Library/LaunchAgents/com.meeting-followup.plist
```

Verify:
```bash
launchctl list | grep meeting-followup
```

You should see a line with `com.meeting-followup`. First column is exit code: `-` means it hasn't run yet, `0` means success.

## How it works in practice

1. You finish an external meeting
2. Granola processes the recording and saves notes to its cache file
3. macOS detects the file change and fires the script (5-second delay for Granola to finish writing)
4. The script reads the latest meeting and checks attendees:
   - **All internal?** → Logs "Internal meeting" and exits. No API call made.
   - **Any external emails?** → Sends notes + transcript to Claude
5. Claude generates a contextual follow-up email based on the full transcript and AI-generated notes
6. Draft is saved to Gmail with:
   - **To:** External attendees
   - **CC:** Internal colleagues (excluding you)
   - **Subject:** Generated from meeting context
7. You open Gmail → Drafts → review, tweak if needed, and send

Processed meeting IDs are tracked in `~/.meeting-followup/state.json` to prevent duplicate drafts.

## Configuration

| Setting | Location | Default |
|---------|----------|---------|
| Internal domain | `meeting_followup.py` line 24 | `rokt.com` |
| Your email | `meeting_followup.py` line 25 | — |
| Recency window | `meeting_followup.py` line 118 | 2 hours |
| Throttle interval | plist `ThrottleInterval` | 30 seconds |
| Model | `meeting_followup.py` line 346 | `claude-haiku-4-5-20251001` |

## Troubleshooting

**"Latest meeting is X hours old — skipping"** — Working correctly. The script only drafts for meetings that ended within the last 2 hours.

**"Internal meeting — no follow-up needed"** — All attendees matched your internal domain. Expected behavior.

**"Google API packages not installed"** — Run `pip3 install google-auth google-auth-oauthlib google-api-python-client --break-system-packages`

**"Failed to refresh Gmail token"** — Your OAuth token expired. Re-run `npx @gongrzhe/server-gmail-autoauth-mcp auth` and rebuild the token (Step 3e).

**"Claude API error"** — Check your API credits at [console.anthropic.com](https://console.anthropic.com).

**LaunchAgent not firing** — Reload it:
```bash
launchctl unload ~/Library/LaunchAgents/com.meeting-followup.plist
launchctl load ~/Library/LaunchAgents/com.meeting-followup.plist
```

## Logs
```bash
# Script log
cat ~/.meeting-followup/followup.log

# LaunchAgent stdout/stderr
cat ~/.meeting-followup/launchagent-stdout.log
cat ~/.meeting-followup/launchagent-stderr.log
```

## Granola cache structure

For reference, the script reads from `~/Library/Application Support/Granola/cache-v3.json`:
```
cache-v3.json
  └── "cache" (JSON string — requires double-parsing)
        └── "state"
              ├── "documents"       → meeting metadata, attendees, calendar events
              ├── "documentPanels"  → AI-generated notes (Summary, Action Items, etc.)
              └── "transcripts"     → full meeting transcripts
```

Key gotchas the script handles: the cache is JSON-inside-JSON, many fields are explicitly null (not missing), AI notes live in documentPanels not in the document itself, and calendar room resources appear in attendee lists with @resource.calendar.google.com emails.
