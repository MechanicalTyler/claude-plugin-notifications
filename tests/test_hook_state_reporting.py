# tests/test_hook_state_reporting.py
import importlib.util
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

HOOKS_DIR = Path(__file__).parent.parent / "hooks"


def run_hook_capture_hub(script_name, hook_input):
    """Run a hook script, return list of (url, method, body) hub requests."""
    spec = importlib.util.spec_from_file_location(
        script_name.replace(".py", ""), HOOKS_DIR / script_name
    )
    hub_requests = []

    def capture_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        hub_requests.append((req.full_url, req.get_method(), body))
        return MagicMock(status=200)

    with patch("sys.stdin", StringIO(json.dumps(hook_input))), \
         patch("requests.post") as mock_post, \
         patch("subprocess.run") as mock_subprocess, \
         patch("urllib.request.urlopen", side_effect=capture_urlopen):
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        mock_subprocess.return_value = MagicMock(returncode=0, stderr="")
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            mod.main()
        except SystemExit:
            pass

    return hub_requests


def hub_events(hub_requests):
    return [body for url, method, body in hub_requests
            if url.endswith("/api/events") and method == "POST"]


# --- Notification hook -> waiting ---

def test_actionable_notification_reports_waiting(base_hook_input, transcript_without_ask):
    # Why: a permission prompt means Claude is blocked on the user; the hub must
    # learn "waiting" or the dashboard never turns red for blocked sessions.
    events = hub_events(run_hook_capture_hub("notifications_notification.py", {
        **base_hook_input,
        "notification_type": "permission_prompt",
        "transcript_path": transcript_without_ask,
    }))
    assert len(events) == 1
    assert events[0]["state"] == "waiting"
    assert events[0]["session_id"] == "test-session-123"


def test_non_actionable_notification_reports_nothing(base_hook_input, transcript_without_ask):
    # Why: non-actionable types (auth_success etc.) must not pollute the dashboard
    # with phantom "waiting" rows — the actionable filter gates hub reporting too.
    events = hub_events(run_hook_capture_hub("notifications_notification.py", {
        **base_hook_input,
        "notification_type": "auth_success",
        "transcript_path": transcript_without_ask,
    }))
    assert events == []


# --- Stop hook -> needs_input / done ---

def test_stop_with_ask_reports_needs_input(base_hook_input, transcript_with_ask):
    # Why: a Stop ending in AskUserQuestion means Claude asked the user something;
    # the dashboard row must go red (needs_input), not yellow.
    events = hub_events(run_hook_capture_hub("notifications_stop.py", {
        **base_hook_input,
        "transcript_path": transcript_with_ask,
    }))
    assert len(events) == 1
    assert events[0]["state"] == "needs_input"


def test_stop_without_ask_reports_done(base_hook_input, transcript_without_ask):
    # Why: a Stop with no pending question is "task complete, awaiting review" —
    # the yellow state that distinguishes finished sessions from blocked ones.
    events = hub_events(run_hook_capture_hub("notifications_stop.py", {
        **base_hook_input,
        "transcript_path": transcript_without_ask,
    }))
    assert len(events) == 1
    assert events[0]["state"] == "done"


def test_subagent_stop_reports_nothing(base_hook_input, transcript_without_ask):
    # Why: subagent sessions are internal; showing them as dashboard rows would
    # recreate the notification spam this feature exists to remove.
    events = hub_events(run_hook_capture_hub("notifications_stop.py", {
        **base_hook_input,
        "transcript_path": transcript_without_ask,
        "agent_type": "Explore",
    }))
    assert events == []


def test_stop_reports_message_snippet(base_hook_input, transcript_without_ask):
    # Why: red/yellow rows must show WHAT the session is asking/finished with, so
    # the user can triage from the dashboard without opening the terminal.
    events = hub_events(run_hook_capture_hub("notifications_stop.py", {
        **base_hook_input,
        "transcript_path": transcript_without_ask,
    }))
    assert events[0]["message"], "Stop event must carry the latest message snippet"


# --- UserPromptSubmit hook -> working ---

def test_user_prompt_submit_reports_working(base_hook_input):
    # Why: the user answering a session is the "addressed" signal — it must flip
    # the row green automatically with no manual bookkeeping.
    events = hub_events(run_hook_capture_hub("notifications_user_prompt_submit.py", {
        **base_hook_input,
        "prompt": "please continue",
    }))
    assert len(events) == 1
    assert events[0]["state"] == "working"
    assert events[0]["session_id"] == "test-session-123"


def test_user_prompt_submit_without_session_id_reports_nothing(base_hook_input):
    # Why: without a session ID the hub cannot key the row; sending would create
    # a garbage entry. The hook must stay silent and exit 0.
    hook_input = {**base_hook_input, "prompt": "hi"}
    hook_input["session_id"] = ""
    events = hub_events(run_hook_capture_hub("notifications_user_prompt_submit.py", hook_input))
    assert events == []


# --- Session name propagation ---

def named_transcript(tmp_path, base_lines_path):
    """Copy a fixture transcript and append a /rename custom-title record."""
    base = Path(base_lines_path).read_text(encoding="utf-8")
    path = tmp_path / "named_transcript.jsonl"
    path.write_text(
        base + json.dumps({"type": "custom-title", "customTitle": "tester",
                           "sessionId": "test-session-123"}) + "\n",
        encoding="utf-8",
    )
    return str(path)


def test_stop_reports_session_name_from_transcript(base_hook_input, transcript_without_ask, tmp_path):
    # Why: a renamed session must reach the hub with its name on the Stop event —
    # the moment the row turns yellow/red is when the label matters most.
    events = hub_events(run_hook_capture_hub("notifications_stop.py", {
        **base_hook_input,
        "transcript_path": named_transcript(tmp_path, transcript_without_ask),
    }))
    assert len(events) == 1
    assert events[0]["session_name"] == "tester"


def test_user_prompt_submit_reports_session_name(base_hook_input, transcript_without_ask, tmp_path):
    # Why: UserPromptSubmit fires on every prompt, so it is the event that picks up
    # a fresh /rename fastest; it must carry the name, not just flip state to green.
    events = hub_events(run_hook_capture_hub("notifications_user_prompt_submit.py", {
        **base_hook_input,
        "prompt": "please continue",
        "transcript_path": named_transcript(tmp_path, transcript_without_ask),
    }))
    assert len(events) == 1
    assert events[0]["session_name"] == "tester"


def test_notification_reports_session_name(base_hook_input, transcript_without_ask, tmp_path):
    # Why: blocked (red) rows are the ones the user triages first; the waiting
    # event from the Notification hook must be name-labeled like the others.
    events = hub_events(run_hook_capture_hub("notifications_notification.py", {
        **base_hook_input,
        "notification_type": "permission_prompt",
        "transcript_path": named_transcript(tmp_path, transcript_without_ask),
    }))
    assert len(events) == 1
    assert events[0]["session_name"] == "tester"


def test_unnamed_session_reports_empty_name(base_hook_input, transcript_without_ask):
    # Why: sessions without a /rename must send an empty name so the dashboard
    # falls back to the project label — not omit the field or send garbage.
    events = hub_events(run_hook_capture_hub("notifications_stop.py", {
        **base_hook_input,
        "transcript_path": transcript_without_ask,
    }))
    assert events[0]["session_name"] == ""


# --- SessionEnd hook -> removal ---

def test_session_end_removes_session(base_hook_input):
    # Why: ended sessions must leave the dashboard, otherwise rows accumulate and
    # the "which instance needs me" answer drowns in dead entries.
    hub_requests = run_hook_capture_hub("notifications_session_end.py", {
        **base_hook_input,
        "reason": "exit",
    })
    deletes = [(url, method) for url, method, body in hub_requests if method == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0][0].endswith("/api/sessions/test-session-123")


def test_hooks_exit_zero_when_hub_down(base_hook_input, transcript_without_ask, monkeypatch):
    # Why: the graceful-degradation acceptance criterion — a dead hub must never
    # error any hook. Uses a real connection-refused, not a mock.
    monkeypatch.setenv("CLAUDE_ATTENTION_HUB_URL", "http://127.0.0.1:1")
    for script, extra in [
        ("notifications_user_prompt_submit.py", {"prompt": "hi"}),
        ("notifications_session_end.py", {"reason": "exit"}),
        ("notifications_notification.py",
         {"notification_type": "permission_prompt", "transcript_path": transcript_without_ask}),
        ("notifications_stop.py", {"transcript_path": transcript_without_ask}),
    ]:
        spec = importlib.util.spec_from_file_location(
            script.replace(".py", ""), HOOKS_DIR / script
        )
        with patch("sys.stdin", StringIO(json.dumps({**base_hook_input, **extra}))), \
             patch("requests.post") as mock_post, \
             patch("subprocess.run") as mock_subprocess:
            mock_post.return_value = MagicMock(status_code=200, text="ok")
            mock_subprocess.return_value = MagicMock(returncode=0, stderr="")
            mod = importlib.util.module_from_spec(spec)
            exit_code = 0
            try:
                spec.loader.exec_module(mod)
                mod.main()
            except SystemExit as e:
                exit_code = e.code or 0
            assert exit_code == 0, f"{script} must exit 0 when hub is unreachable"
