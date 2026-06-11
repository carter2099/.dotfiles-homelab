---
name: opencode-agent-migration
description: "Migrating the homelab's headless/automation agents off Claude onto opencode (provider-agnostic). Digests done 2026-06-11; remote-control + hyperliquid/dependabot still pending."
metadata: 
  node_type: memory
  type: project
  originSessionId: 9539a445-723e-4f19-a99f-f3ef562f0b1a
---

Carter is moving the homelab's **automation/headless agents** off Claude onto **opencode** (sst/opencode) to avoid lock-in to any single vendor. He is **keeping Claude for interactive/web use** (his Claude subscription) — this migration is *only* the unattended agents, not how he works with Claude day to day.

**Provider/billing:** the **OpenCode Go** subscription (~$10/mo flat; dollar-equivalent caps $12/5hr, $30/wk, $60/mo). Chosen over ChatGPT/Claude/Copilot subscriptions because those gate unattended automation as personal-use-only or abuse-flagged, whereas Go is multi-model so even the billing layer isn't locked to one vendor. Auth is an API key in `~/.local/share/opencode/auth.json` (OpenCode Zen is also configured). Binary: `~/.opencode/bin/opencode`.

**Model tier mapping (decided 2026-06-11):**
- **Digests → MiniMax M3** (`opencode-go/minimax-m3`) — ~$0.05/run, accurate, good news judgment.
- **Heavy coding (e.g. hyperliquid-run) → Qwen 3.7 Max** (`opencode-go/qwen3.7-max`) — proven on a write→test→run loop.
- Kimi K2.6 in reserve (rigorous self-verification, but verbose/pricier). Pin per-agent via `-m` / opencode's `model` field.

**Status:**
- ✅ **All 4 email digests cut over 2026-06-11** (dotfiles commit c42547f). As-built + headless gotchas are documented in CLAUDE.md "Email Digests".
- ✅ **hyperliquid-run cut over to Qwen 3.7 Max 2026-06-11** — validated with a full live run: implemented 2 SDK methods, 518/518 unit + 13/13 integration specs pass, pushed `df52f21` to dev. ~$0.13, ~9.5 min. Script runs `opencode run -m opencode-go/qwen3.7-max --dir $HOME` (the `--dir $HOME` is required so the agent can write across `~/dev/hyperliquid`, `~/agent-state`, `~/.config`). Also fixed a latent `send_digest.py --body` → `--body-file` bug in SKILL.md.
- ✅ **dependabot-webhook cut over to opencode + Qwen 3.7 Max 2026-06-11** — `main.go` spawns `opencode run` (dependabot-webhook repo commit 844d14d). The Claude allow/deny sandbox is reproduced as `~/.config/dependabot-webhook/opencode.json` (default-deny bash floor + git/bundle/gh/rake allowlist + sudo/docker/rm/curl denies), loaded via the `OPENCODE_CONFIG` env var set in `main.go`. **Verified headless deny enforcement** end-to-end (battery test + a signed test webhook against carter2099/hub that cloned, listed PRs, found none, stopped cleanly). Key fact: headless `opencode run` enforces `deny` (drops the bash tool entirely on full-deny; blocks non-allowlisted commands incl. non-`main` git push). `OPENCODE_CONFIG` env var is how to scope a per-invocation permission config.
- ⬜ **Remote-control replacement** — swap the `claude remote-control` mobile daemon (`claude-homelab.service`) for `opencode serve` (web UI) reached over WireGuard or the existing Cloudflare tunnel. Not started; additive (keep Claude daemon running in parallel). This is the only remaining piece.

**Spike findings worth remembering:** opencode's WebFetch reaches sites Claude's WebFetch blocks (e.g. arstechnica); MiniMax M3 sometimes *constructs/guesses* dated wire-service URLs (the fetch-or-skip rule catches them via 404); `gamespot.com` bot-blocks (403). My own WebSearch proved unreliable as "ground truth" for fresh news vs. the models' live fetches — verify news claims by fetching cited URLs, not by cross-checking a search summary.

**Why:** Vendor-lock-in avoidance for the agentic homelab; opencode makes provider/model a one-string swap. **How to apply:** When touching any homelab automation agent, default to the opencode pattern and the tier mapping above; consult CLAUDE.md "Email Digests" for the headless invocation gotchas before writing a new headless `opencode run` script.
