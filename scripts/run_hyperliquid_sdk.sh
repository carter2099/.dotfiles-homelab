#!/usr/bin/env bash
# Spawns a headless Claude agent to run the Hyperliquid Ruby SDK maintenance cycle.
# Scheduled via systemd timer (hyperliquid-sdk.timer) every 6 hours.
# Model + effortLevel come from ~/.claude/settings.json (claude-opus-4-7, xhigh).

set -euo pipefail

export HOME="/home/carter"
export PATH="$HOME/.local/bin:$HOME/.rbenv/bin:$HOME/.rbenv/shims:$HOME/.fnm:$PATH"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"

PROMPT='Read /home/carter/.claude/skills/hyperliquid-run/SKILL.md using the Read tool and follow its instructions exactly. This is an automated scheduled SDK maintenance run.'

claude -p \
  --dangerously-skip-permissions \
  --allowedTools "WebSearch WebFetch Bash Read Write Edit Glob Grep" \
  --no-session-persistence \
  --output-format stream-json \
  --include-partial-messages \
  --verbose \
  "$PROMPT" | python3 -u -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
        t = d.get('type')
        if t == 'assistant':
            for block in d.get('message', {}).get('content', []):
                if block.get('type') == 'text' and block.get('text'):
                    print(block['text'], end='', flush=True)
                elif block.get('type') == 'tool_use':
                    inp = json.dumps(block.get('input', {}))[:120]
                    print(f'\n[tool: {block[\"name\"]} {inp}]', flush=True)
        elif t == 'result':
            cost = d.get('total_cost_usd', '?')
            print(f'\n[DONE subtype={d.get(\"subtype\",\"\")} cost=\${cost}]', flush=True)
        elif t == 'rate_limit_event':
            info = d.get('rate_limit_info', {})
            print(f'\n[RATE_LIMIT status={info.get(\"status\")} type={info.get(\"rateLimitType\")} overage={info.get(\"overageStatus\")}]', flush=True)
    except Exception:
        pass
"
