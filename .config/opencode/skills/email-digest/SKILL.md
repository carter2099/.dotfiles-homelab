---
name: email-digest
description: Create a new scheduled email digest that runs daily via a systemd timer, spawning a headless opencode agent (MiniMax M3) to research the web for news on specified topics and email an HTML summary.
---

# email-digest

Create a scheduled daily email digest powered by a local systemd timer that spawns a headless `opencode run` agent on the OpenCode Go subscription, pinned to MiniMax M3 (`opencode-go/minimax-m3`). Provider-agnostic — the model is a single `-m` flag.

## Required input

Gather these from the user before creating the digest. Ask for anything missing:

- **name** (string): short kebab-case identifier (e.g. `ai-tech-digest`, `crypto-weekly`)
- **topics** (string): what the digest should cover
- **sources** (list of URLs): reputable homepages to fetch for this topic (opencode has **no web-search tool** — research is done by fetching news homepages and following article links, so good starting sources matter). If the user doesn't specify, suggest a few reputable outlets for the topic.
- **recipients** (list of emails): one or more addresses to send to
- **time** (string): time of day in the user's local time. Convert to UTC for the systemd timer `OnCalendar` (server is UTC). Currently EDT = UTC-4. Confirm the conversion with the user.
- **frequency** (string, default: daily): daily, weekdays, weekly, etc.

## Infrastructure

- **Email script:** `~/scripts/send_digest.py` — takes `--subject`, `--body-file`, and `--to` (one or more addresses)
- **SMTP config:** `~/scripts/.smtp_config` — Proton Mail SMTP via `bot@carter2099.com` (un-tracked; also a good place to stash third-party recipient addresses, see Step 5 note)
- **HTML template:** `~/digests/template.html` — shared layout for all digests (dark header, story blocks, footer)
- **Digest history:** `~/digests/<name>/` — one `.md` summary file per run, kept for 7 days
- **opencode binary:** `~/.opencode/bin/opencode`; auth is an OpenCode Go API key in `~/.local/share/opencode/auth.json`

If `~/scripts/send_digest.py` or `~/scripts/.smtp_config` don't exist, stop and tell the user — they need to be created first.

## Steps

1. **Verify infrastructure.** Confirm `~/scripts/send_digest.py`, `~/scripts/.smtp_config`, and `~/.opencode/bin/opencode` exist.

2. **Create the history directory.** `mkdir -p ~/digests/<name>/`

3. **Build the OnCalendar expression.** Convert the user's desired time to UTC. Common conversions (EDT, Apr–Nov):
   - 8am ET → `*-*-* 12:00:00`
   - 9am ET → `*-*-* 13:00:00`
   - 11am ET → `*-*-* 15:00:00`
   - 12pm ET → `*-*-* 16:00:00`
   - For weekdays only: `Mon..Fri *-*-* 15:00:00`
   - For weekly (e.g. Monday): `Mon *-*-* 15:00:00`

4. **Craft the prompt.** Build a self-contained prompt for the headless agent. The prompt must include all five steps below — adapt the sources, categories, and context to match the user's topics:

   ```
   You are a daily [TOPIC] news curator. Your job is to research and email a digest.

   ## Step 0: Read prior digest summaries and the HTML template

   First, read /home/carter/digests/template.html using the Read tool. You MUST use this template exactly for the email HTML — fill in the placeholders and use the story block HTML from the comment at the bottom. Do not invent your own layout or styling.

   Then read all .md files in /home/carter/digests/[NAME]/ using the Read tool (may be empty on the first run). These are summaries of stories you have already sent in recent days. Use them to:
   - Avoid repeating stories that have already been covered unless there is a meaningful update (new details, growing momentum, follow-up coverage, community reaction, adoption milestones).
   - Identify evolving stories worth tracking in the "Recent & Relevant" section.

   ## Step 1: Research (use your WebFetch tool — opencode has NO web search tool)

   Fetch reputable [TOPIC] news sources and follow links to specific articles. Start from several of these homepages, then fetch the specific article pages you intend to cite:
   - [SOURCE HOMEPAGE 1]
   - [SOURCE HOMEPAGE 2]
   - [SOURCE HOMEPAGE 3]
   Cover [SPECIFIC ANGLES RELEVANT TO TOPIC]. Prioritize [USER'S PRIORITIES]. If a source fails to fetch, try another reputable outlet.

   ## Accuracy Rules (apply to every story)

   - Only include a story you actually fetched from a reputable outlet with a clear publication date. If the date is ambiguous or you cannot confirm it by fetching, skip it.
   - Fresh stories must be from the last 24 hours. Recent & Relevant stories must be from the last 2-7 days. Verify dates by fetching before including.
   - Never fabricate or extrapolate details. If a page is vague, move on — do not fill gaps with plausible-sounding information.
   - Every story URL must be a real URL you fetched. Never construct or guess a URL.

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

   IMPORTANT: write only inside /home/carter — writes outside it (e.g. /tmp) are blocked in headless mode.
   1. Write the HTML body to /home/carter/digests/[NAME]/.daily_digest.html
   2. Send it by running this command with your Bash tool: python3 /home/carter/scripts/send_digest.py --subject "[SUBJECT] — [DATE]" --body-file /home/carter/digests/[NAME]/.daily_digest.html --to [EMAILS]
   3. Clean up: rm /home/carter/digests/[NAME]/.daily_digest.html

   ## Step 4: Write summary for future runs

   Write a concise summary of what you sent to /home/carter/digests/[NAME]/[DATE].md in this format:

   # [SUBJECT] — [DATE]

   ## Fresh
   - **Story title** — one-line summary
   - **Story title** — one-line summary

   ## Recent & Relevant
   - **Story title** — one-line summary (why still relevant)

   Then delete any .md files in /home/carter/digests/[NAME]/ older than 7 days.
   ```

5. **Create the runner script.** Write `~/scripts/run_[NAME].sh` (replace hyphens with underscores for the filename):

   ```bash
   #!/usr/bin/env bash
   # Researches and emails the daily [TOPIC] digest via opencode + MiniMax M3.
   # Scheduled via systemd timer ([NAME].timer). Provider-agnostic: change the
   # -m model id to switch providers/models without touching anything else.
   set -euo pipefail

   export HOME="/home/carter"
   export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"
   export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"

   TODAY="$(date +%Y-%m-%d)"

   PROMPT='<THE PROMPT FROM STEP 4>'

   exec "$HOME/.opencode/bin/opencode" run -m opencode-go/minimax-m3 "$PROMPT" < /dev/null
   ```

   Make sure to `chmod +x` the script.

   **Quoting:** the PROMPT is wrapped in single quotes. For dates, set `TODAY="$(date +%Y-%m-%d)"` at the top of the script and insert it into the single-quoted prompt with `'"$TODAY"'` (end single-quote, double-quote the var, restart single-quote) — use this wherever the prompt shows `[DATE]` (the subject line, the summary path, and the `{{DATE}}` placeholder). If the prompt text itself contains a literal single quote (e.g. `today's`), escape it as `'"'"'`.

   **Third-party recipients (public repo!):** these run scripts are tracked in the **public** `.dotfiles-homelab` repo. The user's own address is fine to inline, but never hardcode a third party's email. Instead add it to the un-tracked `~/scripts/.smtp_config` (e.g. `FOO_CC=person@example.com`), read it in the script (`FOO_CC="$(grep -E '^FOO_CC=' "$HOME/scripts/.smtp_config" | cut -d= -f2-)"`), and inject it into the prompt's `--to` line with `'"$FOO_CC"'`. (The `agentic-digest` does this.)

6. **Create the systemd service.** Write `~/.config/systemd/user/[NAME].service`:

   ```ini
   [Unit]
   Description=Daily [TOPIC] email digest via opencode (MiniMax M3)

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

9. **Track in dotfiles + report.** The digest scripts, units, and template are tracked in the dotfiles bare repo — add and commit the new files (NOT `.smtp_config`):

   ```bash
   dotfiles add scripts/run_[NAME].sh .config/systemd/user/[NAME].service .config/systemd/user/[NAME].timer
   dotfiles commit -m "digest: add [NAME]" && dotfiles push
   ```

   Then show the user:
   - Timer status: `systemctl --user status [NAME].timer`
   - Next fire time from `systemctl --user list-timers [NAME].timer`
   - Recipient list
   - Offer to do a test run: `bash ~/scripts/run_[NAME].sh` — then **verify it actually sent**: check the journal for a `Sent to` line and that `~/digests/[NAME]/[DATE].md` was written. `opencode run` exits 0 even if the model skipped a step, so the exit code alone is not proof of success.

## Modifying an existing digest

To add/remove recipients or change topics/sources for an existing digest:

1. Edit the prompt in `~/scripts/run_[NAME].sh` (update `--to` addresses, source homepages, or angles)
2. No need to restart the timer — the script is read fresh each invocation
3. Commit the change: `dotfiles add scripts/run_[NAME].sh && dotfiles commit -m "digest: update [NAME]" && dotfiles push`

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

- Agents run locally on the homelab via `opencode run` (headless) on the OpenCode Go subscription, pinned to MiniMax M3.
- **Headless opencode gotchas (load-bearing):**
  - **stdin must be closed** (`< /dev/null`) or `opencode run` hangs after finishing the task.
  - **All file I/O must stay under `/home/carter`** — opencode auto-rejects writes outside the working directory in headless mode, so the temp HTML goes in `~/digests/<name>/`, not `/tmp`.
  - **`opencode run` exits 0 even when the model skipped a step** — verify artifacts (the `Sent to …` journal line + the summary `.md`), never the exit code alone.
  - **No WebSearch tool** — research is `WebFetch` against curated homepages; pick good source URLs per topic. opencode's WebFetch reaches some sites Claude's blocks (e.g. arstechnica), but some outlets bot-block (gamespot returned 403) — give several sources so one failure isn't fatal.
- `DBUS_SESSION_BUS_ADDRESS` is kept for parity with the systemd user session; opencode auth comes from `~/.local/share/opencode/auth.json`, not the keychain.
- `TimeoutStartSec=600` (10 minutes) prevents a stuck agent from blocking indefinitely.
- Digest history in `~/digests/<name>/` provides dedup context — the agent reads prior summaries to avoid repeating stories and to track evolving narratives across days.
- The "Recent & Relevant" section catches stories that are evolving or gaining momentum, not just stories from the last 24 hours — momentum and follow-up coverage are valid reasons to re-include a story.
- **Summary files are trusted context** — the agent treats prior summaries as ground truth when deciding what to include in "Recent & Relevant." If a hallucinated or inaccurate story gets into a summary file, it will persist across future runs. If accuracy guardrails are added or changed, clear existing summary files (`rm ~/digests/<name>/*.md`) so stale/bad data doesn't carry forward.
