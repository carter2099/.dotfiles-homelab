#!/usr/bin/env bash
# Run all 5 daily digests sequentially via the deterministic workflow runner.
# Scheduled by systemd timer (digests-daily.timer) in the 4am-8am ET window.
#
# Each digest runs the full 9-phase pipeline. They must be sequential because
# the local llama.cpp backend is single-request. Total: ~3-3.5 hours.

set -euo pipefail

# omp binary (bun) is at ~/.bun/bin/ — not in systemd default PATH
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
export HOME="/home/carter"
LOGFILE="$HOME/digests/.digests.log"

# ── Signal trap: log cleanly on termination with current topic ──
_CURRENT_TOPIC_FILE="$HOME/digests/.current-topic"
_cleanup() {
    local rc=$?
    local topic="(unknown)"
    if [ -f "$_CURRENT_TOPIC_FILE" ]; then
        topic=$(cat "$_CURRENT_TOPIC_FILE" 2>/dev/null || echo "(unknown)")
        rm -f "$_CURRENT_TOPIC_FILE"
    fi
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) SCRIPT TERMINATED (exit=$rc) topic=$topic — incomplete run" | tee -a "$LOGFILE" 2>/dev/null || true
    exit $rc
}
trap _cleanup TERM INT HUP

# ── Incomplete-run detection ──
# A run is incomplete only if it never wrote ALL DONE. Post-completion lines
# (WARN-ALERT and friends) may legitimately follow ALL DONE, so checking only
# the final log line produced false "Previous run incomplete" warnings
# (digest-quality audit 2026-08-25): compare the last START marker with the
# last ALL DONE instead.
if [ -f "$LOGFILE" ]; then
    LAST_START_LN=$(grep -n " START " "$LOGFILE" 2>/dev/null | tail -1 | cut -d: -f1 || true)
    LAST_DONE_LN=$(grep -n "ALL DONE" "$LOGFILE" 2>/dev/null | tail -1 | cut -d: -f1 || true)
    if [ -n "$LAST_START_LN" ] && { [ -z "$LAST_DONE_LN" ] || [ "$LAST_DONE_LN" -lt "$LAST_START_LN" ]; }; then
        # Extract topic from the most recent SCRIPT TERMINATED line (which must
        # come after the last ALL DONE to belong to the incomplete run)
        topic_hint=""
        if LAST_TERM_LN=$(grep -n "SCRIPT TERMINATED" "$LOGFILE" 2>/dev/null | tail -1 | cut -d: -f1) && \
           [ -n "$LAST_TERM_LN" ] && { [ -z "$LAST_DONE_LN" ] || [ "$LAST_TERM_LN" -gt "$LAST_DONE_LN" ]; }; then
            topic_hint=$(sed -n "${LAST_TERM_LN}p" "$LOGFILE" 2>/dev/null | sed 's/.*topic=\([^ ]*\).*/\1/' || true)
        fi
        if [ -n "$topic_hint" ]; then
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARNING Previous run interrupted during topic=$topic_hint" | tee -a "$LOGFILE"
        else
            echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARNING Previous run incomplete" | tee -a "$LOGFILE"
        fi
    fi
fi

TOPICS=("ai-tech" "agentic-platform" "ai-hardware" "gaming" "world")

for topic in "${TOPICS[@]}"; do
    echo "$topic" > "$_CURRENT_TOPIC_FILE"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) START $topic" | tee -a "$LOGFILE"
    START_TS=$(date +%s)

    if timeout 14400 python3 "$HOME/scripts/digest_runner.py" "$topic"; then
        END_TS=$(date +%s)
        DURATION=$((END_TS - START_TS))
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) DONE  $topic duration=${DURATION}s" | tee -a "$LOGFILE" || true
        # Report editorial-stage degradation in the lifecycle log (digest-quality
        # audit 2026-08-11): a successful run can still have fallen back to a
        # weaker model for proposal/review, which used to be invisible here.
        TODAY=$(date -u +%Y-%m-%d)
        case "$topic" in
            gaming) CATEGORY_DIR="gaming-digest" ;;
            world) CATEGORY_DIR="world-digest" ;;
            *) CATEGORY_DIR="$topic" ;;
        esac
        CURATED="$HOME/digests/$CATEGORY_DIR/$TODAY/06-curated.json"
        if [ -f "$CURATED" ]; then
            DEGRADED=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print('degraded' if d.get('editorial',{}).get('degraded') else 'ok')" "$CURATED" 2>/dev/null || echo ok)
            if [ "$DEGRADED" = "degraded" ]; then
                MODELS=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));e=d.get('editorial',{});print('proposal_model=%s review_model=%s review_status=%s' % (e.get('proposal_model',''),e.get('review_model',''),e.get('review_status','')))" "$CURATED" 2>/dev/null || echo "models unknown")
                echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARN  $topic editorial degraded $MODELS" | tee -a "$LOGFILE" || true
            fi
        fi
    else
        RC=$?
        END_TS=$(date +%s)
        DURATION=$((END_TS - START_TS))
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) FAIL  $topic (exit=$RC) duration=${DURATION}s — continuing" | tee -a "$LOGFILE" || true
    fi
    rm -f "$_CURRENT_TOPIC_FILE"
done

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ALL DONE" | tee -a "$LOGFILE" || true

# ── Consecutive editorial-degradation alert ──
# digest-quality audit 2026-08-15: recurring luna-primary instability
# (08-12: 3 topics mimo; 08-14: ai-hardware mimo + agentic raw fallback;
# 08-15: 4 of 5 topics mimo) degraded topics to the fallback model on
# multiple days, silently from Carter's perspective. Alert when
# "editorial degraded" WARNs appear on two consecutive days so the
# upstream primary path gets investigated instead of compounding.
TODAY="$(date -u +%Y-%m-%d)"
YESTERDAY="$(date -u -d yesterday +%Y-%m-%d)"
WARN_DAYS="$(grep 'editorial degraded' "$LOGFILE" 2>/dev/null | cut -c1-10 | sort -u || true)"
if printf '%s\n' "$WARN_DAYS" | grep -qx "$YESTERDAY" && \
   printf '%s\n' "$WARN_DAYS" | grep -qx "$TODAY" && \
   ! grep -q "^${TODAY}T.*WARN-ALERT" "$LOGFILE" 2>/dev/null; then
    ALERT_BODY="/tmp/digests-degraded-alert.html"
    DETAIL="$(grep 'editorial degraded' "$LOGFILE" 2>/dev/null | grep -E "^(${YESTERDAY}|${TODAY})" | sed 's/^/  /' || true)"
    cat > "$ALERT_BODY" <<HTMLEOF
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0; padding:0; background-color:#f4f4f7; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
<table role="presentation" width="100%" style="background-color:#f4f4f7; padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" style="max-width:600px; width:100%; background-color:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.08);">
<tr>
  <td style="background-color:#ef6c00; padding:28px 32px;">
    <h1 style="margin:0; color:#ffffff; font-size:22px; font-weight:600;">Digest editorial degradation on consecutive days</h1>
    <p style="margin:6px 0 0; color:#ffe0b2; font-size:14px;">${YESTERDAY} and ${TODAY}</p>
  </td>
</tr>
<tr>
  <td style="padding:24px 32px 16px;">
    <p style="margin:0; color:#444; font-size:15px; line-height:1.6;">
      The daily digest pipeline fell back to a weaker editorial model on two consecutive days.
      This recurring pattern (also seen 08-12 and 08-14) points to instability in the luna
      primary path (opencode.ai upstream). Investigate on the host:
    </p>
    <pre style="margin:12px 0 0; padding:12px; background:#f5f5f5; border-radius:4px; font-size:13px; color:#333;">journalctl --user -u opencode-go-proxy.service -b --no-pager | grep -c 'free-tier 429'
grep 'editorial degraded' ~/digests/.digests.log | tail -20</pre>
  </td>
</tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #e8e8ee; margin:8px 0;"></td></tr>
<tr>
  <td style="padding:16px 32px 8px;">
    <h2 style="margin:0; color:#1a1a2e; font-size:15px; font-weight:700;">Degraded topics</h2>
  </td>
</tr>
<tr>
  <td style="padding:8px 32px 24px;">
    <pre style="margin:0; padding:12px; background:#fafafa; border:1px solid #e8e8ee; border-radius:4px; font-size:12px; line-height:1.5; color:#555; white-space:pre-wrap; word-break:break-all;">${DETAIL}</pre>
  </td>
</tr>
<tr>
  <td style="padding:24px 32px; background-color:#f8f8fb; border-top:1px solid #e8e8ee;">
    <p style="margin:0; color:#999; font-size:12px; text-align:center;">carter2099.com · ${TODAY}</p>
  </td>
</tr>
</table>
</td></tr>
</table>
</body>
</html>
HTMLEOF
    python3 "$HOME/scripts/send_digest.py" \
        --subject "digests: editorial degraded on consecutive days (${YESTERDAY}, ${TODAY})" \
        --body-file "$ALERT_BODY" \
        --to "carter2099@pm.me" || echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARN-ALERT email send failed" | tee -a "$LOGFILE" || true
    rm -f "$ALERT_BODY"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WARN-ALERT editorial degraded on consecutive days (${YESTERDAY}, ${TODAY}); alert emailed" | tee -a "$LOGFILE" || true
fi
