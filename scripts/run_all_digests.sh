#!/usr/bin/env bash
# Run all 5 daily digests sequentially via the deterministic workflow runner.
# Scheduled by systemd timer (digests-daily.timer) in the 4am-8am ET window.
#
# Each digest runs the full 9-phase pipeline. They must be sequential because
# the local llama.cpp backend is single-request. Total: ~3-3.5 hours.

set -euo pipefail

# omp binary (bun) is at ~/.bun/bin/ — not in systemd default PATH
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
export HOME="/home/carter"
LOGFILE="$HOME/digests/.digests.log"

# ── Signal trap: log cleanly on termination ──
_cleanup() {
    local rc=$?
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) SCRIPT TERMINATED (exit=$rc) — incomplete run" | tee -a "$LOGFILE" 2>/dev/null || true
    exit $rc
}
trap _cleanup TERM INT HUP

# ── Incomplete-run detection ──
# Check if the previous run was interrupted (no "ALL DONE" at end of log)
if [ -f "$LOGFILE" ]; then
    LAST_LINE=$(tail -1 "$LOGFILE" 2>/dev/null || true)
    if [ -n "$LAST_LINE" ] && echo "$LAST_LINE" | grep -qv "ALL DONE"; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARNING Previous run incomplete (last: $LAST_LINE)"
        # If world was the last started topic and never finished, flag it
        if echo "$LAST_LINE" | grep -q "START world"; then
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARNING world-digest was interrupted — topics before it completed"
        fi
    fi
fi

TOPICS=("ai-tech" "agentic-platform" "ai-hardware" "gaming" "world")

for topic in "${TOPICS[@]}"; do
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) START $topic" | tee -a "$LOGFILE"
    START_TS=$(date +%s)

    if python3 "$HOME/scripts/digest_runner.py" "$topic"; then
        END_TS=$(date +%s)
        DURATION=$((END_TS - START_TS))
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) DONE  $topic duration=${DURATION}s" | tee -a "$LOGFILE"
    else
        RC=$?
        END_TS=$(date +%s)
        DURATION=$((END_TS - START_TS))
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) FAIL  $topic (exit=$RC) duration=${DURATION}s — continuing" | tee -a "$LOGFILE"
    fi
done

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ALL DONE" | tee -a "$LOGFILE"
