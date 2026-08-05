#!/bin/bash
# Runs the Hyperliquid Ruby SDK autonomous maintenance cycle via omp + opencode-go/glm-5.2.
# Scheduled via systemd timer (hyperliquid-sdk.timer) Mon/Thu at 4am ET.
# Provider-agnostic: change the --model id to switch providers/models.

set -euo pipefail

export HOME="/home/carter"
export PATH="$HOME/.local/bin:$HOME/.rbenv/bin:$HOME/.rbenv/shims:$HOME/.fnm:$HOME/.bun/bin:$PATH"

# Sanity check: verify omp and its bun interpreter are reachable
# (exit 127 on omp means PATH issue at execution time)
if ! command -v omp &>/dev/null; then
    echo "FATAL: omp not found in PATH=$PATH" >&2
    exit 1
fi
if ! command -v bun &>/dev/null; then
    echo "FATAL: bun (omp interpreter) not found in PATH=$PATH" >&2
    exit 1
fi
echo "ok: omp at $(command -v omp), bun at $(command -v bun)"

PROMPT='Read /home/carter/.omp/agent/skills/hyperliquid-run/SKILL.md using the read tool and follow its instructions exactly. This is an automated scheduled SDK maintenance run.'

omp -p --model opencode-go/glm-5.2 --api-key proxy --allow-home --config ~/.omp/agent/headless-override.yml --session-dir ~/.omp/agent/sessions-automated "$PROMPT"
