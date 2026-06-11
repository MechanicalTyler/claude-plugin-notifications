# tests/test_attention_hub.py
import importlib.util
import json
import threading
import urllib.request
from pathlib import Path

import pytest


def load_hub():
    spec = importlib.util.spec_from_file_location(
        "attention_hub",
        Path(__file__).parent.parent / "hub" / "attention_hub.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_event(session_id="sess-1", state="working", project="proj", host="mac",
               message="", timestamp="2026-06-11T00:00:00+00:00"):
    return {
        "session_id": session_id,
        "project": project,
        "host": host,
        "state": state,
        "message": message,
        "timestamp": timestamp,
    }


# --- Store: upsert / list / delete ---

def test_upsert_creates_session(tmp_path):
    # Why: the first event from a new session must create its dashboard row —
    # create-or-update semantics keyed by session ID.
    hub = load_hub()
    store = hub.AttentionStore(str(tmp_path / "state.json"))
    store.upsert(make_event())
    sessions = store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "sess-1"
    assert sessions[0]["state"] == "working"


def test_upsert_updates_existing_session(tmp_path):
    # Why: a state transition must update the existing row, never add a second
    # row for the same session — one discrete row per session is the core UX rule.
    hub = load_hub()
    store = hub.AttentionStore(str(tmp_path / "state.json"))
    store.upsert(make_event(state="working"))
    store.upsert(make_event(state="waiting", message="May I run rm?"))
    sessions = store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["state"] == "waiting"
    assert sessions[0]["message"] == "May I run rm?"


def test_state_duration_resets_on_state_change_only(tmp_path):
    # Why: "waiting 4m" must measure time in the CURRENT state; repeated events in
    # the same state must not reset the clock, and a state change must.
    hub = load_hub()
    clock = {"now": 1000.0}
    store = hub.AttentionStore(str(tmp_path / "state.json"), now=lambda: clock["now"])
    store.upsert(make_event(state="working"))
    clock["now"] = 1060.0
    store.upsert(make_event(state="working"))
    clock["now"] = 1120.0
    working = store.list_sessions()[0]
    assert working["state_seconds"] == pytest.approx(120.0)
    store.upsert(make_event(state="waiting"))
    clock["now"] = 1130.0
    waiting = store.list_sessions()[0]
    assert waiting["state_seconds"] == pytest.approx(10.0)


def test_list_reports_last_update_age(tmp_path):
    # Why: the dashboard shows last-update age so stale/crashed sessions are
    # recognizable; the API must expose it.
    hub = load_hub()
    clock = {"now": 1000.0}
    store = hub.AttentionStore(str(tmp_path / "state.json"), now=lambda: clock["now"])
    store.upsert(make_event())
    clock["now"] = 1030.0
    assert store.list_sessions()[0]["age_seconds"] == pytest.approx(30.0)


def test_list_sorts_needs_attention_first(tmp_path):
    # Why: the dashboard answers "which instance is waiting on me" at a glance —
    # red (waiting/needs_input) must sort before yellow (done) before green (working).
    hub = load_hub()
    store = hub.AttentionStore(str(tmp_path / "state.json"))
    store.upsert(make_event(session_id="green", state="working"))
    store.upsert(make_event(session_id="yellow", state="done"))
    store.upsert(make_event(session_id="red1", state="waiting"))
    store.upsert(make_event(session_id="red2", state="needs_input"))
    states = [s["state"] for s in store.list_sessions()]
    assert states[:2] in ([["waiting", "needs_input"], ["needs_input", "waiting"]])
    assert states[2] == "done"
    assert states[3] == "working"


def test_delete_removes_session(tmp_path):
    # Why: dismissing a crashed/abandoned session must actually drop it from the
    # store, not just hide it client-side.
    hub = load_hub()
    store = hub.AttentionStore(str(tmp_path / "state.json"))
    store.upsert(make_event(session_id="a"))
    store.upsert(make_event(session_id="b"))
    assert store.delete("a") is True
    assert [s["session_id"] for s in store.list_sessions()] == ["b"]
    assert store.delete("missing") is False


# --- Persistence ---

def test_state_survives_restart(tmp_path):
    # Why: the hub is a manual-start script; restarting it must restore the
    # session list from the JSON file (the story's persistence criterion).
    hub = load_hub()
    state_file = str(tmp_path / "state.json")
    store = hub.AttentionStore(state_file)
    store.upsert(make_event(session_id="persisted", state="needs_input", message="Q?"))
    restored = hub.AttentionStore(state_file)
    sessions = restored.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "persisted"
    assert sessions[0]["state"] == "needs_input"
    assert sessions[0]["message"] == "Q?"


def test_legacy_state_record_missing_time_fields_loads(tmp_path):
    # Why: a hand-edited or older-format state file without state_since/last_update
    # must not KeyError every list_sessions call and blank the dashboard forever.
    hub = load_hub()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"sessions": {
        "legacy": {"session_id": "legacy", "state": "working"}
    }}))
    store = hub.AttentionStore(str(state_file))
    sessions = store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "legacy"
    assert sessions[0]["state_seconds"] >= 0


def test_corrupt_state_file_starts_empty(tmp_path):
    # Why: a truncated/corrupt JSON file must not crash the hub on start; it
    # degrades to an empty session list.
    hub = load_hub()
    state_file = tmp_path / "state.json"
    state_file.write_text("{not json")
    store = hub.AttentionStore(str(state_file))
    assert store.list_sessions() == []


# --- Pruning ---

def test_sessions_silent_past_window_are_pruned(tmp_path):
    # Why: sessions that died without a SessionEnd must disappear after the prune
    # window instead of cluttering the dashboard forever.
    hub = load_hub()
    clock = {"now": 0.0}
    store = hub.AttentionStore(str(tmp_path / "state.json"), prune_hours=24,
                               now=lambda: clock["now"])
    store.upsert(make_event(session_id="old"))
    clock["now"] = 25 * 3600.0
    store.upsert(make_event(session_id="fresh"))
    ids = [s["session_id"] for s in store.list_sessions()]
    assert ids == ["fresh"]


def test_prune_window_configurable(tmp_path):
    # Why: the 24h window is configurable; a custom window must be honored.
    hub = load_hub()
    clock = {"now": 0.0}
    store = hub.AttentionStore(str(tmp_path / "state.json"), prune_hours=1,
                               now=lambda: clock["now"])
    store.upsert(make_event(session_id="old"))
    clock["now"] = 2 * 3600.0
    assert store.list_sessions() == []


# --- HTTP API + dashboard ---

@pytest.fixture
def hub_server(tmp_path):
    hub = load_hub()
    server = hub.create_server("127.0.0.1", 0, str(tmp_path / "state.json"), 24)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base
    server.shutdown()
    server.server_close()


def http_json(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read().decode()
        return resp.status, json.loads(raw) if raw else None


def test_http_event_upsert_and_list(hub_server):
    # Why: end-to-end contract the hooks rely on — POST /api/events must surface
    # the session in GET /api/sessions with its state.
    status, _ = http_json(f"{hub_server}/api/events", "POST",
                          make_event(session_id="http-1", state="waiting", message="hi"))
    assert status == 200
    status, listing = http_json(f"{hub_server}/api/sessions")
    assert status == 200
    assert len(listing["sessions"]) == 1
    row = listing["sessions"][0]
    assert row["session_id"] == "http-1"
    assert row["state"] == "waiting"
    assert "state_seconds" in row and "age_seconds" in row


def test_http_delete_session(hub_server):
    # Why: the dashboard dismiss control calls DELETE /api/sessions/{id}; it must
    # remove the row server-side.
    http_json(f"{hub_server}/api/events", "POST", make_event(session_id="gone"))
    status, _ = http_json(f"{hub_server}/api/sessions/gone", "DELETE")
    assert status == 200
    _, listing = http_json(f"{hub_server}/api/sessions")
    assert listing["sessions"] == []


def test_http_delete_unknown_session_404(hub_server):
    # Why: deleting an unknown ID must be a clean 404, not a server error.
    req = urllib.request.Request(f"{hub_server}/api/sessions/nope", method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=5)
        status = 200
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 404


def test_http_invalid_event_rejected(hub_server):
    # Why: an event without a session_id cannot key a row; the hub must reject it
    # with 400 instead of storing garbage.
    try:
        status, _ = http_json(f"{hub_server}/api/events", "POST", {"state": "working"})
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 400
    _, listing = http_json(f"{hub_server}/api/sessions")
    assert listing["sessions"] == []


def test_http_non_object_event_rejected_with_400(hub_server):
    # Why: a JSON array/string body must produce a clean 400, not an uncaught
    # AttributeError that kills the handler thread with no response.
    req = urllib.request.Request(
        f"{hub_server}/api/events", data=b'["not", "an", "object"]', method="POST",
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
        status = 200
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 400


def test_http_delete_percent_encoded_session_id(hub_server):
    # Why: the hook client percent-encodes session IDs; the hub must decode the
    # path or IDs with reserved characters could never be removed.
    http_json(f"{hub_server}/api/events", "POST", make_event(session_id="odd id/1"))
    status, _ = http_json(f"{hub_server}/api/sessions/odd%20id%2F1", "DELETE")
    assert status == 200
    _, listing = http_json(f"{hub_server}/api/sessions")
    assert listing["sessions"] == []


def test_dashboard_served_at_root(hub_server):
    # Why: the dashboard is the feature's only indicator surface; the root URL
    # must serve a self-contained HTML page that polls the sessions API.
    with urllib.request.urlopen(f"{hub_server}/", timeout=5) as resp:
        assert resp.status == 200
        assert "text/html" in resp.headers.get("Content-Type", "")
        page = resp.read().decode()
    assert "/api/sessions" in page, "dashboard must poll the session-list endpoint"
    assert "dismiss" in page.lower(), "dashboard must expose a per-row dismiss control"


def test_dashboard_data_one_row_per_session(hub_server):
    # Why: 5 sessions = 5 rows, never aggregated — verified at the data level the
    # page renders from (per spec, no browser automation).
    for i in range(5):
        http_json(f"{hub_server}/api/events", "POST",
                  make_event(session_id=f"s{i}", project=f"proj-{i}"))
    _, listing = http_json(f"{hub_server}/api/sessions")
    ids = sorted(s["session_id"] for s in listing["sessions"])
    assert ids == ["s0", "s1", "s2", "s3", "s4"]
