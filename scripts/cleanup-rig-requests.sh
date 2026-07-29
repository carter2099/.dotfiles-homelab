#!/usr/bin/env bash
# Remove request files in ~/rig-requests older than 14 days.
# Scheduled via systemd timer (cleanup-rig-requests.timer).
set -euo pipefail
export HOME="/home/carter"

START_TS="$(date +%s)"
COUNT=0

if [ -d "$HOME/rig-requests" ]; then
  while IFS= read -r -d '' f; do
    rm -f "$f"
    COUNT=$((COUNT + 1))
  done < <(find "$HOME/rig-requests" -maxdepth 1 -type f -mtime +14 -print0)
fi

END_TS="$(date +%s)"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) cleanup-rig-requests removed=$COUNT duration=$((END_TS - START_TS))s" >> "$HOME/digests/cleanup-rig-requests/.runs.log"
