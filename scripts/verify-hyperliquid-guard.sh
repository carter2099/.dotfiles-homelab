#!/usr/bin/env bash
# Deterministic verification for the shipped Hyperliquid Dependabot guard.
# Usage: verify-hyperliquid-guard.sh [fast|full]
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
GUARD_PATH="${HYPERLIQUID_GUARD_PATH:-/home/carter/.config/hyperliquid-agent/omp-dependabot-guard.ts}"
TEST_PATH="$SCRIPT_DIR/test_hyperliquid_dependabot_guard.ts"
BUN="${BUN:-bun}"
SHELLCHECK="${SHELLCHECK:-shellcheck}"
MODE="${1:-fast}"

usage() {
  printf 'Usage: %s [fast|full]\n' "${BASH_SOURCE[0]}"
}
if (( $# > 1 )); then
  usage >&2
  exit 2
fi

case "$MODE" in
  fast|--fast)
    MODE=fast
    ;;
  full|--full)
    MODE=full
    ;;
  help|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

require_tool() {
  local name
  name=$1
  if ! command -v "$name" >/dev/null 2>&1; then
    printf 'FATAL: required tool is unavailable: %s\n' "$name" >&2
    exit 127
  fi
}

if [[ ! -f "$GUARD_PATH" ]]; then
  printf 'FATAL: shipped Hyperliquid guard is missing: %s\n' "$GUARD_PATH" >&2
  exit 1
fi
if [[ ! -f "$TEST_PATH" ]]; then
  printf 'FATAL: guard behavior test is missing: %s\n' "$TEST_PATH" >&2
  exit 1
fi

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/hyperliquid-guard-verify.XXXXXX")"
cleanup() { # shellcheck disable=SC2329
  local status
  status=$?
  rm -rf -- "$WORKDIR"
  return "$status"
}
trap cleanup EXIT

run_shellcheck() {
  "$SHELLCHECK" --shell=bash \
    "$SCRIPT_DIR/cleanup-rig-requests.sh" \
    "$SCRIPT_DIR/run_hyperliquid_sdk.sh" \
    "$SCRIPT_DIR/smoke-test-llm.sh" \
    "$SCRIPT_DIR/update_llama_cpp_remote.sh" \
    "$SCRIPT_DIR/verify-dependabot-intake.sh" \
    "$SCRIPT_DIR/verify-hyperliquid-guard.sh" \
    "/home/carter/dev/dependabot-webhook/verify.sh"
}

run_bun_syntax_checks() {
  local syntax_dir
  syntax_dir="$WORKDIR/syntax-check"
  mkdir -p -- "$syntax_dir"
  "$BUN" build "$GUARD_PATH" --target=bun --outfile="$syntax_dir/guard.js"
  "$BUN" build "$TEST_PATH" --target=bun --outfile="$syntax_dir/test.js"
}

run_behavior_check() {
  HYPERLIQUID_GUARD_PATH="$GUARD_PATH" "$BUN" run "$TEST_PATH"
}

run_artifact_check() {
  local artifact artifact_check_dir
  artifact="$WORKDIR/omp-dependabot-guard.js"
  artifact_check_dir="$WORKDIR/artifact-check"
  "$BUN" build "$GUARD_PATH" --target=bun --outfile="$artifact"
  mkdir -p -- "$artifact_check_dir"
  "$BUN" build "$artifact" --target=bun \
    --outfile="$artifact_check_dir/omp-dependabot-guard.js"
  [[ -s "$artifact_check_dir/omp-dependabot-guard.js" ]]
  printf 'verified Bun artifact: %s (%s bytes)\n' "$artifact" "$(wc -c < "$artifact")"
}

require_tool "$BUN"
require_tool "$SHELLCHECK"
run_shellcheck
run_bun_syntax_checks
run_behavior_check
if [[ "$MODE" == full ]]; then
  run_artifact_check
fi
printf 'HYPERLIQUID_GUARD_%s_OK\n' "${MODE^^}"
