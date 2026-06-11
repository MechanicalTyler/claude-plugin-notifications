#!/usr/bin/env python3
"""
Shared attention-hub client for Claude hooks.

Builds per-session state events and delivers them to the attention hub over
HTTP. Every network operation uses a short timeout and swallows all failures:
an unreachable hub must never block or error a Claude session.

Also hosts the per-channel notification flags (CLAUDE_NOTIFY_MACOS /
CLAUDE_NOTIFY_SLACK) shared by the hook scripts.
"""

import json
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_HUB_URL = "http://localhost:8765"
HUB_TIMEOUT_SECONDS = 2
MESSAGE_SNIPPET_MAX = 200
_FALSY_VALUES = {"0", "false", "no", "off"}


def log_hub(message, log_file="attention_hub_client.log"):
    """Write a timestamped log message to ~/.claude/logs/{log_file}. Never raises."""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_path = Path.home() / ".claude" / "logs" / log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def get_hub_url():
    """Hub base URL from CLAUDE_ATTENTION_HUB_URL, default http://localhost:8765."""
    url = os.environ.get("CLAUDE_ATTENTION_HUB_URL", "").strip() or DEFAULT_HUB_URL
    return url.rstrip("/")


def get_host_label():
    """Host label from CLAUDE_HOST_LABEL, falling back to the machine hostname."""
    label = os.environ.get("CLAUDE_HOST_LABEL", "").strip()
    if label:
        return label
    try:
        return socket.gethostname() or "unknown-host"
    except Exception:
        return "unknown-host"


def _channel_enabled(env_var):
    value = os.environ.get(env_var, "").strip().lower()
    if not value:
        return True
    return value not in _FALSY_VALUES


def macos_enabled():
    """macOS channel flag (CLAUDE_NOTIFY_MACOS). Defaults to enabled."""
    return _channel_enabled("CLAUDE_NOTIFY_MACOS")


def slack_enabled():
    """Slack channel flag (CLAUDE_NOTIFY_SLACK). Defaults to enabled."""
    return _channel_enabled("CLAUDE_NOTIFY_SLACK")


def build_event_payload(session_id, cwd, state, message=None):
    """Build the state-event payload identifying this session to the hub."""
    snippet = (message or "").strip()
    if len(snippet) > MESSAGE_SNIPPET_MAX:
        snippet = snippet[: MESSAGE_SNIPPET_MAX - 3] + "..."
    return {
        "session_id": session_id,
        "project": os.path.basename(os.path.normpath(cwd)) if cwd else "unknown",
        "host": get_host_label(),
        "state": state,
        "message": snippet,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _issue_request(url, method, body, timeout, result):
    """Worker body for _request. Stores True/False in result["ok"]. Never raises."""
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        response = urllib.request.urlopen(req, timeout=timeout)
        try:
            result["ok"] = 200 <= getattr(response, "status", 200) < 300
        finally:
            response.close()
    except urllib.error.HTTPError as e:
        log_hub(f"Hub rejected request ({method} {url}): HTTP {e.code}")
        result["ok"] = False
    except Exception as e:
        log_hub(f"Hub unreachable ({method} {url}): {e}")
        result["ok"] = False


def _request(url, method, body=None):
    """Issue an HTTP request to the hub with a bounded total wall clock.

    urlopen's timeout only covers socket operations — DNS resolution
    (getaddrinfo) runs before it applies, so a black-holed or unresolvable hub
    URL could stall a hook well past the budget. The request runs in a daemon
    thread joined for HUB_TIMEOUT_SECONDS, capping total time regardless of
    where the delay occurs. Returns True on 2xx, False otherwise. Never raises.
    """
    result = {}
    try:
        worker = threading.Thread(
            target=_issue_request,
            args=(url, method, body, HUB_TIMEOUT_SECONDS, result),
            daemon=True,
        )
        worker.start()
        worker.join(HUB_TIMEOUT_SECONDS)
    except Exception as e:
        log_hub(f"Hub request failed to dispatch ({method} {url}): {e}")
        return False
    if "ok" not in result:
        log_hub(f"Hub request exceeded {HUB_TIMEOUT_SECONDS}s budget ({method} {url})")
        return False
    return result["ok"]


def report_state(session_id, cwd, state, message=None):
    """POST a state event to the hub. Swallows every failure; returns success bool."""
    if not session_id:
        return False
    payload = build_event_payload(session_id, cwd, state, message)
    return _request(f"{get_hub_url()}/api/events", "POST", payload)


def remove_session(session_id):
    """Ask the hub to forget a session. Swallows every failure; returns success bool."""
    if not session_id:
        return False
    quoted_id = urllib.parse.quote(str(session_id), safe="")
    return _request(f"{get_hub_url()}/api/sessions/{quoted_id}", "DELETE")
