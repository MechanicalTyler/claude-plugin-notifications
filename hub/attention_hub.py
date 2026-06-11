#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
"""
Claude attention hub — self-hosted session attention tracker.

A zero-dependency (stdlib only) HTTP server that receives per-session state
events from Claude Code hooks and serves a web dashboard showing one
color-coded row per session, sorted needs-attention first.

Manual start (typically inside tmux/screen):

    uv run hub/attention_hub.py [--port 8765] [--bind 0.0.0.0]
                                [--state-file PATH] [--prune-hours 24]

Security: no authentication or TLS. Run it on a trusted private network
(localhost, LAN, VPN/tailnet) only — see README.
"""

import argparse
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8765
DEFAULT_BIND = "0.0.0.0"
DEFAULT_PRUNE_HOURS = 24.0
DEFAULT_STATE_FILE = str(Path.home() / ".claude" / "attention_hub_state.json")

# Lower value sorts first on the dashboard: red, yellow, green.
STATE_PRIORITY = {"waiting": 0, "needs_input": 0, "done": 1, "working": 2}
VALID_STATES = set(STATE_PRIORITY)

# Server-side input caps: the hub trusts no client to truncate for it.
MAX_BODY_BYTES = 64 * 1024
MESSAGE_MAX_CHARS = 200
FIELD_MAX_CHARS = 256


def _clamp(value, limit):
    """Coerce to str and truncate to limit characters."""
    return str(value)[:limit]

SESSION_PATH_RE = re.compile(r"^/api/sessions/([^/]+)$")


class AttentionStore:
    """In-memory per-session state, mirrored to a JSON file after each change."""

    def __init__(self, state_file, prune_hours=DEFAULT_PRUNE_HOURS, now=time.time):
        self._state_file = Path(state_file)
        self._prune_seconds = float(prune_hours) * 3600.0
        self._now = now
        self._lock = threading.Lock()
        self._sessions = {}
        self._load()

    def upsert(self, event):
        """Create or update a session from a state event. Returns the stored record."""
        session_id = _clamp(event.get("session_id") or "", FIELD_MAX_CHARS).strip()
        state = str(event.get("state") or "").strip()
        if not session_id:
            raise ValueError("event is missing session_id")
        if state not in VALID_STATES:
            raise ValueError(f"invalid state {state[:64]!r}")

        now = self._now()
        with self._lock:
            existing = self._sessions.get(session_id)
            record = {
                "session_id": session_id,
                "project": _clamp(event.get("project") or (existing or {}).get("project")
                                  or "unknown", FIELD_MAX_CHARS),
                "host": _clamp(event.get("host") or (existing or {}).get("host")
                               or "unknown", FIELD_MAX_CHARS),
                "state": state,
                "message": _clamp(event.get("message") or "", MESSAGE_MAX_CHARS),
                "state_since": existing["state_since"]
                if existing and existing["state"] == state else now,
                "last_update": now,
            }
            self._sessions[session_id] = record
            self._save()
            return dict(record)

    def delete(self, session_id):
        """Remove a session. Returns True if it existed."""
        with self._lock:
            removed = self._sessions.pop(session_id, None) is not None
            if removed:
                self._save()
            return removed

    def list_sessions(self):
        """Prune stale sessions, then list all sessions with computed durations,
        sorted needs-attention first (red, yellow, green)."""
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            rows = []
            for record in self._sessions.values():
                row = dict(record)
                row["state_seconds"] = max(0.0, now - record["state_since"])
                row["age_seconds"] = max(0.0, now - record["last_update"])
                rows.append(row)
        rows.sort(key=lambda r: (STATE_PRIORITY.get(r["state"], 3), r["state_since"]))
        return rows

    def _prune_locked(self, now):
        stale = [sid for sid, record in self._sessions.items()
                 if now - record["last_update"] > self._prune_seconds]
        for sid in stale:
            del self._sessions[sid]
        if stale:
            self._save()

    def _save(self):
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._state_file.with_suffix(".tmp")
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"sessions": self._sessions}, f, indent=2)
            os.replace(tmp_path, self._state_file)
        except OSError as e:
            print(f"warning: could not persist state to {self._state_file}: {e}")

    def _load(self):
        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            sessions = data.get("sessions", {})
            if isinstance(sessions, dict):
                now = self._now()
                for sid, record in sessions.items():
                    if not (isinstance(record, dict) and record.get("session_id")):
                        continue
                    if record.get("state") not in VALID_STATES:
                        continue
                    for time_field in ("state_since", "last_update"):
                        if not isinstance(record.get(time_field), (int, float)):
                            record[time_field] = now
                    record["session_id"] = sid  # key wins over a hand-edited mismatch
                    record["project"] = _clamp(record.get("project") or "unknown",
                                               FIELD_MAX_CHARS)
                    record["host"] = _clamp(record.get("host") or "unknown",
                                            FIELD_MAX_CHARS)
                    record["message"] = _clamp(record.get("message") or "",
                                               MESSAGE_MAX_CHARS)
                    self._sessions[sid] = record
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError, AttributeError) as e:
            print(f"warning: ignoring unreadable state file {self._state_file}: {e}")


class AttentionHubHandler(BaseHTTPRequestHandler):
    # Keep-alive for the dashboard's 3s poll (every response sets Content-Length).
    protocol_version = "HTTP/1.1"

    @property
    def store(self):
        return self.server.store

    def log_message(self, format, *args):
        pass  # keep the terminal quiet; the dashboard is the surface

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/sessions":
            self._send_json(200, {"sessions": self.store.list_sessions()})
        else:
            self._send_json(404, {"error": "not found"})

    def _reject_unread_body(self, status, error):
        """Reject a request whose body will not be read. The connection must
        close: with keep-alive the unread body would corrupt the next request."""
        self.close_connection = True
        self._send_json(status, {"error": error})

    def do_POST(self):
        if self.path != "/api/events":
            self._send_json(404, {"error": "not found"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._reject_unread_body(415, "Content-Type must be application/json")
            return
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            length = -1
        if length <= 0:
            self._reject_unread_body(400, "missing or invalid Content-Length")
            return
        if length > MAX_BODY_BYTES:
            self._reject_unread_body(413, f"request body exceeds {MAX_BODY_BYTES} bytes")
            return
        try:
            event = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(event, dict):
                raise ValueError("event body must be a JSON object")
            record = self.store.upsert(event)
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {"error": str(e)})
            return
        self._send_json(200, {"ok": True, "session": record})

    def do_DELETE(self):
        match = SESSION_PATH_RE.match(self.path)
        if not match:
            self._send_json(404, {"error": "not found"})
            return
        if self.store.delete(urllib.parse.unquote(match.group(1))):
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "unknown session"})


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Attention Hub</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; background: #14171c; color: #e6e8eb;
         max-width: 64rem; margin: 1.5rem auto; padding: 0 1rem; }
  h1 { font-size: 1.2rem; font-weight: 600; }
  h1 small { color: #8b939e; font-weight: 400; margin-left: .6rem; }
  #sessions { display: flex; flex-direction: column; gap: .5rem; margin-top: 1rem; }
  .row { display: flex; align-items: center; gap: .9rem; padding: .7rem .9rem;
         background: #1d2128; border-radius: 8px; border-left: 6px solid #555; }
  .row.red { border-left-color: #e5534b; }
  .row.yellow { border-left-color: #d4a72c; }
  .row.green { border-left-color: #46954a; }
  .who { min-width: 16rem; }
  .project { font-weight: 600; }
  .host { color: #8b939e; font-size: .85rem; }
  .state { min-width: 10rem; font-size: .9rem; }
  .red .state { color: #e5534b; }
  .yellow .state { color: #d4a72c; }
  .green .state { color: #46954a; }
  .snippet { flex: 1; color: #b4bac2; font-size: .85rem; overflow: hidden;
             text-overflow: ellipsis; white-space: nowrap; }
  .age { color: #8b939e; font-size: .8rem; white-space: nowrap; }
  button.dismiss { background: none; border: 1px solid #3a4048; color: #8b939e;
                   border-radius: 6px; padding: .15rem .55rem; cursor: pointer; }
  button.dismiss:hover { color: #e6e8eb; border-color: #8b939e; }
  #empty { color: #8b939e; margin-top: 2rem; }
</style>
</head>
<body>
<h1>Claude Attention Hub <small id="meta"></small></h1>
<div id="sessions"></div>
<p id="empty" hidden>No sessions reporting.</p>
<script>
const STATE_LABEL = {
  waiting: "waiting on you",
  needs_input: "needs your input",
  done: "done, awaiting review",
  working: "working",
};
const STATE_COLOR = { waiting: "red", needs_input: "red", done: "yellow", working: "green" };

function fmtDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  return Math.floor(s / 3600) + "h" + Math.floor((s % 3600) / 60) + "m";
}

function dismiss(sessionId) {
  fetch("/api/sessions/" + encodeURIComponent(sessionId), { method: "DELETE" })
    .then(refresh).catch(() => {});
}

function render(sessions) {
  const container = document.getElementById("sessions");
  container.replaceChildren();
  document.getElementById("empty").hidden = sessions.length > 0;
  document.getElementById("meta").textContent =
    sessions.length + " session" + (sessions.length === 1 ? "" : "s");
  for (const s of sessions) {
    const row = document.createElement("div");
    row.className = "row " + (STATE_COLOR[s.state] || "");

    const who = document.createElement("div");
    who.className = "who";
    const project = document.createElement("div");
    project.className = "project";
    project.textContent = s.project;
    const host = document.createElement("div");
    host.className = "host";
    host.textContent = s.host;
    who.append(project, host);

    const state = document.createElement("div");
    state.className = "state";
    state.textContent = (STATE_LABEL[s.state] || s.state) + " " + fmtDuration(s.state_seconds);

    const snippet = document.createElement("div");
    snippet.className = "snippet";
    if (s.state === "waiting" || s.state === "needs_input" || s.state === "done") {
      snippet.textContent = s.message || "";
      snippet.title = s.message || "";
    }

    const age = document.createElement("div");
    age.className = "age";
    age.textContent = "updated " + fmtDuration(s.age_seconds) + " ago";

    const btn = document.createElement("button");
    btn.className = "dismiss";
    btn.textContent = "dismiss";
    btn.title = "Remove this session from the hub";
    btn.addEventListener("click", () => dismiss(s.session_id));

    row.append(who, state, snippet, age, btn);
    container.append(row);
  }
}

function refresh() {
  fetch("/api/sessions")
    .then((r) => r.json())
    .then((data) => render(data.sessions))
    .catch(() => {});
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


def create_server(bind, port, state_file, prune_hours):
    """Build a ThreadingHTTPServer wired to an AttentionStore."""
    server = ThreadingHTTPServer((bind, port), AttentionHubHandler)
    server.store = AttentionStore(state_file, prune_hours=prune_hours)
    return server


def main():
    parser = argparse.ArgumentParser(description="Claude attention hub")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--bind", default=DEFAULT_BIND,
                        help=f"address to bind (default {DEFAULT_BIND}; use 127.0.0.1 for localhost only)")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE,
                        help=f"JSON state file path (default {DEFAULT_STATE_FILE})")
    parser.add_argument("--prune-hours", type=float, default=DEFAULT_PRUNE_HOURS,
                        help=f"drop sessions silent for this many hours (default {DEFAULT_PRUNE_HOURS:g})")
    args = parser.parse_args()

    server = create_server(args.bind, args.port, args.state_file, args.prune_hours)
    print(f"attention hub listening on http://{args.bind}:{args.port}"
          f" (state: {args.state_file}, prune: {args.prune_hours:g}h)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.server_close()


if __name__ == "__main__":
    main()
