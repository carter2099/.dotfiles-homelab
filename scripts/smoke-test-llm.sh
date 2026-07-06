#!/bin/bash
# Smoke test for local LLM (Qwen Q6 via llm-proxy → gaming rig)
# Measures: content production, TPS, context recall, memory/VRAM
# Usage: bash ~/scripts/smoke-test-llm.sh [model_id]
#
# Tests:
#   1. Thinking model — simple Q&A (verify content + TPS)
#   2. No-thinking model — simple Q&A (verify content + TPS)
#   3. Context window benchmark — 20K file (no-thinking model)
#   4. Memory usage

set -euo pipefail

MODEL_THINK="${1:-qwen3.6-35b-q6}"
MODEL_NOTHINK="${2:-qwen3.6-35b-q6-fast}"
ENDPOINT="http://localhost:8081/v1/chat/completions"
TIMEOUT=300
BENCHMARK_FILE="${3:-$HOME/benchmarks/context-window/context_20_000.md}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
pass_count=0; fail_count=0
pass() { echo -e "${GREEN}[PASS]${NC} $1"; pass_count=$((pass_count + 1)); }
fail() { echo -e "${RED}[FAIL]${NC} $1"; fail_count=$((fail_count + 1)); }
info() { echo -e "${CYAN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

echo "============================================"
echo "  LLM Smoke Test"
echo "  Thinking:  $MODEL_THINK"
echo "  No-think:  $MODEL_NOTHINK"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================"
echo ""

# ── Helper: run a single API call and return metrics ──────────────
run_test() {
    local model="$1" prompt="$2" max_tok="${3:-4096}" temp="${4:-0.3}"
    
    START=$(date +%s.%N)
    RESPONSE=$(curl -s --max-time "$TIMEOUT" "$ENDPOINT" \
      -H "Content-Type: application/json" \
      -d "$(python3 -c "
import json, sys
print(json.dumps({
    'model': '$model',
    'messages': [{'role': 'user', 'content': sys.argv[1]}],
    'max_tokens': $max_tok,
    'temperature': $temp,
    'stream': False
}))
" "$prompt")" 2>&1)
    END=$(date +%s.%N)
    ELAPSED=$(echo "$END - $START" | bc)
    
    python3 -c "
import json, sys
d = json.loads(sys.argv[1])
msg = d.get('choices', [{}])[0].get('message', {})
content = msg.get('content', '') or ''
reasoning = msg.get('reasoning_content', '') or ''
usage = d.get('usage', {})
finish = d.get('choices', [{}])[0].get('finish_reason', '?')
print(f'COMPLETION_TOKENS={usage.get(\"completion_tokens\", \"?\")}')
print(f'PROMPT_TOKENS={usage.get(\"prompt_tokens\", \"?\")}')
print(f'CONTENT_LEN={len(content)}')
print(f'REASONING_LEN={len(reasoning)}')
print(f'FINISH_REASON={finish}')
print(f'CONTENT={content[:300]}')
print(f'REASONING_TAIL={reasoning[-200:]}')
" "$RESPONSE" 2>&1
    echo "ELAPSED=${ELAPSED}"
}

# ── Pre-test: health check ────────────────────────────────────────
info "Checking llm-proxy health..."
HEALTH=$(curl -s --max-time 10 "http://localhost:8081/health" 2>&1 || echo "FAIL")
if echo "$HEALTH" | grep -q '"status":"healthy"'; then
    pass "llm-proxy reachable"
elif echo "$HEALTH" | grep -qi "loading\|unavailable"; then
    warn "llm-proxy reports model loading — waiting up to 60s..."
    for i in $(seq 1 12); do
        sleep 5
        HEALTH=$(curl -s --max-time 10 "http://localhost:8081/health" 2>&1 || echo "FAIL")
        if echo "$HEALTH" | grep -q '"status":"healthy"'; then
            pass "llm-proxy ready after ${i}x5s wait"; break
        fi
        [ "$i" -eq 12 ] && { fail "llm-proxy not ready after 60s"; exit 1; }
    done
else
    fail "llm-proxy health check failed: $HEALTH"; exit 1
fi

# ── Pre-test: memory snapshot ─────────────────────────────────────
info ""
info "── Pre-test Memory ──"
MEM_BEFORE=$(ssh gamingrig 'wmic OS get TotalVisibleMemorySize,FreePhysicalMemory /value' 2>&1 | grep -v '^$' | tr '\r' ' ')
VRAM_BEFORE=$(ssh gamingrig 'powershell -Command "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits"' 2>&1 | tr '\r' ' ')
echo "  RAM: $MEM_BEFORE"
echo "  VRAM: $VRAM_BEFORE MB (used,total)"

# ── Test 1: Thinking model — simple Q&A ───────────────────────────
info ""
info "═══════════════════════════════════════════"
info "  Test 1: Thinking model — simple Q&A"
info "═══════════════════════════════════════════"

RESULT1=$(run_test "$MODEL_THINK" "What is the capital of France? Reply in one sentence." 4096 0.3)
echo "$RESULT1" | grep -E "^(COMPLETION|PROMPT|CONTENT_LEN|REASONING_LEN|FINISH|CONTENT=|ELAPSED)" | while read line; do info "  $line"; done

C_TOK1=$(echo "$RESULT1" | grep "^COMPLETION_TOKENS=" | cut -d= -f2)
C_LEN1=$(echo "$RESULT1" | grep "^CONTENT_LEN=" | cut -d= -f2)
R_LEN1=$(echo "$RESULT1" | grep "^REASONING_LEN=" | cut -d= -f2)
FINISH1=$(echo "$RESULT1" | grep "^FINISH_REASON=" | cut -d= -f2)
ELAPSED1=$(echo "$RESULT1" | grep "^ELAPSED=" | cut -d= -f2)

if [ "$C_LEN1" -gt 0 ] 2>/dev/null; then
    pass "Content produced (${C_LEN1} chars)"
else
    fail "No content produced — thinking model failed to generate answer"
fi

if [ "$FINISH1" = "stop" ]; then
    pass "Natural stop (not truncated)"
elif [ "$FINISH1" = "length" ]; then
    fail "Truncated (finish_reason=length) — increase max_tokens"
fi

if [ -n "$C_TOK1" ] && [ "$C_TOK1" != "?" ] && [ -n "$ELAPSED1" ]; then
    TPS1=$(echo "scale=1; $C_TOK1 / $ELAPSED1" | bc)
    info "  TPS: ${TPS1} tok/s, Reasoning: ${R_LEN1:-0} chars"
    if [ "$(echo "$TPS1 >= 5" | bc -l)" -eq 1 ] 2>/dev/null; then
        pass "TPS >= 5 (${TPS1})"
    else
        warn "TPS low: ${TPS1} (threshold: 5)"
    fi
fi

# ── Test 2: No-thinking model — simple Q&A ────────────────────────
info ""
info "═══════════════════════════════════════════"
info "  Test 2: No-thinking model — simple Q&A"
info "═══════════════════════════════════════════"

# Trigger model swap (kill current, let no-think model load)
info "Switching to no-thinking model (this may take ~60s for model swap)..."
ssh gamingrig 'taskkill /f /im llama-server.exe' 2>&1 | grep -v "^$" || true
sleep 3

RESULT2=$(run_test "$MODEL_NOTHINK" "What is the capital of France? Reply in one sentence." 4096 0.3)
echo "$RESULT2" | grep -E "^(COMPLETION|PROMPT|CONTENT_LEN|REASONING_LEN|FINISH|CONTENT=|ELAPSED)" | while read line; do info "  $line"; done

C_TOK2=$(echo "$RESULT2" | grep "^COMPLETION_TOKENS=" | cut -d= -f2)
C_LEN2=$(echo "$RESULT2" | grep "^CONTENT_LEN=" | cut -d= -f2)
R_LEN2=$(echo "$RESULT2" | grep "^REASONING_LEN=" | cut -d= -f2)
FINISH2=$(echo "$RESULT2" | grep "^FINISH_REASON=" | cut -d= -f2)
ELAPSED2=$(echo "$RESULT2" | grep "^ELAPSED=" | cut -d= -f2)

if [ "$C_LEN2" -gt 0 ] 2>/dev/null; then
    pass "Content produced (${C_LEN2} chars)"
else
    fail "No content produced"
fi

if [ -n "$R_LEN2" ] && [ "$R_LEN2" != "0" ] 2>/dev/null; then
    warn "No-thinking model still produced reasoning (${R_LEN2} chars) — --reasoning off may not be working"
fi

if [ "$FINISH2" = "stop" ]; then
    pass "Natural stop"
fi

if [ -n "$C_TOK2" ] && [ "$C_TOK2" != "?" ] && [ -n "$ELAPSED2" ]; then
    TPS2=$(echo "scale=1; $C_TOK2 / $ELAPSED2" | bc)
    info "  TPS: ${TPS2} tok/s, Reasoning: ${R_LEN2:-0} chars"
    if [ "$(echo "$TPS2 >= 5" | bc -l)" -eq 1 ] 2>/dev/null; then
        pass "TPS >= 5 (${TPS2})"
    else
        warn "TPS low: ${TPS2}"
    fi
fi

# Compare thinking vs no-thinking token efficiency
if [ -n "$C_TOK1" ] && [ -n "$C_TOK2" ] && [ "$C_TOK1" != "?" ] && [ "$C_TOK2" != "?" ]; then
    SAVINGS=$(echo "scale=0; 100 - ($C_TOK2 * 100 / $C_TOK1)" | bc 2>/dev/null || echo "?")
    info "  Token savings vs thinking model: ~${SAVINGS}%"
fi

# ── Test 3: Context window benchmark ──────────────────────────────
info ""
info "═══════════════════════════════════════════"
info "  Test 3: Context window — $(basename $BENCHMARK_FILE)"
info "  (using no-thinking model)"
info "═══════════════════════════════════════════"

if [ ! -f "$BENCHMARK_FILE" ]; then
    warn "Benchmark file not found: $BENCHMARK_FILE — skipping"
else
    BENCH_CONTENT=$(cat "$BENCHMARK_FILE")
    FILE_KB=$(echo "scale=0; $(wc -c < "$BENCHMARK_FILE") / 1024" | bc)
    info "File size: ${FILE_KB} KB"
    
    START3=$(date +%s.%N)
    RESPONSE3=$(curl -s --max-time "$TIMEOUT" "$ENDPOINT" \
      -H "Content-Type: application/json" \
      -d "$(python3 -c "
import json, sys
print(json.dumps({
    'model': '$MODEL_NOTHINK',
    'messages': [{'role': 'user', 'content': sys.stdin.read()}],
    'max_tokens': 2048,
    'temperature': 0.1,
    'stream': False
}))
" <<< "$BENCH_CONTENT")" 2>&1)
    END3=$(date +%s.%N)
    ELAPSED3=$(echo "$END3 - $START3" | bc)
    
    RESULT3=$(python3 -c "
import json, sys
d = json.loads(sys.argv[1])
msg = d.get('choices', [{}])[0].get('message', {})
content = msg.get('content', '') or ''
usage = d.get('usage', {})
finish = d.get('choices', [{}])[0].get('finish_reason', '?')
print(f'COMPLETION_TOKENS={usage.get(\"completion_tokens\", \"?\")}')
print(f'PROMPT_TOKENS={usage.get(\"prompt_tokens\", \"?\")}')
print(f'FINISH_REASON={finish}')
print('---ANSWER---')
print(content)
print('---END---')
" "$RESPONSE3" 2>&1)
    
    C_TOK3=$(echo "$RESULT3" | grep "^COMPLETION_TOKENS=" | cut -d= -f2)
    P_TOK3=$(echo "$RESULT3" | grep "^PROMPT_TOKENS=" | cut -d= -f2)
    FINISH3=$(echo "$RESULT3" | grep "^FINISH_REASON=" | cut -d= -f2)
    ANSWER=$(echo "$RESULT3" | sed -n '/^---ANSWER---$/,/^---END---$/p' | sed '1d;$d')
    
    # Score: keyword match against expected answers
    SCORE=0
    for check in "Portland" "age 7" "Dune" "2019" "Daily Grind" "Maple" "March 14" "2 years" "Thai green curry" "Queenstown"; do
        if echo "$ANSWER" | grep -qi "$check"; then
            SCORE=$((SCORE + 1))
        fi
    done
    
    if [ -n "$C_TOK3" ] && [ "$C_TOK3" != "?" ]; then
        TPS3=$(echo "scale=1; $C_TOK3 / $ELAPSED3" | bc)
        info "  ${C_TOK3} comp tokens / ${P_TOK3} prompt tokens in ${ELAPSED3}s = ${TPS3} tok/s"
        info "  Context recall: ${SCORE}/10 correct, Finish: ${FINISH3}"
        
        if [ "$SCORE" -ge 8 ]; then
            pass "Context recall >= 8/10 (${SCORE}/10)"
        elif [ "$SCORE" -ge 5 ]; then
            warn "Context recall moderate: ${SCORE}/10"
        else
            fail "Context recall poor: ${SCORE}/10"
        fi
        
        if [ "$FINISH3" = "stop" ]; then
            pass "Natural stop on benchmark"
        elif [ "$FINISH3" = "length" ]; then
            warn "Hit max_tokens on benchmark — answers may be truncated"
        fi
    else
        fail "No completion tokens in benchmark"
    fi
fi

# ── Post-test: memory ─────────────────────────────────────────────
info ""
info "── Post-test Memory ──"
MEM_AFTER=$(ssh gamingrig 'wmic OS get TotalVisibleMemorySize,FreePhysicalMemory /value' 2>&1 | grep -v '^$' | tr '\r' ' ')
VRAM_AFTER=$(ssh gamingrig 'powershell -Command "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits"' 2>&1 | tr '\r' ' ')
echo "  RAM: $MEM_AFTER"
echo "  VRAM: $VRAM_AFTER MB (used,total)"

BEFORE_FREE=$(echo "$MEM_BEFORE" | grep -oP 'FreePhysicalMemory=\K[0-9]+' || echo "0")
AFTER_FREE=$(echo "$MEM_AFTER" | grep -oP 'FreePhysicalMemory=\K[0-9]+' || echo "0")
if [ "$BEFORE_FREE" != "0" ] && [ "$AFTER_FREE" != "0" ]; then
    AFTER_GB=$(echo "scale=1; $AFTER_FREE / 1024 / 1024" | bc)
    info "Free RAM: ${AFTER_GB} GB"
    if [ "$(echo "$AFTER_GB < 2" | bc -l)" -eq 1 ] 2>/dev/null; then
        fail "Less than 2 GB free RAM — OOM risk"
    else
        pass "RAM headroom OK (${AFTER_GB} GB free)"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  Results: ${pass_count} passed, ${fail_count} failed"
echo "============================================"

if [ "$fail_count" -eq 0 ]; then
    echo -e "${GREEN}  TIER 1 PASSED ✓${NC}"
    echo ""
    echo "  Next: Tier 2 — world digest with Qwen Q6"
    exit 0
else
    echo -e "${RED}  TIER 1 FAILED ✗${NC}"
    echo ""
    echo "  Fix failures above before Tier 2."
    exit 1
fi
