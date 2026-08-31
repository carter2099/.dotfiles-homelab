#!/bin/bash
# Runs the Hyperliquid Ruby SDK autonomous maintenance cycle via omp + opencode-go/glm-5.2.
# Scheduled via systemd timer (hyperliquid-sdk.timer) Mon/Thu at 4am ET.
# Provider-agnostic: change the --model id to switch providers/models.
#
# Failure alerting: the omp run emails Carter on success itself; on any
# non-zero exit from this wrapper we send a failure notice (journal excerpt +
# captured output) so a missed cycle never goes silent. (agent-fleet-review)

set -euo pipefail

export HOME="/home/carter"
export PATH="$HOME/.local/bin:$HOME/.rbenv/bin:$HOME/.rbenv/shims:$HOME/.fnm:$HOME/.bun/bin:$PATH"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
# Timer/manual invocations share one maintenance workspace. Exit cleanly when
# another run owns the lock rather than racing Git state and duplicate emails.
exec 9>/tmp/hyperliquid-sdk.lock
if ! flock -n 9; then
    echo "skip: another Hyperliquid SDK maintenance run is active"
    exit 0
fi

RECIPIENT="carter2099@pm.me"
RUN_LOG="$(mktemp /tmp/hyperliquid-sdk-run.XXXXXX.log)"
DEPENDABOT_MANIFEST="$(mktemp /tmp/hyperliquid-dependabot-intake.XXXXXX.json)"
export HYPERLIQUID_DEPENDABOT_MANIFEST="$DEPENDABOT_MANIFEST"
OMP_PATH="${OMP_PATH:-omp}"

# On failure, email Carter. Runs on EXIT so success paths stay untouched.
on_exit() {
    local rc=$?
    if [ "$rc" -eq 0 ]; then
        rm -f "$RUN_LOG" "$DEPENDABOT_MANIFEST"
        return 0
    fi
    local ts body_file logs
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    body_file="/tmp/hyperliquid-sdk-fail.html"
    logs="$(journalctl --user -u hyperliquid-sdk.service -b --no-pager -n 50 2>/dev/null || cat "$RUN_LOG" 2>/dev/null || echo '(no logs available)')"

    cat > "$body_file" <<HTMLEOF
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0; padding:0; background-color:#f4f4f7; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
<table role="presentation" width="100%" style="background-color:#f4f4f7; padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" style="max-width:600px; width:100%; background-color:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.08);">
<tr>
  <td style="background-color:#c62828; padding:28px 32px;">
    <h1 style="margin:0; color:#ffffff; font-size:22px; font-weight:600;">⚠️ hyperliquid-sdk FAILED</h1>
    <p style="margin:6px 0 0; color:#ffcdd2; font-size:14px;">${ts} (exit ${rc})</p>
  </td>
</tr>
<tr>
  <td style="padding:24px 32px 16px;">
    <p style="margin:0; color:#444; font-size:15px; line-height:1.6;">
      The scheduled Hyperliquid SDK maintenance run failed. Investigate on the host:
    </p>
    <pre style="margin:12px 0 0; padding:12px; background:#f5f5f5; border-radius:4px; font-size:13px; color:#333;">journalctl --user -u hyperliquid-sdk.service -b --no-pager</pre>
  </td>
</tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #e8e8ee; margin:8px 0;"></td></tr>
<tr>
  <td style="padding:16px 32px 8px;">
    <h2 style="margin:0; color:#1a1a2e; font-size:15px; font-weight:700;">Journal excerpt</h2>
  </td>
</tr>
<tr>
  <td style="padding:8px 32px 24px;">
    <pre style="margin:0; padding:12px; background:#fafafa; border:1px solid #e8e8ee; border-radius:4px; font-size:12px; line-height:1.5; color:#555; white-space:pre-wrap; word-break:break-all;">${logs}</pre>
  </td>
</tr>
<tr>
  <td style="padding:24px 32px; background-color:#f8f8fb; border-top:1px solid #e8e8ee;">
    <p style="margin:0; color:#999; font-size:12px; text-align:center;">carter2099.com · ${ts}</p>
  </td>
</tr>
</table>
</td></tr>
</table>
</body>
</html>
HTMLEOF

    python3 "$HOME/scripts/send_digest.py" \
        --subject "hyperliquid-sdk FAILED ${ts} (exit ${rc})" \
        --body-file "$body_file" \
        --to "$RECIPIENT" \
        || echo "WARNING: failed to send failure email" >&2
    rm -f "$body_file" "$RUN_LOG" "$DEPENDABOT_MANIFEST"
}
trap on_exit EXIT

# Sanity check: verify omp and its bun interpreter are reachable
# (exit 127 on omp means PATH issue at execution time)
if ! command -v "$OMP_PATH" &>/dev/null; then
    echo "FATAL: omp not found: $OMP_PATH (PATH=$PATH)" >&2
    exit 1
fi
if ! command -v bun &>/dev/null; then
    echo "FATAL: bun (omp interpreter) not found in PATH=$PATH" >&2
    exit 1
fi
echo "ok: omp at $(command -v "$OMP_PATH"), bun at $(command -v bun)"

# GitHub discovery and Prompt-Guard classification happen outside the model.
# The manifest contains only validated branch metadata; PR titles and bodies
# never enter the agent prompt or tool context.
python3 "$HOME/scripts/hyperliquid_dependabot_intake.py" \
    --output "$DEPENDABOT_MANIFEST" \
    2>&1 | tee -a "$RUN_LOG"

PROMPT='/hyperliquid-run'

"$OMP_PATH" -p \
    --model opencode-go/glm-5.2 \
    --api-key proxy \
    --allow-home \
    --config "$HOME/.omp/agent/headless-override.yml" \
    --tools bash,read,write,edit,grep \
    -e "$HOME/.config/hyperliquid-agent/omp-dependabot-guard.ts" \
    --session-dir "$HOME/.omp/agent/sessions-automated" \
    "$PROMPT" 2>&1 | tee -a "$RUN_LOG"
