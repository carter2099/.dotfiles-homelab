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
- All known gaps and their statuses (🔧 bugs, 🟡 queued, 🔴 needs_approval)
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

- **Python SDK**: Extract method signatures only — do NOT fetch full files:
  ```bash
  curl -s "https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/master/hyperliquid/info.py" | grep -E "^\s+def " | sed 's/^\s*//'
  curl -s "https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/master/hyperliquid/exchange.py" | grep -E "^\s+def " | sed 's/^\s*//'
  ```
  Compare against Ruby SDK signatures:
  ```bash
  grep -E "^\s+def " ~/dev/hyperliquid/lib/hyperliquid/info.rb
  grep -E "^\s+def " ~/dev/hyperliquid/lib/hyperliquid/exchange.rb
  ```
  For any gap you plan to implement this run, fetch the full upstream method body to understand its parameters and behaviour.

- **TS SDK (nktkas)**: The repo uses one file per method. List the method directories directly — no file fetching needed for the comparison pass:
  ```bash
  curl -s "https://api.github.com/repos/nktkas/hyperliquid/contents/src/api/info/_methods" \
    | python3 -c "import sys,json; [print(f['name'].replace('.ts','')) for f in json.load(sys.stdin)]"
  curl -s "https://api.github.com/repos/nktkas/hyperliquid/contents/src/api/exchange/_methods" \
    | python3 -c "import sys,json; [print(f['name'].replace('.ts','')) for f in json.load(sys.stdin)]"
  ```
  Compare the resulting method names against the Ruby SDK. For any gap you plan to implement, fetch the specific `.ts` file to understand parameters and return type.

- **HL API docs**: WebFetch the Hyperliquid GitBook docs for new endpoint types, new action types, new subscription channels.

For each gap found:
- If already in the state file, skip.
- If it's a new method/endpoint that fits the existing architecture (no new classes, no new deps), add it to Known Gaps as 🟡 queued.
- If it requires architectural changes (new signing scheme, new transport, new major dependency), add it as 🔴 needs_approval with a one-paragraph description of what's needed and why. Do NOT implement it — flag it and move on.

Update the upstream SHA and scan date in the state file for any source actually scanned.

## Step 4: Define scope for this run

From the state file, select gaps to implement this session. Apply these constraints:
- Max 3 gaps per run (session time budget).
- **Priority order**: 🔧 bugs first → approved architectural changes → oldest-queued 🟡 gaps → housekeeping todos.
- Skip anything marked 🔴 needs_approval that is not yet approved.
- If there is nothing to implement, skip to Step 9 (update state + email).

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

Before investigating any failures, cross-reference against the **Known Pre-existing Failures** section in the state file. If a failure matches a known pre-existing issue, note it in the email but do not spend tool calls re-investigating it. Only investigate genuinely new failures.

If a new failure is caused by this run's changes, fix before committing. If it's an unrelated flake, note it.

## Step 8: Sync CLAUDE.md if needed

Before staging the commit, decide whether `~/dev/hyperliquid/CLAUDE.md` needs updating. CLAUDE.md is the canonical source of truth for the repo and should stay current.

Update it whenever this run:
- Bumps the SDK version (the "currently vX.Y.Z" line).
- Adds a new pattern, transport, dependency, constant, or convention a future agent reading the repo cold would want to know (e.g. a new base URL, a new signing variant, a new test harness file).
- Changes how something documented in CLAUDE.md actually works (architecture, request flow, signing, numeric conversion, code style, CI matrix, release flow).
- Introduces a new gotcha worth preserving (the `dump_status` String-response guard is the canonical example).

Routine additions that fit cleanly into existing patterns (one more Info method, one more Exchange action that uses the existing signer) generally do **not** need a CLAUDE.md update. Skip it rather than churn the file.

If you do edit CLAUDE.md, include it in the same commit as the code change.

## Step 9: Commit and push

```bash
cd ~/dev/hyperliquid
git add lib/hyperliquid/info.rb spec/hyperliquid/info_spec.rb  # stage specific files (include CLAUDE.md if updated)
git commit -m "feat: <concise description of what was implemented>

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
git push origin dev
```

If nothing was implemented (no gaps or scope was zero), skip the commit.

## Step 10: Update state file

Edit `~/agent-state/hyperliquid-sdk.md`:
- Update **Last run** date and outcome.
- Update upstream SHA/scan dates for any sources scanned this run.
- Update gap statuses (🟡→✅, new gaps added, 🔧 bugs fixed, etc.).
- Append a row to the Run History table.

## Step 11: Email summary

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

## Step 12: Backup state file reminder

The state file is backed up by homelab-backup nightly. No action needed — just don't delete it.
