# tests/test_hub_client.py
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch, MagicMock


def load_client():
    spec = importlib.util.spec_from_file_location(
        "attention_hub_client",
        Path(__file__).parent.parent / "hooks" / "attention_hub_client.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Payload identity fields ---

def test_payload_contains_identity_fields(monkeypatch):
    # Why: the dashboard can only label a row if every event identifies the session,
    # project, host, state, and time. Guards against silently dropping a field the
    # hub needs to render "which instance is waiting on me".
    monkeypatch.delenv("CLAUDE_HOST_LABEL", raising=False)
    client = load_client()
    payload = client.build_event_payload(
        "sess-1", "/home/user/my-project", "waiting", "Need permission?"
    )
    assert payload["session_id"] == "sess-1"
    assert payload["project"] == "my-project"
    assert payload["state"] == "waiting"
    assert payload["message"] == "Need permission?"
    assert payload["host"]  # non-empty hostname fallback
    assert payload["timestamp"]


def test_payload_host_label_env_override(monkeypatch):
    # Why: docker containers and remote servers need friendly names instead of
    # generated hostnames; CLAUDE_HOST_LABEL must win over the machine hostname.
    monkeypatch.setenv("CLAUDE_HOST_LABEL", "docker-build-box")
    client = load_client()
    payload = client.build_event_payload("s", "/srv/app", "working", None)
    assert payload["host"] == "docker-build-box"


def test_payload_truncates_long_message(monkeypatch):
    # Why: dashboard rows show a snippet, not a full transcript; unbounded messages
    # would bloat hub state and the JSON persistence file.
    monkeypatch.delenv("CLAUDE_HOST_LABEL", raising=False)
    client = load_client()
    payload = client.build_event_payload("s", "/srv/app", "needs_input", "x" * 1000)
    assert len(payload["message"]) <= 200


def test_payload_empty_message_allowed(monkeypatch):
    # Why: working/removed states carry no snippet; payload building must not
    # require one. Guards the None-message path used by UserPromptSubmit.
    monkeypatch.delenv("CLAUDE_HOST_LABEL", raising=False)
    client = load_client()
    payload = client.build_event_payload("s", "/srv/app", "working", None)
    assert payload["message"] == ""


# --- Hub URL resolution ---

def test_hub_url_default(monkeypatch):
    # Why: with no configuration the hooks must target the documented default
    # http://localhost:8765 so local setups work out of the box.
    monkeypatch.delenv("CLAUDE_ATTENTION_HUB_URL", raising=False)
    client = load_client()
    assert client.get_hub_url() == "http://localhost:8765"


def test_hub_url_env_override(monkeypatch):
    # Why: docker containers and remote servers reach the hub via a configurable
    # address; the env var must override the default and tolerate trailing slashes.
    monkeypatch.setenv("CLAUDE_ATTENTION_HUB_URL", "http://10.0.0.5:9999/")
    client = load_client()
    assert client.get_hub_url() == "http://10.0.0.5:9999"


# --- Graceful degradation ---

def test_report_state_unreachable_hub_no_exception(monkeypatch):
    # Why: an unreachable hub must never block or error a Claude session — the
    # core graceful-degradation guarantee of the whole feature.
    monkeypatch.setenv("CLAUDE_ATTENTION_HUB_URL", "http://127.0.0.1:1")
    client = load_client()
    result = client.report_state("s", "/srv/app", "waiting", "msg")
    assert result is False


def test_remove_session_unreachable_hub_no_exception(monkeypatch):
    # Why: SessionEnd must exit cleanly even when the hub is down; a raised
    # exception here would surface as a hook error in Claude Code.
    monkeypatch.setenv("CLAUDE_ATTENTION_HUB_URL", "http://127.0.0.1:1")
    client = load_client()
    result = client.remove_session("s")
    assert result is False


def test_report_state_posts_event_to_hub(monkeypatch):
    # Why: the hub contract is POST {hub}/api/events with the JSON payload; if the
    # path or body encoding drifts, every hook silently stops reporting.
    monkeypatch.setenv("CLAUDE_ATTENTION_HUB_URL", "http://hub.example:8765")
    monkeypatch.delenv("CLAUDE_HOST_LABEL", raising=False)
    client = load_client()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["method"] = req.get_method()
        return MagicMock(status=200)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ok = client.report_state("sess-9", "/work/proj", "waiting", "hello")

    assert ok is True
    assert captured["url"] == "http://hub.example:8765/api/events"
    assert captured["method"] == "POST"
    assert captured["body"]["session_id"] == "sess-9"
    assert captured["body"]["state"] == "waiting"


def test_remove_session_sends_delete(monkeypatch):
    # Why: SessionEnd removal must target DELETE /api/sessions/{id}; a wrong verb
    # or path would leave dead rows on the dashboard forever.
    monkeypatch.setenv("CLAUDE_ATTENTION_HUB_URL", "http://hub.example:8765")
    client = load_client()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return MagicMock(status=200)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ok = client.remove_session("sess-9")

    assert ok is True
    assert captured["url"] == "http://hub.example:8765/api/sessions/sess-9"
    assert captured["method"] == "DELETE"
