#!/usr/bin/env bash
# PreToolUse hook for AskUserQuestion
# Reads tool JSON from stdin, sends to nudge daemon, outputs block decision.

set -euo pipefail

DAEMON_URL="http://localhost:9981"
LOG="$HOME/.claude/nudge/ask.log"

INPUT="$(cat)"

# Extract question and flat list of option labels via python3
PAYLOAD="$(python3 - <<'PYEOF'
import json, sys

data = json.loads(sys.stdin.read())
tool_input = data.get("tool_input", {})

# AskUserQuestion tool_input has a "questions" array
questions = tool_input.get("questions", [])
if questions:
    q = questions[0]
    question = q.get("question", "Claude is asking a question.")
    raw_opts  = q.get("options", [])
    # Options may be strings or dicts with a "label" key
    options = []
    for o in raw_opts:
        if isinstance(o, dict):
            options.append(o.get("label", str(o)))
        else:
            options.append(str(o))
else:
    question = tool_input.get("question", "Claude is asking a question.")
    options  = tool_input.get("options", ["OK", "Cancel"])

print(json.dumps({"question": question, "options": options}))
PYEOF
)" <<< "$INPUT"

if [ $? -ne 0 ] || [ -z "$PAYLOAD" ]; then
    echo "[$(date)] Failed to parse tool input" >> "$LOG"
    exit 0
fi

# Check daemon liveness (fast probe)
if ! curl --connect-timeout 1 -sf "$DAEMON_URL/health" > /dev/null 2>&1; then
    echo "[$(date)] Daemon not running, falling through to VS Code" >> "$LOG"
    exit 0
fi

# POST to /ask — blocks until user clicks (up to 125s)
RESPONSE="$(curl -sf \
    --connect-timeout 2 \
    -m 125 \
    -X POST \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$DAEMON_URL/ask" 2>>"$LOG")"

CURL_EXIT=$?

if [ $CURL_EXIT -ne 0 ] || [ -z "$RESPONSE" ]; then
    echo "[$(date)] curl failed (exit $CURL_EXIT), falling through" >> "$LOG"
    exit 0
fi

# Output block decision — Claude Code reads this on stdout
printf '%s' "$RESPONSE"
