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

def log_message(message):
    """Write a timestamped log message to ~/.claude/logs/subagent_stop_hook.log"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_path = Path.home() / ".claude" / "logs" / "subagent_stop_hook.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")

    # Also print to stderr for immediate feedback
    print(f"[{timestamp}] {message}", file=sys.stderr)

def extract_latest_message(transcript_path):
    """
    Extract the latest Claude message from the transcript file (JSONL format).
    """
    try:
        if not transcript_path or not Path(transcript_path).exists():
            log_message(f"📄 Transcript file not found: {transcript_path}")
            return None

        # Parse JSONL file - each line is a separate JSON object
        messages = []
        with open(transcript_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        messages.append(entry)
                    except json.JSONDecodeError as e:
                        log_message(f"⚠️ JSON decode error on line {line_num}: {e}")
                        continue

        log_message(f"📖 Parsed {len(messages)} entries from transcript")

        # Find the most recent assistant message with text content
        assistant_messages_found = 0
        for entry in reversed(messages):
            # Check if this is an assistant message
            message = entry.get('message', {})
            if message.get('role') == 'assistant':
                assistant_messages_found += 1
                content = message.get('content', [])

                # Content can be a string or array
                if isinstance(content, str):
                    if content.strip():
                        log_message(f"✅ Found assistant message (string format): {content[:50]}...")
                        return content.strip()
                elif isinstance(content, list):
                    # Extract text content from the array
                    for item in content:
                        if isinstance(item, dict) and item.get('type') == 'text':
                            text = item.get('text', '')
                            if text.strip():
                                log_message(f"✅ Found assistant message (array format): {text[:50]}...")
                                return text.strip()

        log_message(f"🔍 Found {assistant_messages_found} assistant messages but none with text content")
        return None
    except Exception as e:
        log_message(f"❌ Error extracting message: {e}")
        return None

def send_to_slack_app(session_id, message, hook_type="subagent_stop"):
    """
    Send the message to the Slack app via HTTP POST.
    """
    try:
        payload = {
            "session_id": session_id,
            "message": message,
            "hook_type": hook_type
        }

        log_message("🔄 SUBAGENT STOP HOOK TRIGGERED")
        log_message(f"🚀 Sending payload to http://localhost:8080/claude/hook: {payload}")

        # Send to local Slack app
        response = requests.post(
            "http://localhost:8080/claude/hook",
            json=payload,
            timeout=10
        )

        log_message(f"📡 Response status: {response.status_code}")
        log_message(f"📡 Response body: {response.text}")

        return response.status_code == 200
    except requests.exceptions.RequestException as e:
        log_message(f"🔌 Connection error to Slack app: {e}")
        log_message("💡 Is the Slack app running on localhost:8080?")
        return False

def main():
    try:
        log_message("🔵 SUBAGENT STOP HOOK MAIN CALLED")

        # Read JSON input from stdin
        input_data = json.load(sys.stdin)
        log_message(f"📥 Subagent stop hook input data: {input_data}")

        session_id = input_data.get('session_id', '')
        transcript_path = input_data.get('transcript_path', '')

        log_message(f"🆔 Session ID: {session_id}")
        log_message(f"📄 Transcript path: {transcript_path}")

        if not session_id:
            log_message("❌ No session ID provided, exiting")
            sys.exit(0)

        # Extract latest Claude message
        message = extract_latest_message(transcript_path)
        log_message(f"💬 Extracted message: {message[:100] if message else 'None'}...")

        if message:
            log_message("📤 Sending to Slack app...")
            success = send_to_slack_app(session_id, message, "subagent_stop")
            if success:
                log_message("✅ Successfully sent to Slack")
            else:
                log_message("❌ Failed to send to Slack")
            # Note: No macOS notification for subtask completion - these don't require user attention
        else:
            log_message("⚠️ No message to send")

        sys.exit(0)

    except json.JSONDecodeError:
        # Handle JSON decode errors gracefully
        sys.exit(0)
    except Exception:
        # Handle any other errors gracefully
        sys.exit(0)

if __name__ == '__main__':
    main()
