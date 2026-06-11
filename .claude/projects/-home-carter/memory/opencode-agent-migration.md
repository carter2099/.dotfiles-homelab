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
- ⬜ **Remote-control replacement** — swap the `claude remote-control` mobile daemon (`claude-homelab.service`) for `opencode serve` (web UI) reached over WireGuard or the existing Cloudflare tunnel. Not started; additive (keep Claude daemon running in parallel).
- ⬜ **hyperliquid-run** daily dev agent → Qwen 3.7 Max. Do a full-scale test run to gauge quality + budget before retiring the Claude version.
- ⬜ **dependabot-webhook** spawned agent → opencode. Not started.

**Spike findings worth remembering:** opencode's WebFetch reaches sites Claude's WebFetch blocks (e.g. arstechnica); MiniMax M3 sometimes *constructs/guesses* dated wire-service URLs (the fetch-or-skip rule catches them via 404); `gamespot.com` bot-blocks (403). My own WebSearch proved unreliable as "ground truth" for fresh news vs. the models' live fetches — verify news claims by fetching cited URLs, not by cross-checking a search summary.

**Why:** Vendor-lock-in avoidance for the agentic homelab; opencode makes provider/model a one-string swap. **How to apply:** When touching any homelab automation agent, default to the opencode pattern and the tier mapping above; consult CLAUDE.md "Email Digests" for the headless invocation gotchas before writing a new headless `opencode run` script.
