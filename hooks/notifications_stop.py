#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.8"
# dependencies = ["requests"]
# ///

import json
import sys
import requests
from pathlib import Path
from datetime import datetime

try:
    from macos_notification import send_macos_notification, extract_latest_message, has_ask_user_question
except ImportError:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "macos_notification",
        Path(__file__).parent / "macos_notification.py"
    )
    macos_notification = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(macos_notification)
    send_macos_notification = macos_notification.send_macos_notification
    extract_latest_message = macos_notification.extract_latest_message
    has_ask_user_question = macos_notification.has_ask_user_question


def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_path = Path.home() / ".claude" / "logs" / "stop_hook.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")
    print(f"[{timestamp}] {message}", file=sys.stderr)


def send_to_slack_app(session_id, message, hook_type):
    try:
        payload = {"session_id": session_id, "message": message, "hook_type": hook_type}
        log_message(f"🚀 Sending to Slack: {payload}")
        response = requests.post("http://localhost:8080/claude/hook", json=payload, timeout=10)
        log_message(f"📡 Slack response: {response.status_code}")
        return response.status_code == 200
    except requests.exceptions.RequestException as e:
        log_message(f"🔌 Slack connection error: {e}")
        return False


def main():
    try:
        log_message("🛑 STOP HOOK TRIGGERED")

        input_data = json.load(sys.stdin)
        session_id = input_data.get("session_id", "")
        transcript_path = input_data.get("transcript_path", "")

        if not session_id:
            log_message("❌ No session ID, exiting")
            sys.exit(0)

        message = extract_latest_message(transcript_path)
        if not message:
            log_message("⚠️ No message to send")
            sys.exit(0)

        needs_input = has_ask_user_question(transcript_path)
        if needs_input:
            subtitle = "Needs Input"
            sound = "Glass"
            hook_type = "stop_needs_input"
        else:
            subtitle = "Task Complete"
            sound = "Hero"
            hook_type = "stop_complete"

        log_message(f"📤 Notifying both channels — subtitle: {subtitle!r}, hook_type: {hook_type!r}")

        slack_success = send_to_slack_app(session_id, message, hook_type)
        log_message(f"{'✅' if slack_success else '❌'} Slack")

        macos_success = send_macos_notification(message, subtitle=subtitle, sound=sound)
        log_message(f"{'✅' if macos_success else '❌'} macOS")

        sys.exit(0)

    except json.JSONDecodeError:
        sys.exit(0)
    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
