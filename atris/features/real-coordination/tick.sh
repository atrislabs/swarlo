#!/bin/bash
# One Real Coordination heartbeat. Skips if a run is still going. Budget caps the spend.
set -u
ROOT="$HOME/arena/empire/swarlo"
CLI="$HOME/arena/empire/atris-cli-campaign/bin/atris.js"
LOG="$ROOT/atris/features/real-coordination/run.log"
LOCK="/tmp/real-coordination.lock"
# pytest lives in the backend venv, not the system python
export PATH="$HOME/arena/empire/atrisos-backend/venv/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
mkdir "$LOCK" 2>/dev/null || { echo "$(date '+%F %T') skip: a run is still going" >> "$LOG"; exit 0; }
trap 'rmdir "$LOCK"' EXIT
cd "$ROOT" || exit 1
echo "$(date '+%F %T') heartbeat" >> "$LOG"
node "$CLI" campaign run cmp-real-coordination --parallel 2 --max-steps 3 --max-ticks 3 >> "$LOG" 2>&1
