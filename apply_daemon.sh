#!/usr/bin/env bash
# Runs on a schedule. Resets stuck in_progress jobs, drains the apply queue.

DB="$HOME/.applypilot/applypilot.db"
APPLYPILOT="$(which applypilot 2>/dev/null || echo "$HOME/.local/bin/applypilot")"

# Kill any stale Chrome workers left by a previous killed/crashed run
pkill -f "chrome-workers/worker-" 2>/dev/null || true
sleep 1

python3 -c "
import sqlite3
conn = sqlite3.connect('$DB')
reset = conn.execute(\"UPDATE jobs SET apply_status = NULL WHERE apply_status = 'in_progress'\").rowcount
conn.commit()
if reset: print(f'Reset {reset} stuck in_progress jobs')
"

$APPLYPILOT apply --limit 15 --workers 2 --model haiku --headless --max-turns 30
