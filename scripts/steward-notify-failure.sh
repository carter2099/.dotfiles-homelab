#!/usr/bin/env bash
# Emailed failure notification for the homelab-steward service.
# Wired via OnFailure=homelab-steward-notify.service.
# Reuses ~/scripts/send_digest.py + ~/scripts/.smtp_config (Docker-independent SMTP).
#
# Logs of the failed run live in the user journal:
#   journalctl --user -u homelab-steward.service -b --no-pager
set -euo pipefail

export HOME="/home/carter"
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

RECIPIENT="carter2099@pm.me"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BODY_FILE="/tmp/homelab-steward-fail.html"

# Last 50 lines of the current-boot journal for the failing unit.
LOGS="$(journalctl --user -u homelab-steward.service -b --no-pager -n 50 2>/dev/null || echo "(journal unavailable)")"

# Build a minimal HTML email body.
cat > "$BODY_FILE" <<HTMLEOF
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0; padding:0; background-color:#f4f4f7; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
<table role="presentation" width="100%" style="background-color:#f4f4f7; padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" style="max-width:600px; width:100%; background-color:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.08);">
<tr>
  <td style="background-color:#c62828; padding:28px 32px;">
    <h1 style="margin:0; color:#ffffff; font-size:22px; font-weight:600;">⚠️ homelab-steward FAILED</h1>
    <p style="margin:6px 0 0; color:#ffcdd2; font-size:14px;">${TS}</p>
  </td>
</tr>
<tr>
  <td style="padding:24px 32px 16px;">
    <p style="margin:0; color:#444; font-size:15px; line-height:1.6;">
      The nightly steward run failed. Investigate on the host:
    </p>
    <pre style="margin:12px 0 0; padding:12px; background:#f5f5f5; border-radius:4px; font-size:13px; color:#333;">journalctl --user -u homelab-steward.service -b --no-pager</pre>
  </td>
</tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #e8e8ee; margin:8px 0;"></td></tr>
<tr>
  <td style="padding:16px 32px 8px;">
    <h2 style="margin:0; color:#1a1a2e; font-size:15px; font-weight:700;">Last 50 journal lines</h2>
  </td>
</tr>
<tr>
  <td style="padding:8px 32px 24px;">
    <pre style="margin:0; padding:12px; background:#fafafa; border:1px solid #e8e8ee; border-radius:4px; font-size:12px; line-height:1.5; color:#555; white-space:pre-wrap; word-break:break-all;">${LOGS}</pre>
  </td>
</tr>
<tr>
  <td style="padding:24px 32px; background-color:#f8f8fb; border-top:1px solid #e8e8ee;">
    <p style="margin:0; color:#999; font-size:12px; text-align:center;">carter2099.com · ${TS}</p>
  </td>
</tr>
</table>
</td></tr>
</table>
</body>
</html>
HTMLEOF

python3 "$HOME/scripts/send_digest.py" \
  --subject "homelab-steward FAILED ${TS}" \
  --body-file "$BODY_FILE" \
  --to "$RECIPIENT"

rm -f "$BODY_FILE"
echo "[steward-notify-failure] sent to ${RECIPIENT}"
