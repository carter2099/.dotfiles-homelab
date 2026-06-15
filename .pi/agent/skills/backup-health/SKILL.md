---
name: backup-health
description: Check health of the homelab-backup service — last run status, next scheduled run, R2 bucket contents, and local archive state. Quick mobile-friendly summary.
---

# backup-health

Health check for the homelab-backup systemd timer + R2 upload pipeline.

## Steps

1. **Timer status.** Run `systemctl --user list-timers homelab-backup.timer --no-pager` to show next/last fire time.
2. **Last run result.** Run `systemctl --user status homelab-backup.service --no-pager -l` to check exit status of the most recent run.
3. **Recent logs.** Run `journalctl --user -u homelab-backup.service -n 25 --no-pager` for the last run's output. Look for errors, partial failures, or integrity check issues.
4. **R2 bucket contents.** Load the R2 credentials and list remote backups:
   ```bash
   set -a && source ~/homelab-backup/.env && set +a
   ~/homelab-backup/homelab-backup list 2>&1 || \
     aws s3 ls s3://homelab-backup/ --endpoint-url https://a77d31afd0d47d93f186059514689751.r2.cloudflarestorage.com 2>/dev/null || \
     echo "(list command not available — check R2 dashboard)"
   ```
   If the binary doesn't have a `list` subcommand yet, just report what the logs say about upload/retention.
5. **Local archives.** Run `ls -lht ~/backups/ | head -10` to show recent local backups and sizes.
6. **Disk usage.** Run `du -sh ~/backups/` to report total local backup footprint.

## Report format

Summarize as a short mobile-friendly checklist:

- **Last run:** when, success/failure
- **Next run:** when
- **R2 backups:** count and newest date
- **Local backups:** count and total size
- **Issues:** any errors or warnings (or "none")

Keep it concise — this is meant to be glanced at on a phone.
