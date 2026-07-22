---
name: llm-smoke-test
description: Run the local LLM smoke test suite against Qwen Q6 on the gaming rig — benchmarks TPS, context recall, memory, and tool-calling eagerness. Use when user says "test the LLM", "smoke test Qwen", "benchmark the local model", or after changing llama-swap/llama.cpp config.
---

# LLM Smoke Test

Comprehensive smoke test for the local Qwen Q6 model served via llama-swap on the gaming rig (192.168.4.103), accessed through llm-proxy on the homelab (localhost:8081).

## Architecture

```
Open WebUI / omp → llm-proxy:8081 (homelab) → llama-swap:8080 (gaming rig) → llama-server:5800+
```

Two model variants are registered in llama-swap config (`C:\llm\config.yaml` on gaming rig):

| Model ID | Alias | Thinking | Use |
|---|---|---|---|
| `qwen-3.6-35b-q6` | `qwen-3.6-35b-q6` | ON (budget 1024) | **General use** — default for most tasks |
| `qwen-3.6-35b-q6-fast` | `qwen-3.6-35b-q6-fast` | OFF | **Fallback** — when reasoning eats token budget or breaks tool calling |

Key config flags: `-c 131072` (128K ctx), `-t 8`, `--flash-attn on`, `--no-mmap`, `-ctk q8_0 -ctv q8_0`, `--cache-ram 2048`, `--prio 2`, `--temp 0.5 --top-k 20 --min-p 0.1`.

Switching models triggers a ~60s reload (unload one, load the other).

## Quick Run

```bash
# Full smoke test (thinking + no-thinking + context benchmark)
bash ~/scripts/smoke-test-llm.sh

# Custom models or benchmark file
bash ~/scripts/smoke-test-llm.sh qwen-3.6-35b-q6 qwen-3.6-35b-q6-fast ~/benchmarks/context-window/context_50_000.md
```

## What the Smoke Test Measures

### Test 1 — Thinking model Q&A
Sends "What is the capital of France?" with max_tokens=4096.
- **Pass:** Content produced, finish_reason=stop, TPS ≥ 5
- **Fail:** Empty content (model used all tokens on reasoning)

### Test 2 — No-thinking model Q&A
Same prompt with `--reasoning off` variant. Triggers model swap (~60s).
- **Pass:** Content produced, 0 reasoning tokens, 90%+ token savings vs thinking
- **Warn:** If reasoning tokens still appear (--reasoning off isn't working)

### Test 3 — Context Window Recall
Runs `~/benchmarks/context-window/context_20_000.md` (default, ~20K tokens of interview transcript with 10 planted facts) against the no-thinking model.
- **Pass:** ≥ 8/10 recall, finish_reason=stop
- **Fail:** < 5/10 recall or truncated (finish_reason=length)

Larger benchmarks available: `context_50_000.md`, `context_100_000.md`, `context_200_000.md`. Pass as 3rd argument.

### Test 4 (Manual) — Tool-Calling Eagerness
Run through omp to test if the model proactively uses web_search:

```bash
# No-thinking (recommended — more accurate with search results)
echo "What's the latest on the US team in the world cup?" | \
  omp -p --provider local-llm --model qwen-3.6-35b-q6-fast --api-key none

# Thinking (more verbose, may hallucinate details)
echo "What's the latest on the US team in the world cup?" | \
  omp -p --provider local-llm --model qwen-3.6-35b-q6 --api-key none
```

The test prompt should be a **current events question with no explicit search instruction** — the model must decide to search on its own. The user supplies the test case and correctness criteria interactively.

**Known behavior:** Both models proactively use web_search for current events. The no-thinking model produces cleaner, more accurate results. The thinking model sometimes overcomplicates and hallucinates details.

## Memory Baseline (128K context, idle)

| Metric | Value |
|---|---|
| Free RAM | ~2.2 GB (of 32 GB) |
| VRAM used | ~6.1 GB (of 12.2 GB) |
| Context | 131072 (128K) |

Context may be lazily allocated — memory grows as context fills.

## Key Findings (as of 2026-07-05)

1. **`--reasoning-budget` is the critical tuning knob.** The thinking model is not broken — the budget was just too loose. At 4096, the model finishes reasoning naturally before the budget triggers, so it's effectively unlimited. At 1024, the budget triggers early, cuts reasoning cleanly, and the model transitions to content. **Optimal budget: 1024 tokens.** This allows enough thinking for complex tasks (bat+ball puzzle solved correctly) while forcing transition on simple Qs and long-context tasks.

    Budget reference:
    - 100: tight — good for simple Q&A, may cut off complex reasoning
    - 512: balanced — works for bat+ball level puzzles, slight cutoff
    - **1024: recommended** — room for multi-step reasoning, triggers before max_tokens runs out
    - 4096: too loose — effectively unlimited, model burns all tokens on reasoning

    Client max_tokens must still be ≥ budget + answer space. Recommended: max_tokens ≥ 2048 for thinking model.

2. **No-thinking model is 96% more token-efficient** for simple Q&A (8 tokens vs 191). Use for chat, facts, context recall, and tool use.

3. **Qwen is eager enough with tool calls.** Both variants proactively use web_search without explicit prompting. No tuning needed — behavior is comparable to DeepSeek. If more aggressiveness is desired, use a system prompt ("Always search before answering factual questions") or the `tool_choice` API parameter (`{"type": "any"}` forces a tool call on every message — overkill for normal chat). llama.cpp supports `tool_choice` but omp doesn't expose it directly in config.

4. **System prompts can't reduce reasoning verbosity.** Tested "be brief" prompts — they made it worse (model reasons about being brief). The only control is binary: thinking on or off.

5. **128K context is the safe ceiling.** 256K was borderline — prompt processing at 200K+ caused OOM on 32 GB RAM. 128K leaves comfortable headroom for both variants.

6. **Thinking model works at 100K with budget=1024.** At budget=1024, the thinking model scores 10/10 on 100K context recall (~9 min). At budget=4096, it fails 0/10 (burns all tokens on reasoning). The budget knob is make-or-break.

7. **100K context benchmark reference:** No-thinking scores 10/10 in ~8 min. Thinking scores 10/10 in ~9 min. Prompt processing dominates, generation is fast.

## Restarting llama-swap

If llama-swap dies or config changes need a restart:

```bash
# Kill existing
ssh gamingrig 'taskkill /f /im llama-swap.exe'

# Start (must use wmic for background process via SSH)
ssh gamingrig 'wmic process call create "C:\llm\llama-swap.exe --config C:\llm\config.yaml --listen 0.0.0.0:8080"'

# Verify
ssh gamingrig 'powershell -Command "Get-Process llama-swap | Select-Object Id,StartTime"'
```

Config pushed from homelab via:
```bash
# Edit /tmp/llama-swap-config.yaml, then:
scp /tmp/llama-swap-config.yaml gamingrig:C:/llm/config.yaml
# Then restart llama-swap
```

## Related Files

- `~/scripts/smoke-test-llm.sh` — the smoke test script
- `~/benchmarks/context-window/` — context recall benchmarks (20K, 50K, 100K, 200K tokens)
- `~/benchmarks/context-window/README.md` — benchmark methodology and scoring
- `/tmp/llama-swap-config.yaml` — working copy of gaming rig config (push via scp)
- `~/dev/llm-proxy/` — llm-proxy source (runs on homelab, routes to gaming rig)
