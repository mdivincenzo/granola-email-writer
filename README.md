# Granola Email Writer

Auto-drafts follow-up emails in Gmail after external meetings using Granola meeting notes + Claude API.

## How it works
- LaunchAgent watches Granola's cache file for changes
- On update, pulls latest meeting data (notes, transcript, attendees)
- Skips internal (rokt.com-only) meetings
- Sends to Claude API (Haiku) to generate a contextual follow-up email
- Creates a draft in Gmail with To/CC pre-filled

## Setup
See the setup guide in the granola-notion-sync repo or the original conversation history.
