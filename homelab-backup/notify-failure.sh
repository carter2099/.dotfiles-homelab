#!/usr/bin/env bash
# Emailed failure notification for the homelab-backup service.
# Wired via OnFailure=homelab-backup-notify.service.
# Reuses ~/scripts/send_digest.py + ~/scripts/.smtp_config (Docker-independent SMTP).
#
# Logs of the failed run live in the user journal:
#   journalctl --user -u homelab-backup.service -b --no-pager
set -euo pipefail

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
RECIPIENT="carter2099@pm.me"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
EMAIL_TMPL="$HOME/homelab-backup/email-template.sh"

# Last 120 lines of the current-boot journal for the failing unit.
LOGS="$(journalctl --user -u homelab-backup.service -b --no-pager -n 120 2>/dev/null || echo "(journal unavailable)")"

# Build the email body via the shared, escaping template (status=fail → red banner).
{
  echo "homelab-backup failed at ${TS}"
  echo
  echo "One or more targets did not back up successfully. Run on the host for full logs:"
  echo "  journalctl --user -u homelab-backup.service -b --no-pager"
  echo
  echo "---- last 120 log lines ----"
  printf '%s\n' "$LOGS"
} | bash "$EMAIL_TMPL" fail /tmp/homelab-backup-fail.html

python3 "$HOME/scripts/send_digest.py" \
  --subject "homelab-backup FAILED ${TS}" \
  --body-file /tmp/homelab-backup-fail.html \
  --to "$RECIPIENT"

rm -f /tmp/homelab-backup-fail.html
echo "[notify-failure] sent to ${RECIPIENT}"