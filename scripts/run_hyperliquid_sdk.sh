#!/usr/bin/env bash
# Runs the Hyperliquid Ruby SDK autonomous maintenance cycle via Pi + Qwen 3.7 Max.
# Scheduled via systemd timer (hyperliquid-sdk.timer) daily at 4am.
# Provider-agnostic: change the --model id to switch providers/models.

set -euo pipefail

export HOME="/home/carter"
export PATH="$HOME/.local/bin:$HOME/.rbenv/bin:$HOME/.rbenv/shims:$HOME/.fnm:$PATH"

PROMPT='Read /home/carter/.pi/agent/skills/hyperliquid-run/SKILL.md using the read tool and follow its instructions exactly. This is an automated scheduled SDK maintenance run.'

pi -p --model opencode-go/glm-5.2 "$PROMPT"
