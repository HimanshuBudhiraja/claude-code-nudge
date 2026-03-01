# claude-code-nudge

A persistent macOS floating widget for [Claude Code](https://claude.ai/claude-code) that pops up whenever Claude needs your input — so you never have to switch to VS Code.

## What it does

| Event | Behavior |
|-------|----------|
| `AskUserQuestion` | Widget appears with the question and clickable option buttons. Your selection is sent back to Claude — VS Code popup is suppressed entirely. |
| `Stop` (Claude finished) | macOS Glass notification sound + widget with a text input. Type your reply and hit **Send ↵** — text is injected into VS Code via AppleScript. |
| `Notification` | Brief banner in the corner, auto-dismisses after 4 seconds. |

The widget is **hidden by default** and only appears when Claude needs something from you.

## Requirements

- macOS
- Python 3 (pre-installed on macOS at `/usr/bin/python3`)
- [Claude Code](https://claude.ai/claude-code) with VS Code extension

No `pip install` needed — only Python stdlib + tkinter (bundled with macOS Python).

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/HimanshuBudhiraja/claude-code-nudge.git ~/.claude/nudge
```

### 2. Make scripts executable

```bash
chmod +x ~/.claude/nudge/daemon.py
chmod +x ~/.claude/nudge/start.sh
chmod +x ~/.claude/nudge/hooks/*.sh
```

### 3. Configure Claude Code hooks

Add this to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "AskUserQuestion",
        "hooks": [{"type": "command", "command": "~/.claude/nudge/hooks/ask.sh"}]
      }
    ],
    "Stop": [
      {
        "hooks": [{"type": "command", "command": "~/.claude/nudge/hooks/stop.sh"}]
      }
    ],
    "Notification": [
      {
        "hooks": [{"type": "command", "command": "~/.claude/nudge/hooks/notify.sh"}]
      }
    ]
  }
}
```

### 4. Install the LaunchAgent (auto-start on login)

```bash
cp ~/.claude/nudge/plist/com.user.claudenudge.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.claudenudge.plist
```

### 5. Verify it's running

```bash
curl http://localhost:9981/health
# → {"status": "running"}
```

### 6. Grant Accessibility permission (for Stop mode text injection)

Go to **System Settings → Privacy & Security → Accessibility** and add Python to the allowed list. macOS will also prompt you automatically the first time.

## Manual start / restart

```bash
~/.claude/nudge/start.sh
```

## Stop the daemon

```bash
kill "$(cat ~/.claude/nudge/daemon.pid)"
```

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.user.claudenudge.plist
rm ~/Library/LaunchAgents/com.user.claudenudge.plist
rm -rf ~/.claude/nudge
```

And remove the `hooks` block from `~/.claude/settings.json`.

## Architecture

```
Claude Code hook fires
        │
        ▼
  hook script (.sh)
        │
        │  curl POST (blocking for /ask, fire-and-forget for /stop & /notify)
        ▼
  daemon.py HTTP server (localhost:9981)
        │
        │  thread-safe queue + tkinter after()
        ▼
  tkinter floating window (always-on-top, bottom-right corner)
        │
  user clicks / types
        │
        ▼
  response → HTTP reply → hook stdout → Claude Code
```

## Files

```
~/.claude/nudge/
├── daemon.py              # Core: tkinter GUI + HTTP server + AppleScript injection
├── start.sh               # Manual start / restart
├── hooks/
│   ├── ask.sh             # PreToolUse hook for AskUserQuestion (blocking)
│   ├── stop.sh            # Stop hook (fire-and-forget)
│   └── notify.sh          # Notification hook (fire-and-forget)
└── plist/
    └── com.user.claudenudge.plist  # LaunchAgent for auto-start on login
```

## Logs

```bash
tail -f ~/.claude/nudge/daemon.log
```
