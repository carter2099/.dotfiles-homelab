---
name: hyperliquid-release
description: Release the Hyperliquid Ruby SDK — merges dev into main, bumps version, updates CHANGELOG, runs full test suite + integration tests, creates a git tag (triggers GitHub Release), and prompts for RubyGems MFA to push the gem.
---

# hyperliquid-release

Repo: `~/dev/hyperliquid`
Ruby: always use `RBENV_VERSION=3.4.8`

## Step 1: Confirm intent

Ask Carter: "Ready to release. What version bump — patch, minor, or major?" Wait for answer before proceeding.

## Step 2: Ensure dev is clean and up to date

```bash
cd ~/dev/hyperliquid
git checkout dev
git pull origin dev
git status
```

There must be no uncommitted changes. If there are, stop and ask Carter how to handle them.

## Step 3: Run full unit test suite on dev

```bash
cd ~/dev/hyperliquid
RBENV_VERSION=3.4.8 bundle exec rake
```

All tests must pass. Fix failures or abort — do not release with failing unit tests.

## Step 4: Run integration tests on dev

```bash
cd ~/dev/hyperliquid
source ~/.config/hyperliquid-agent/env
RBENV_VERSION=3.4.8 HYPERLIQUID_PRIVATE_KEY=$HYPERLIQUID_PRIVATE_KEY ruby scripts/test_automated.rb
```

All integration tests must pass. Investigate failures before proceeding.

## Step 5: Merge dev → main

```bash
cd ~/dev/hyperliquid
git checkout main
git pull origin main
git merge --no-ff dev -m "Merge dev into main for release"
```

If there are conflicts, resolve them and confirm with Carter before continuing.

## Step 6: Bump version

Read `lib/hyperliquid/version.rb` to see the current version. Calculate the new version based on Carter's answer in Step 1.

Edit `lib/hyperliquid/version.rb` to set the new version string.

## Step 7: Update CHANGELOG.md

Read the CHANGELOG.md. Prepend a new section at the top (below the title line) in the format:

```
## [X.Y.Z] - YYYY-MM-DD

- <summary of changes since last release, drawn from git log>
```

Use `git log <previous-tag>..HEAD --oneline` to get the list of commits. Write human-readable bullet points, not raw commit messages.

## Step 8: Commit version bump

```bash
cd ~/dev/hyperliquid
git add lib/hyperliquid/version.rb CHANGELOG.md
git commit -m "version to X.Y.Z"
```

## Step 9: Run tests one final time on main

```bash
RBENV_VERSION=3.4.8 bundle exec rake
```

Must pass. If anything broke in the merge, fix it now.

## Step 10: Push main and create tag

```bash
cd ~/dev/hyperliquid
git push origin main
git tag vX.Y.Z
git push origin vX.Y.Z
```

This triggers the GitHub Actions release workflow, which creates a GitHub Release from the CHANGELOG.

## Step 11: Push gem to RubyGems (requires MFA)

```bash
cd ~/dev/hyperliquid
RBENV_VERSION=3.4.8 bundle exec rake build
```

Then tell Carter: "Gem built. Run `gem push pkg/hyperliquid-X.Y.Z.gem` and enter your RubyGems OTP when prompted — or give me the OTP and I'll run it."

If Carter provides the OTP, run:
```bash
cd ~/dev/hyperliquid
RBENV_VERSION=3.4.8 gem push pkg/hyperliquid-X.Y.Z.gem
```
Enter the OTP when prompted.

## Step 12: Sync dev with main

```bash
cd ~/dev/hyperliquid
git checkout dev
git merge main
git push origin dev
```

## Step 13: Update state file

Edit `~/agent-state/hyperliquid-sdk.md`:
- Update **SDK version** to the new version.
- Note the release date in Run History.

## Step 14: Confirm

Tell Carter: "Released hyperliquid vX.Y.Z. GitHub Release created. Gem pushed to RubyGems." (or note if gem push was skipped.)
