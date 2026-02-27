"""
Tests for has_ask_user_question in hooks/macos_notification.py.

Covers:
- Returns True when the latest assistant message contains AskUserQuestion
- Returns False when the latest assistant message does not contain AskUserQuestion
- Regression: Returns False when a *previous* assistant message had AskUserQuestion
  but the *latest* one does not (the loop-break bug)
"""

import importlib.util
from pathlib import Path

# Load macos_notification.py directly so we don't depend on it being a package
_MODULE_PATH = Path(__file__).parent.parent / "hooks" / "macos_notification.py"
_spec = importlib.util.spec_from_file_location("macos_notification", _MODULE_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

has_ask_user_question = _mod.has_ask_user_question


def test_returns_true_when_latest_assistant_has_ask(transcript_with_ask):
    """Latest assistant message contains AskUserQuestion — should return True."""
    assert has_ask_user_question(transcript_with_ask) is True


def test_returns_false_when_latest_assistant_has_no_ask(transcript_without_ask):
    """Latest assistant message has no AskUserQuestion — should return False."""
    assert has_ask_user_question(transcript_without_ask) is False


def test_returns_false_when_only_previous_assistant_had_ask(transcript_ask_then_no_ask):
    """
    Regression test: a prior assistant message used AskUserQuestion, but the most
    recent assistant message did not.  The function must return False.

    Before the fix the loop fell through to the older message and returned True.
    """
    assert has_ask_user_question(transcript_ask_then_no_ask) is False
