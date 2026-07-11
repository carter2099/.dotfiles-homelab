#!/usr/bin/env bash
# Monthly restore drill: download the newest backup from R2, verify its
# integrity (readable tar.gz + PRAGMA integrity_check on every embedded DB),
# and email Carter a PASS/FAIL report. It never touches prod data.
#
# Wired to homelab-backup-restore-drill.{service,timer}. Also runnable by hand:
#   bash ~/homelab-backup/restore-drill.sh
#
# Requires R2 creds in the env (systemd EnvironmentFile=~/homelab-backup/.env
# provides R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY under the timer).
set -uo pipefail

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
RECIPIENT="carter2099@pm.me"
HB="$HOME/homelab-backup/homelab-backup"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

REPORT="$WORK/report.txt"
: > "$REPORT"

run() {
  echo "+ $*" >> "$REPORT"
  "$@" >> "$REPORT" 2>&1
  echo >> "$REPORT"
}

echo "homelab-backup restore drill — ${TS}" > "$REPORT"
echo "host: $(hostname)  work: ${WORK}" >> "$REPORT"
echo >> "$REPORT"

# 1. Fetch newest backup from R2.
DL_OUT="$WORK/download_path.txt"
run "$HB" latest "$WORK"
# homelab-backup latest prints the downloaded path to stdout (slog lines go to
# stderr); the whole combined stream is in report.txt. Grab the last .tar.gz
# path match — robust to stderr/stdout interleaving.
ARCHIVE="$(grep -Eo '/[^ ]+\.tar\.gz' "$WORK/report.txt" | tail -1)"

EMAIL_TMPL="$HOME/homelab-backup/email-template.sh"
[ -x "$EMAIL_TMPL" ] || EMAIL_TMPL="/usr/bin/env bash $HOME/homelab-backup/email-template.sh"

if [ -z "${ARCHIVE:-}" ] || [ ! -f "${ARCHIVE:-/nonexistent}" ]; then
  SUBJECT="homelab restore drill FAILED ${TS} (download)"
  {
    echo "FAILED at the download stage. Did R2 creds reach the drill? Is the bucket reachable?"
    echo
    cat "$REPORT"
  } | bash "$EMAIL_TMPL" fail /tmp/restore-drill-body.html
  python3 "$HOME/scripts/send_digest.py" --subject "$SUBJECT" --body-file /tmp/restore-drill-body.html --to "$RECIPIENT"
  rm -f /tmp/restore-drill-body.html
  echo "[restore-drill] FAILED at download; emailed ${RECIPIENT}" >&2
  exit 1
fi

# 2. Verify the archive.
VERIFY_RC=0
run "$HB" verify "$ARCHIVE" || VERIFY_RC=$?

if [ "$VERIFY_RC" -eq 0 ]; then
  SUBJECT="homelab restore drill PASS ${TS}"
  {
    echo "PASS — newest R2 backup downloaded and verified intact."
    echo
    cat "$REPORT"
  } | bash "$EMAIL_TMPL" pass /tmp/restore-drill-body.html
else
  SUBJECT="homelab restore drill FAILED ${TS} (verify)"
  {
    echo "FAIL — archive downloaded but integrity check failed. Investigate before trusting backups."
    echo
    cat "$REPORT"
  } | bash "$EMAIL_TMPL" fail /tmp/restore-drill-body.html
fi

python3 "$HOME/scripts/send_digest.py" --subject "$SUBJECT" --body-file /tmp/restore-drill-body.html --to "$RECIPIENT"
rm -f /tmp/restore-drill-body.html
echo "[restore-drill] done (verify rc=${VERIFY_RC}); emailed ${RECIPIENT}"
exit "$VERIFY_RC"