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

# ── Signal trap: log cleanly on termination with current topic ──
_CURRENT_TOPIC_FILE="$HOME/digests/.current-topic"
_cleanup() {
    local rc=$?
    local topic="(unknown)"
    if [ -f "$_CURRENT_TOPIC_FILE" ]; then
        topic=$(cat "$_CURRENT_TOPIC_FILE" 2>/dev/null || echo "(unknown)")
        rm -f "$_CURRENT_TOPIC_FILE"
    fi
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) SCRIPT TERMINATED (exit=$rc) topic=$topic — incomplete run" | tee -a "$LOGFILE" 2>/dev/null || true
    exit $rc
}
trap _cleanup TERM INT HUP

# ── Incomplete-run detection ──
# Check if the previous run was interrupted (no "ALL DONE" at end of log)
if [ -f "$LOGFILE" ]; then
    LAST_LINE=$(tail -1 "$LOGFILE" 2>/dev/null || true)
    if [ -n "$LAST_LINE" ] && echo "$LAST_LINE" | grep -qv "ALL DONE"; then
        # Extract topic from SCRIPT TERMINATED line if available
        local topic_hint=""
        if echo "$LAST_LINE" | grep -q "topic="; then
            topic_hint=$(echo "$LAST_LINE" | sed 's/.*topic=\([^ ]*\).*/\1/')
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARNING Previous run interrupted during topic=$topic_hint (last: $LAST_LINE)" | tee -a "$LOGFILE"
        else
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARNING Previous run incomplete (last: $LAST_LINE)" | tee -a "$LOGFILE"
        fi
    fi
fi

TOPICS=("ai-tech" "agentic-platform" "ai-hardware" "gaming" "world")

for topic in "${TOPICS[@]}"; do
    echo "$topic" > "$_CURRENT_TOPIC_FILE"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) START $topic" | tee -a "$LOGFILE"
    START_TS=$(date +%s)

    if timeout 14400 python3 "$HOME/scripts/digest_runner.py" "$topic"; then
        END_TS=$(date +%s)
        DURATION=$((END_TS - START_TS))
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) DONE  $topic duration=${DURATION}s" | tee -a "$LOGFILE" || true
    else
        RC=$?
        END_TS=$(date +%s)
        DURATION=$((END_TS - START_TS))
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) FAIL  $topic (exit=$RC) duration=${DURATION}s — continuing" | tee -a "$LOGFILE" || true
    fi
    rm -f "$_CURRENT_TOPIC_FILE"
done

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ALL DONE" | tee -a "$LOGFILE" || true
