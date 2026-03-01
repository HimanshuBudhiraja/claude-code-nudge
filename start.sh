#!/usr/bin/env bash
# Start (or restart) the Claude Code nudge daemon.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON="$SCRIPT_DIR/daemon.py"
PID_FILE="$SCRIPT_DIR/daemon.pid"
LOG="$SCRIPT_DIR/daemon.log"

# Kill existing daemon if running
if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE")"
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing daemon (PID $OLD_PID)..."
        kill "$OLD_PID"
        sleep 0.5
    fi
    rm -f "$PID_FILE"
fi

# Ensure scripts are executable
chmod +x "$DAEMON"
chmod +x "$SCRIPT_DIR/hooks/ask.sh"
chmod +x "$SCRIPT_DIR/hooks/stop.sh"
chmod +x "$SCRIPT_DIR/hooks/notify.sh"

echo "Starting Claude Code nudge daemon..."

# Launch detached from terminal
nohup /usr/bin/python3 "$DAEMON" >> "$LOG" 2>&1 &

DAEMON_PID=$!
echo "Daemon PID: $DAEMON_PID (log: $LOG)"

# Wait for HTTP server to become ready (up to 5 seconds)
for i in $(seq 1 50); do
    if curl --connect-timeout 0.2 -sf http://localhost:9981/health > /dev/null 2>&1; then
        echo "Daemon is ready at http://localhost:9981"
        exit 0
    fi
    sleep 0.1
done

echo "WARNING: Daemon did not respond within 5 seconds."
echo "Check the log: $LOG"
exit 1
