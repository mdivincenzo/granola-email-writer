#!/usr/bin/env python3
"""Follow-Up -- Meeting follow-up status viewer.

Opens a native macOS window showing the health and activity of your
automated meeting follow-up pipeline. Read-only: it just displays
data written by the pipeline script.
"""
import json
import os
import webview

STATUS_DIR = os.path.expanduser("~/.meeting-followup")
STATUS_FILE = os.path.join(STATUS_DIR, "status.json")

# Find status.html relative to this script (works both in dev and inside .app bundle)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_FILE = os.path.join(SCRIPT_DIR, "status.html")


class StatusAPI:
    """Exposed to JavaScript via pywebview's js_api bridge."""

    def refresh(self):
        """Re-read status.json from disk and return fresh data."""
        return load_status()


def load_status():
    """Load status.json. Returns empty defaults if the file is missing or invalid."""
    try:
        with open(STATUS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "last_updated": None,
            "health": {},
            "config": {},
            "events": [],
        }


def build_html():
    """Read the HTML template and inject current status data."""
    with open(TEMPLATE_FILE, "r") as f:
        template = f.read()
    data = load_status()
    return template.replace("__STATUS_DATA_PLACEHOLDER__", json.dumps(data))


def main():
    api = StatusAPI()
    html_content = build_html()
    webview.create_window(
        title="Automatic",
        html=html_content,
        width=700,
        height=800,
        resizable=True,
        min_size=(500, 600),
        js_api=api,
    )
    webview.start()


if __name__ == "__main__":
    main()
