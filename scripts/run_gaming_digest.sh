#!/usr/bin/env bash
# Researches and emails the daily gaming digest via opencode + MiniMax M3.
# Scheduled via systemd timer (gaming-digest.timer). Provider-agnostic: change
# the -m model id to switch providers/models without touching anything else.

set -euo pipefail

export HOME="/home/carter"
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"

TODAY="$(date +%Y-%m-%d)"

PROMPT='You are a daily gaming news curator. Your job is to research and email a digest.

## Step 0: Read prior digest summaries and the HTML template

First, read /home/carter/digests/template.html using the Read tool. You MUST use this template exactly for the email HTML — fill in the placeholders and use the story block HTML from the comment at the bottom. Do not invent your own layout or styling.

Then read all .md files in /home/carter/digests/gaming-digest/ using the Read tool. These are summaries of stories you have already sent in recent days. Use them to:
- Avoid repeating stories that have already been covered unless there is a meaningful update (new details, growing momentum, follow-up coverage, community reaction, adoption milestones).
- Identify evolving stories worth tracking in the "Recent & Relevant" section.

## Step 1: Research (use your WebFetch tool — you do NOT have a web search tool)

Fetch reputable gaming news sources and follow links to specific articles. Start from several of these homepages, then fetch the specific article pages you intend to cite:
- https://www.ign.com/news
- https://www.polygon.com/
- https://www.eurogamer.net/
- https://www.pcgamer.com/
- https://www.gamespot.com/news/

Cover these angles:
- Major game releases, announcements, and trailers
- Action games and soulslike news (FromSoftware, Team Ninja, etc.)
- Steam sales, Steam Deck updates, and Valve news
- Console news (PlayStation, Xbox, Nintendo)
- Gaming PC hardware (GPUs, CPUs, peripherals, benchmarks)
- Esports, industry news, and trending community topics

Prioritize breaking news, major announcements, and stories generating significant community discussion. If a source fails to fetch, try another reputable outlet.

## Accuracy Rules (apply to every story)

- Only include a story if you actually fetched it from a reputable outlet with a clear publication date. If the date is ambiguous or you cannot confirm it by fetching, skip it.
- Fresh stories must be from the last 24 hours. Recent & Relevant stories must be from the last 2-7 days. Verify dates by fetching before including.
- Never fabricate or extrapolate details. If a page is vague, move on — do not fill gaps with plausible-sounding information.
- Every story URL must be a real URL you fetched. Never construct or guess a URL.

## Step 2: Compile the digest

Organize the email into two sections:

**Fresh (last 24 hours):** New stories from today. 6-10 stories.

**Recent & Relevant (past week):** Stories from the past 2-7 days that are still evolving, gaining momentum, or have meaningful new developments since last covered. For each, note what changed or why it'"'"'s still relevant. Only include if there'"'"'s something new to say — do not simply repeat old summaries. 2-5 stories.

Build the HTML by filling in the template you read in Step 0:
- Replace {{DIGEST_TITLE}} with "Gaming Digest"
- Replace {{DATE}} with '"$TODAY"'
- Replace {{INTRO}} with a 2-3 sentence editorial intro highlighting the biggest stories of the day
- Replace {{FRESH_STORIES}} with story blocks using the story block template from the HTML comment
- Replace {{RECENT_STORIES}} with story blocks using the "Recent & Relevant" variant (with the WHY line)
- Each story needs a TITLE, URL, CATEGORY (e.g. Soulslike, Steam, Console, PC Hardware, Industry, Esports, Action, RPG, Indie), and SUMMARY

## Step 3: Write and send

IMPORTANT: write only inside /home/carter — writes outside it (e.g. /tmp) are blocked in headless mode.
1. Write the HTML body to /home/carter/digests/gaming-digest/.daily_digest.html
2. Send it by running this command with your Bash tool: python3 /home/carter/scripts/send_digest.py --subject "Gaming Digest — '"$TODAY"'" --body-file /home/carter/digests/gaming-digest/.daily_digest.html --to carter2099@pm.me
3. Clean up: rm /home/carter/digests/gaming-digest/.daily_digest.html

## Step 4: Write summary for future runs

Write a concise summary of what you sent to /home/carter/digests/gaming-digest/'"$TODAY"'.md in this format:

# Gaming Digest — '"$TODAY"'

## Fresh
- **Story title** — one-line summary
- **Story title** — one-line summary

## Recent & Relevant
- **Story title** — one-line summary (why still relevant)

Then delete any .md files in /home/carter/digests/gaming-digest/ older than 7 days.'

exec "$HOME/.opencode/bin/opencode" run -m opencode-go/minimax-m3 "$PROMPT" < /dev/null
