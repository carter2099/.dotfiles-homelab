---
name: llm-smoke-test
description: Run the local LLM smoke test suite against Qwen 3.6 35B Q8 on the gaming rig — benchmarks TPS, context recall, memory, and tool-calling eagerness. Use when user says "test the LLM", "smoke test Qwen", "benchmark the local model", or after changing llama-swap/llama.cpp config.
---

# LLM Smoke Test

Comprehensive smoke test for the local Qwen 3.6 35B Q8 model served via llama-swap on the gaming rig (192.168.4.103), accessed through llm-proxy on the homelab (localhost:8081).

## Architecture

```
Open WebUI / omp → llm-proxy:8081 (homelab) → llama-swap:8080 (gaming rig) → llama-server:5800+
```

Two model variants are registered in llama-swap config (`C:\llm\config.yaml` on gaming rig):

| Model ID | Alias | Thinking | Use |
|---|---|---|---|
| `qwen-3.6-35b-q8` | `qwen-3.6-35b-q8` | ON (budget 1024) | **General use** — default for most tasks |
| `qwen-3.6-35b-q8-fast` | `qwen-3.6-35b-q8-fast` | OFF | **Fallback** — when reasoning eats token budget or breaks tool calling |

Key config flags: `-c 262144` (256K ctx), `--n-cpu-moe 33`, `-t 8`, `--flash-attn on`, `--no-mmap`, `-ctk q4_0 -ctv q4_0`, `--cache-ram 2048`, `--prio 2`, `--temp 0.5 --top-k 20 --min-p 0.1`.

Switching between the tuned finalists takes roughly 8–32s in the measured model-swap test.

## Quick Run

```bash
# Full smoke test (thinking + no-thinking + context benchmark)
bash ~/scripts/smoke-test-llm.sh

# Custom models or benchmark file
bash ~/scripts/smoke-test-llm.sh qwen-3.6-35b-q8 qwen-3.6-35b-q8-fast ~/benchmarks/context-window/context_50_000.md
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
  omp -p --provider local-llm --model qwen-3.6-35b-q8-fast --api-key none

# Thinking (more verbose, may hallucinate details)
echo "What's the latest on the US team in the world cup?" | \
  omp -p --provider local-llm --model qwen-3.6-35b-q8 --api-key none
```

The test prompt should be a **current events question with no explicit search instruction** — the model must decide to search on its own. The user supplies the test case and correctness criteria interactively.

**Known behavior:** Both variants proactively use web_search for current events. Prefer the no-thinking entry for efficiency, but validate its source extraction and calendar interpretation; proactive search alone did not produce reliable dates in the current OMP evaluation.

## Memory Baseline (256K context under 200K load)

| Metric | Value |
|---|---|
| llama-server working set | ~29.68 GB |
| Whole-GPU VRAM used | 11,498 MiB of 12,227 MiB |
| Context / K/V | 262144, `q4_0` K and V |
| 200K recall | 10/10, 1027.21s, 208.69 prompt tok/s |

The K/V cache is lazily allocated. The full-context measurement, not idle startup memory,
is the relevant capacity check.

## Key Findings (as of 2026-08-05)

1. **`--reasoning-budget` is the critical tuning knob.** The thinking model is not broken — the budget was just too loose. At 4096, the model finishes reasoning naturally before the budget triggers, so it's effectively unlimited. At 1024, the budget triggers early, cuts reasoning cleanly, and the model transitions to content. **Optimal budget: 1024 tokens.** This allows enough thinking for complex tasks (bat+ball puzzle solved correctly) while forcing transition on simple Qs and long-context tasks.

    Budget reference:
    - 100: tight — good for simple Q&A, may cut off complex reasoning
    - 512: balanced — works for bat+ball level puzzles, slight cutoff
    - **1024: recommended** — room for multi-step reasoning, triggers before max_tokens runs out
    - 4096: too loose — effectively unlimited, model burns all tokens on reasoning

    Client max_tokens must still be ≥ budget + answer space. Recommended: max_tokens ≥ 2048 for thinking model.

2. **No-thinking model is 96% more token-efficient** for simple Q&A (8 tokens vs 191). Use for chat, facts, context recall, and tool use.

3. **Qwen is eager enough with tool calls, but tool use is not correctness.** Both variants proactively use web_search without explicit prompting. In the Aug 5 five-run OMP golf evaluation, the no-thinking model called tools in 5/5 runs but resolved the required date window correctly in 0/5; advisor feedback sometimes made the final answer worse. No tuning is needed merely to trigger search, but ambiguous calendar prompts need explicit date resolution and sourced verification.

4. **System prompts can't reduce reasoning verbosity.** Tested "be brief" prompts — they made it worse (model reasons about being brief). The only control is binary: thinking on or off.

5. **256K is validated with the tuned placement.** Qwen Q8 uses 33 CPU MoE layers and a `q4_0` K/V cache. It recalled 10/10 planted facts from a 201,031-token prompt in 1027.21s without OOM. The earlier 128K ceiling applied to the old all-GPU/Q8-cache placement.

6. **The proxy must not impose an absolute response write deadline.** A 1002.62s 200K request completed on llama.cpp but was disconnected by llm-proxy's former 10-minute `WriteTimeout`. Commit `8eae500` disables that deadline; after release, a fresh 201,031-token run returned through the deployed proxy in 970.91s with HTTP 200, no fallback, and 10/10 recall.

7. **Current warm API baseline:** 1.71s TTFT and 29.73 generation tok/s across five runs. Measured model-swap TTFT is 33.06s. A two-slot 128K smoke test completed both simultaneous requests at 45.16 aggregate tok/s; production stays at one 256K slot.

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
- `~/benchmarks/local-llm-research-2026-08-05/` — full five-model quant/context, performance, concurrency, recall, and OMP quality corpus
