#!/bin/bash
# Gaming proxy: auto-pause LLM when Apollo/Sunshine is streaming.
# Run via systemd timer every 10s.

STATE_FILE="$HOME/.cache/llama-gaming-state"

# Check GPU encoder sessions on gaming rig
ENCODER_SESSIONS=$(ssh -o ConnectTimeout=3 -o StrictHostKeyChecking=no gamingrig \
  "nvidia-smi --query-gpu=encoder.stats.sessionCount --format=csv,noheader" 2>/dev/null | tr -d ' \r')

# If SSH fails (gaming rig offline), do nothing — let systemd handle restart attempts
if [ -z "$ENCODER_SESSIONS" ]; then
    exit 0
fi

if [ "$ENCODER_SESSIONS" -gt 0 ] 2>/dev/null; then
    # Gaming active — stop LLM if running
    if systemctl --user is-active llama-server.service --quiet 2>/dev/null; then
        systemctl --user stop llama-server.service
        echo "stopped" > "$STATE_FILE"
        logger -t llama-gaming-proxy "Gaming detected (encoder sessions: $ENCODER_SESSIONS) — stopped LLM"
    fi
else
    # No gaming — resume LLM if we previously stopped it
    if [ -f "$STATE_FILE" ] && grep -q "stopped" "$STATE_FILE"; then
        if ! systemctl --user is-active llama-server.service --quiet 2>/dev/null; then
            systemctl --user start llama-server.service
            logger -t llama-gaming-proxy "Gaming ended — restarted LLM"
        fi
        rm -f "$STATE_FILE"
    fi
fi
