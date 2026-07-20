#!/usr/bin/env bash
# Post-reboot resume for homelab-steward.
# Checks ~/agent-state/pending.md — if it exists and is recent (< 30 min since boot),
# resumes the steward runner. Otherwise, no-op.
set -euo pipefail

export HOME="/home/carter"
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

PENDING="$HOME/agent-state/pending.md"

if [ ! -f "$PENDING" ]; then
    echo "[steward-resume] no pending.md — nothing to resume"
    exit 0
fi

# Check boot recency: if system has been up > 30 min, pending.md is stale
UPTIME_SEC=$(awk '{print int($1)}' /proc/uptime)
if [ "$UPTIME_SEC" -gt 1800 ]; then
    echo "[steward-resume] system up ${UPTIME_SEC}s (>30 min) — pending.md is stale, removing"
    rm -f "$PENDING"
    exit 0
fi

echo "[steward-resume] pending.md found, uptime ${UPTIME_SEC}s — resuming steward"
python3 "$HOME/scripts/steward_runner.py" --resume

# Clean up pending.md after successful resume
rm -f "$PENDING"
echo "[steward-resume] done"
