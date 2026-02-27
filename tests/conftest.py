# tests/conftest.py
import json
import pytest
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def transcript_with_ask(tmp_path):
    """Transcript file where last assistant message contains AskUserQuestion."""
    src = FIXTURES_DIR / "transcript_with_ask.jsonl"
    dest = tmp_path / "transcript_with_ask.jsonl"
    dest.write_text(src.read_text())
    return str(dest)


@pytest.fixture
def transcript_without_ask(tmp_path):
    """Transcript file where last assistant message has no AskUserQuestion."""
    src = FIXTURES_DIR / "transcript_without_ask.jsonl"
    dest = tmp_path / "transcript_without_ask.jsonl"
    dest.write_text(src.read_text())
    return str(dest)


@pytest.fixture
def base_hook_input():
    """Base stdin payload common to all hooks."""
    return {
        "session_id": "test-session-123",
        "transcript_path": "",
        "cwd": "/tmp/test-project",
        "permission_mode": "default",
    }
