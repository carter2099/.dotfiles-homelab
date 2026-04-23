---
name: email-digest
description: Create a new scheduled email digest that runs daily via a systemd timer, spawning a headless Claude agent to search the web for news on specified topics and email an HTML summary.
---

# email-digest

Create a scheduled daily email digest powered by a local systemd timer that spawns a headless `claude -p` agent.

## Required input

Gather these from the user before creating the digest. Ask for anything missing:

- **name** (string): short kebab-case identifier (e.g. `ai-tech-digest`, `crypto-weekly`)
- **topics** (string): what the digest should cover — used to craft search queries in the prompt
- **recipients** (list of emails): one or more email addresses to send to
- **time** (string): time of day to send, in the user's local time. Convert to UTC for the systemd timer `OnCalendar` (server is UTC). Currently EDT = UTC-4. Confirm the conversion with the user.
- **frequency** (string, default: daily): daily, weekdays, weekly, etc.

## Infrastructure

- **Email script:** `~/scripts/send_digest.py` — takes `--subject`, `--body-file`, and `--to` (one or more addresses)
- **SMTP config:** `~/scripts/.smtp_config` — Proton Mail SMTP via `bot@carter2099.com`
- **HTML template:** `~/digests/template.html` — shared layout for all digests (dark header, story blocks, footer)
- **Digest history:** `~/digests/<name>/` — one `.md` summary file per run, kept for 7 days

If `~/scripts/send_digest.py` or `~/scripts/.smtp_config` don't exist, stop and tell the user — they need to be created first.

## Steps

1. **Verify infrastructure.** Confirm `~/scripts/send_digest.py` and `~/scripts/.smtp_config` exist.

2. **Create the history directory.** `mkdir -p ~/digests/<name>/`

3. **Build the OnCalendar expression.** Convert the user's desired time to UTC. Common conversions (EDT, Apr–Nov):
   - 8am ET → `*-*-* 12:00:00`
   - 9am ET → `*-*-* 13:00:00`
   - 11am ET → `*-*-* 15:00:00`
   - 12pm ET → `*-*-* 16:00:00`
   - For weekdays only: `Mon..Fri *-*-* 15:00:00`
   - For weekly (e.g. Monday): `Mon *-*-* 15:00:00`

4. **Craft the prompt.** Build a self-contained prompt for the headless agent. The prompt must include all five steps below — adapt the search instructions, categories, and context to match the user's topics:

   ```
   You are a daily [TOPIC] news curator. Your job is to research and email a digest.

   ## Step 0: Read prior digest summaries and the HTML template

   First, read ~/digests/template.html using the Read tool. You MUST use this template exactly for the email HTML — fill in the placeholders and use the story block HTML from the comment at the bottom. Do not invent your own layout or styling.

   Then read all .md files in ~/digests/[NAME]/ using the Read tool. These are summaries of stories you have already sent in recent days. Use them to:
   - Avoid repeating stories that have already been covered unless there is a meaningful update (new details, growing momentum, follow-up coverage, community reaction, adoption milestones).
   - Identify evolving stories worth tracking in the "Recent & Relevant" section.

   ## Step 1: Research

   Use WebSearch to find the most important [TOPIC] developments. Run at least 5-6 searches across different angles: [SPECIFIC ANGLES RELEVANT TO TOPIC]. Prioritize [USER'S PRIORITIES].

   ## Accuracy Rules (apply to every story)

   - Only include a story if you found it on a reputable outlet with a clear publication date. If the date is ambiguous or you cannot find a corroborating source, skip it.
   - Fresh stories must be from the last 24 hours. Recent & Relevant stories must be from the last 2-7 days. Verify dates before including.
   - Never fabricate or extrapolate details. If a search result is vague, move on — do not fill gaps with plausible-sounding information.
   - Every story URL must come directly from your search results. Never construct or guess a URL.

   ## Step 2: Compile the digest

   Organize the email into two sections:

   **Fresh (last 24 hours):** New stories from today. 6-10 stories.

   **Recent & Relevant (past week):** Stories from the past 2-7 days that are still evolving, gaining momentum, or have meaningful new developments since last covered. For each, note what changed or why it's still relevant. Only include if there's something new to say — do not simply repeat old summaries. 2-5 stories.

   Build the HTML by filling in the template you read in Step 0:
   - Replace {{DIGEST_TITLE}} with "[SUBJECT]"
   - Replace {{DATE}} with today's date
   - Replace {{INTRO}} with a 2-3 sentence editorial intro
   - Replace {{FRESH_STORIES}} with story blocks using the story block template from the HTML comment
   - Replace {{RECENT_STORIES}} with story blocks using the "Recent & Relevant" variant (with the WHY line)
   - Each story needs a TITLE, URL, CATEGORY (e.g. [CATEGORIES RELEVANT TO TOPIC]), and SUMMARY

   ## Step 3: Write and send

   1. Write the HTML body to /tmp/[NAME]_digest.html
   2. Send it: python3 ~/scripts/send_digest.py --subject "[SUBJECT] — $(date +%Y-%m-%d)" --body-file /tmp/[NAME]_digest.html --to [EMAILS]
   3. Clean up: rm /tmp/[NAME]_digest.html

   ## Step 4: Write summary for future runs

   Write a concise summary of what you sent to ~/digests/[NAME]/YYYY-MM-DD.md in this format:

   ```
   # [SUBJECT] — YYYY-MM-DD

   ## Fresh
   - **Story title** — one-line summary
   - **Story title** — one-line summary

   ## Recent & Relevant
   - **Story title** — one-line summary (why still relevant)
   ```

   Then delete any .md files in ~/digests/[NAME]/ older than 7 days.
   ```

5. **Create the runner script.** Write `~/scripts/run_[NAME].sh` (replace hyphens with underscores for the filename):

   ```bash
   #!/usr/bin/env bash
   set -euo pipefail

   export HOME="/home/carter"
   export PATH="$HOME/.local/bin:$HOME/.rbenv/bin:$HOME/.fnm:$PATH"
   export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"

   PROMPT='<THE PROMPT FROM STEP 4>'

   exec claude -p \
     --model claude-sonnet-4-6 \
     --dangerously-skip-permissions \
     --allowedTools "WebSearch Bash Read Write Edit Glob Grep" \
     --no-session-persistence \
     "$PROMPT"
   ```

   Make sure to `chmod +x` the script.

   Note on quoting: the PROMPT is wrapped in single quotes. If the prompt itself contains single quotes (e.g. `today's`), escape them as `'"'"'` (end single-quote, double-quote a single-quote, start single-quote again). For the date in the summary file path, break out of the single-quoted PROMPT to embed `$(date +%Y-%m-%d)` in double quotes so it evaluates at runtime.

6. **Create the systemd service.** Write `~/.config/systemd/user/[NAME].service`:

   ```ini
   [Unit]
   Description=Daily [TOPIC] email digest via Claude agent

   [Service]
   Type=oneshot
   ExecStart=/home/carter/scripts/run_[NAME].sh
   TimeoutStartSec=600
   ```

7. **Create the systemd timer.** Write `~/.config/systemd/user/[NAME].timer`:

   ```ini
   [Unit]
   Description=Fire [TOPIC] digest [FREQUENCY] at [TIME] ET

   [Timer]
   OnCalendar=[ONCALENDAR EXPRESSION]
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```

   `Persistent=true` ensures a missed run (e.g. host was off) fires on next boot.

8. **Enable the timer.**

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now [NAME].timer
   ```

9. **Report.** Show the user:
   - Timer status: `systemctl --user status [NAME].timer`
   - Next fire time from `systemctl --user list-timers [NAME].timer`
   - Recipient list
   - Offer to do a test run: `bash ~/scripts/run_[NAME].sh`

## Modifying an existing digest

To add/remove recipients or change topics for an existing digest:

1. Edit the prompt in `~/scripts/run_[NAME].sh` (update `--to` addresses or search instructions)
2. No need to restart the timer — the script is read fresh each invocation

To change the schedule:

1. Edit `~/.config/systemd/user/[NAME].timer` (update `OnCalendar`)
2. `systemctl --user daemon-reload && systemctl --user restart [NAME].timer`

## Listing digests

```bash
systemctl --user list-timers '*digest*' --no-pager
```

## Deleting a digest

```bash
systemctl --user disable --now [NAME].timer
rm ~/.config/systemd/user/[NAME].service ~/.config/systemd/user/[NAME].timer
rm ~/scripts/run_[NAME].sh
rm -rf ~/digests/[NAME]/
systemctl --user daemon-reload
```

## Notes

- Agents run locally on the homelab via `claude -p` (headless print mode)
- `--dangerously-skip-permissions` is safe here — no untrusted input, script is only invoked by the timer
- `DBUS_SESSION_BUS_ADDRESS` is required so the headless process can reach the keychain for OAuth credentials
- `--no-session-persistence` avoids filling disk with one-shot session data
- `TimeoutStartSec=600` (10 minutes) prevents a stuck agent from blocking indefinitely
- Digest history in `~/digests/<name>/` provides dedup context — the agent reads prior summaries to avoid repeating stories and to track evolving narratives across days
- The "Recent & Relevant" section catches stories that are evolving or gaining momentum, not just stories from the last 24 hours — momentum and follow-up coverage are valid reasons to re-include a story
- **Summary files are trusted context** — the agent treats prior summaries as ground truth when deciding what to include in "Recent & Relevant." If a hallucinated or inaccurate story gets into a summary file, it will persist across future runs. If accuracy guardrails are added or changed, clear existing summary files (`rm ~/digests/<name>/*.md`) so stale/bad data doesn't carry forward
