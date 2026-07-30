#!/usr/bin/env bash
# Runs on a schedule every 12h.
# Discovers new jobs, scores/tailors/covers them, then drains the apply queue.

set -euo pipefail

APPLYPILOT="$(which applypilot 2>/dev/null || echo "$HOME/.local/bin/applypilot")"
LOG="$HOME/.applypilot/logs/apply_daemon.log"

mkdir -p "$HOME/.applypilot/logs"

echo "" >> "$LOG"
echo "======================================" >> "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') : daemon run start" >> "$LOG"
echo "======================================" >> "$LOG"

# Kill any stale Chrome workers left by a previous killed/crashed run
pkill -f "chrome-workers/worker-" 2>/dev/null || true
sleep 1

# Note: stuck 'in_progress' jobs from a previous crashed run are reset
# automatically by `applypilot apply` itself (launcher.main() calls
# reset_stuck_jobs() on startup) -- no separate step needed here.

# Run full pipeline: discover → enrich → score → tailor → cover → pdf
echo "$(date '+%H:%M:%S') : running pipeline..." >> "$LOG"
"$APPLYPILOT" run >> "$LOG" 2>&1

# Drain the apply queue
echo "$(date '+%H:%M:%S') : running apply..." >> "$LOG"
"$APPLYPILOT" apply --limit 15 --workers 2 --model haiku --headless >> "$LOG" 2>&1

echo "$(date '+%H:%M:%S') : done" >> "$LOG"
