#!/usr/bin/env bash
# Stop hook — fires when Claude finishes its turn and waits for user input.
# Fire-and-forget: responds instantly, daemon shows widget asynchronously.

set -euo pipefail

DAEMON_URL="http://localhost:9981"
LOG="$HOME/.claude/nudge/stop.log"

INPUT="$(cat)"

# Prevent infinite loop: if a stop hook is already active, bail out
ACTIVE="$(python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
print('true' if data.get('stop_hook_active', False) else 'false')
" <<< "$INPUT" 2>/dev/null || echo 'false')"

if [ "$ACTIVE" = "true" ]; then
    exit 0
fi

# Check daemon liveness
if ! curl --connect-timeout 1 -sf "$DAEMON_URL/health" > /dev/null 2>&1; then
    echo "[$(date)] Daemon not running for stop hook" >> "$LOG"
    exit 0
fi

# Fire-and-forget POST
curl -sf \
    --connect-timeout 2 \
    -m 5 \
    -X POST \
    -H "Content-Type: application/json" \
    -d '{}' \
    "$DAEMON_URL/stop" > /dev/null 2>>"$LOG" || true

exit 0
