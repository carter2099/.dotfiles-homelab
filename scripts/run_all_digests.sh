#!/usr/bin/env bash
# Run all 4 daily digests sequentially via the deterministic workflow runner.
# Scheduled by systemd timer (digests-daily.timer) in the 4am-7am ET window.
#
# Each digest runs the full 9-phase pipeline. They must be sequential because
# the local llama.cpp backend is single-request. Total: ~2.5-3 hours.

set -euo pipefail

export HOME="/home/carter"
LOGFILE="$HOME/digests/.digests.log"

TOPICS=("ai-tech" "agentic-platform" "gaming" "world")

for topic in "${TOPICS[@]}"; do
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) START $topic" | tee -a "$LOGFILE"
    START_TS=$(date +%s)

    python3 "$HOME/scripts/digest_runner.py" "$topic"

    END_TS=$(date +%s)
    DURATION=$((END_TS - START_TS))
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) DONE  $topic duration=${DURATION}s" | tee -a "$LOGFILE"
done

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ALL DONE" | tee -a "$LOGFILE"
