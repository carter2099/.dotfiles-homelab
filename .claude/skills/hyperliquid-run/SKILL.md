---
name: hyperliquid-run
description: Autonomous 6h development run for the Hyperliquid Ruby SDK — reads state file, scans upstream refs for API gaps, implements a fixed scope of changes with tests, runs test suite, commits to dev branch, updates state, and emails a progress summary.
---

# hyperliquid-run

Repo: `~/dev/hyperliquid` (dev branch)
State file: `~/agent-state/hyperliquid-sdk.md`
Private key: `~/.config/hyperliquid-agent/env`
Ruby: always use `RBENV_VERSION=3.4.8`

## Step 1: Read state

Read `~/agent-state/hyperliquid-sdk.md` in full. Note:
- Current SDK version
- Last run date and outcome
- Upstream reference SHAs (Python SDK, TS SDK, docs)
- All known gaps and their statuses
- Any approved architectural changes ready to implement
- Todos/housekeeping items

## Step 2: Ensure on dev branch and up to date

```bash
cd ~/dev/hyperliquid
git checkout dev
git pull origin dev
RBENV_VERSION=3.4.8 bundle install --quiet
```

## Step 3: Scan upstream references (skip if SHA unchanged)

For each upstream source, fetch the current HEAD SHA via GitHub API:

```bash
curl -s "https://api.github.com/repos/hyperliquid-dex/hyperliquid-python-sdk/commits/master" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['sha'][:12], d['commit']['committer']['date'][:10])"
curl -s "https://api.github.com/repos/nktkas/hyperliquid/commits/main" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['sha'][:12], d['commit']['committer']['date'][:10])"
```

Compare to the SHAs in the state file. For any source whose SHA has changed (or was never scanned):

- **Python SDK**: Fetch key source files via GitHub raw URLs. Focus on `hyperliquid/info.py`, `hyperliquid/exchange.py`. Look for methods/endpoints not present in `lib/hyperliquid/info.rb` and `lib/hyperliquid/exchange.rb`.
- **TS SDK (nktkas)**: Fetch `src/clients/public.ts`, `src/clients/wallet.ts`. Same comparison.
- **HL API docs**: WebFetch the Hyperliquid GitBook docs for new endpoint types, new action types, new subscription channels.

For each gap found:
- If already in the state file, skip.
- If it's a new method/endpoint that fits the existing architecture (no new classes, no new deps), add it to Known Gaps as 🟡 queued.
- If it requires architectural changes (new signing scheme, new transport, new major dependency), add it as 🔴 needs_approval with a one-paragraph description of what's needed and why. Do NOT implement it — flag it and move on.

Update the upstream SHA and scan date in the state file for any source actually scanned.

## Step 4: Define scope for this run

From the state file, select gaps to implement this session. Apply these constraints:
- Max 3 gaps per run (session time budget).
- Prioritise: approved architectural changes first, then oldest-queued 🟡 gaps, then housekeeping todos.
- Skip anything marked 🔴 needs_approval that is not yet approved.
- If there is nothing to implement, skip to Step 8 (update state + email).

Write a brief scope summary (1–3 bullet points) to refer back to during the run.

## Step 5: Implement

For each gap in scope:
1. Read the relevant source files before editing. Understand the existing pattern.
2. Implement the method/feature in the appropriate file (`lib/hyperliquid/info.rb`, `lib/hyperliquid/exchange.rb`, `lib/hyperliquid/ws/`, etc.), following existing code style.
3. Write a unit test in `spec/` mirroring the existing test structure (WebMock stubs for HTTP methods, no live calls in unit tests).
4. Run the single spec file to verify before moving on:
   ```bash
   cd ~/dev/hyperliquid && RBENV_VERSION=3.4.8 bundle exec rspec spec/path/to/new_spec.rb
   ```
5. Mark the gap 🔵 in_progress in the state file, then ✅ done once the test passes.

Do not implement more than the defined scope even if time seems available — stay within the session budget.

## Step 6: Run full test suite

```bash
cd ~/dev/hyperliquid
RBENV_VERSION=3.4.8 bundle exec rake
```

Fix any failures before continuing. If a failure is unrelated to this run's changes, note it in the state file and email summary but do not block the commit.

## Step 7: Run integration tests

Load the private key and run the automated integration suite:

```bash
cd ~/dev/hyperliquid
source ~/.config/hyperliquid-agent/env
RBENV_VERSION=3.4.8 HYPERLIQUID_PRIVATE_KEY=$HYPERLIQUID_PRIVATE_KEY ruby scripts/test_automated.rb
```

If integration tests fail:
- Investigate the failure. If it's caused by this run's changes, fix before committing.
- If it's a pre-existing testnet flake (oracle price error, network timeout), note it in the email but do not block the commit.

## Step 8: Commit and push

```bash
cd ~/dev/hyperliquid
git add -p   # stage thoughtfully — do not stage test_automated.rb separately, it should be committed
git commit -m "feat: <concise description of what was implemented>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push origin dev
```

If nothing was implemented (no gaps or scope was zero), skip the commit.

## Step 9: Update state file

Edit `~/agent-state/hyperliquid-sdk.md`:
- Update **Last run** date and outcome.
- Update upstream SHA/scan dates for any sources scanned this run.
- Update gap statuses (🟡→✅, new gaps added, etc.).
- Append a row to the Run History table.

## Step 10: Email summary

Send an email to carter2099@pm.me with subject `Hyperliquid SDK run — <date>`.

Use the `send_digest.py` script pattern:

```bash
python3 ~/scripts/send_digest.py \
  --to carter2099@pm.me \
  --subject "Hyperliquid SDK run — $(date +%Y-%m-%d)" \
  --body "<html body>"
```

Email body should include:
- **What was implemented** this run (or "nothing new to implement" if scope was empty)
- **New gaps discovered** (if any), with a note if any need approval
- **Test results** (unit + integration pass/fail summary)
- **Anything needing manual action** (needs_approval items, test failures requiring investigation)
- **Next run preview** — what's queued for next time

Keep it concise. Carter reads these on mobile.

## Step 11: Backup state file reminder

The state file is backed up by homelab-backup nightly. No action needed — just don't delete it.
