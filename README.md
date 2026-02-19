# notifications

System notifications when Claude completes tasks or needs input.

## What it does

- **Notification hook**: Sends message to Slack app + macOS notification when Claude needs user input (AskUserQuestion detected)
- **Stop hook**: Sends message to Slack app + macOS "Task Complete" notification when Claude finishes
- **SubagentStop hook**: Sends message to Slack app when a subagent finishes (no macOS notification for subtasks)

## Requirements

- macOS: `brew install terminal-notifier`
- Slack app: must be running at `http://localhost:8080` (optional, gracefully degrades)

## Installation

Add to `~/.claude/settings.json`:

```json
{
  "enabledPlugins": {
    "notifications@local": { "path": "/path/to/notifications" }
  }
}
```

## License

MIT
