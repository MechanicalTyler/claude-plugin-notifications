#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///

import json
import sys
from pathlib import Path

try:
    from attention_hub_client import report_state, log_hub
except ImportError:
    import importlib.util
    hub_spec = importlib.util.spec_from_file_location(
        "attention_hub_client",
        Path(__file__).parent / "attention_hub_client.py"
    )
    attention_hub_client = importlib.util.module_from_spec(hub_spec)
    hub_spec.loader.exec_module(attention_hub_client)
    report_state = attention_hub_client.report_state
    log_hub = attention_hub_client.log_hub


def main():
    # UserPromptSubmit: the user answered this session, so attention is addressed.
    # Report "working" to the attention hub; never block or error the session.
    try:
        input_data = json.load(sys.stdin)
        session_id = input_data.get("session_id", "")
        if not session_id:
            sys.exit(0)

        success = report_state(session_id, input_data.get("cwd", ""), "working")
        log_hub(f"UserPromptSubmit -> working for {session_id}: {'ok' if success else 'hub unreachable'}")
        sys.exit(0)
    except json.JSONDecodeError:
        sys.exit(0)
    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
