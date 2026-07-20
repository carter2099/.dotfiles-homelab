#!/usr/bin/env bash
# Run all 5 daily digests sequentially via the deterministic workflow runner.
# Scheduled by systemd timer (digests-daily.timer) in the 4am-8am ET window.
#
# Each digest runs the full 9-phase pipeline. They must be sequential because
# the local llama.cpp backend is single-request. Total: ~3-3.5 hours.

set -euo pipefail

export HOME="/home/carter"
LOGFILE="$HOME/digests/.digests.log"

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
