#!/usr/bin/env bash
# Notification hook — fires when Claude sends a notification event.

set -euo pipefail

DAEMON_URL="http://localhost:9981"
LOG="$HOME/.claude/nudge/notify.log"

INPUT="$(cat)"

# Extract message
PAYLOAD="$(python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
msg = data.get('message', 'Claude notification')
print(json.dumps({'message': msg}))
" <<< "$INPUT" 2>/dev/null || echo '{"message":"Claude notification"}')"

# Check daemon liveness
if ! curl --connect-timeout 1 -sf "$DAEMON_URL/health" > /dev/null 2>&1; then
    echo "[$(date)] Daemon not running for notify hook" >> "$LOG"
    exit 0
fi

curl -sf \
    --connect-timeout 2 \
    -m 5 \
    -X POST \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$DAEMON_URL/notify" > /dev/null 2>>"$LOG" || true

exit 0
