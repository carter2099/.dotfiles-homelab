#!/usr/bin/env bash
# Runs the Hyperliquid Ruby SDK autonomous maintenance cycle via opencode + Qwen 3.7 Max.
# Scheduled via systemd timer (hyperliquid-sdk.timer) daily at 4am.
# Provider-agnostic: change the -m model id to switch providers/models.

set -euo pipefail

export HOME="/home/carter"
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$HOME/.rbenv/bin:$HOME/.rbenv/shims:$HOME/.fnm:$PATH"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"

PROMPT='Read /home/carter/.claude/skills/hyperliquid-run/SKILL.md using the Read tool and follow its instructions exactly. This is an automated scheduled SDK maintenance run.'

# --dir $HOME so the agent can write across ~/dev/hyperliquid, ~/agent-state, and
# ~/.config — opencode auto-rejects writes outside the working directory in headless
# mode, and stdin must be closed (< /dev/null) or the process hangs after finishing.
exec "$HOME/.opencode/bin/opencode" run -m opencode-go/qwen3.7-max --dir "$HOME" "$PROMPT" < /dev/null
