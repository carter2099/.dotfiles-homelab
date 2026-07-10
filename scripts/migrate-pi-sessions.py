#!/usr/bin/env python3
"""
Migrate Pi sessions: move automated sessions to a separate directory,
delete trivial throwaway sessions, keep interactive ones.

Automated sessions are moved to ~/.pi/agent/sessions-automated/ so they
no longer clutter /resume in interactive Pi sessions. The source fix
(--session-dir flag) has already been applied to digest_runner.py,
run_hyperliquid_sdk.sh, and dependabot-webhook/main.go.

DRY RUN (default): Shows what would happen without making changes.
To execute: python3 ~/scripts/migrate-pi-sessions.py --execute
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

SRC_DIR = Path.home() / ".pi/agent/sessions/--home-carter--"
DST_DIR = Path.home() / ".pi/agent/sessions-automated/--home-carter--"


def classify(path: Path):
    """Classify a session as MOVE, DELETE, or KEEP. Returns (action, reason)."""
    try:
        with open(path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
    except Exception:
        return "ERROR", "parse failed"

    user_count = 0
    has_web_search = False
    has_web_fetch = False
    first_user = ""
    provider = "?"

    for entry in lines:
        if entry.get("type") == "message":
            msg = entry.get("message", {})
            if msg.get("role") == "user":
                user_count += 1
                if not first_user:
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "")
                            for b in content
                            if b.get("type") == "text"
                        )
                    first_user = content
            elif msg.get("role") == "assistant":
                for block in msg.get("content", []):
                    if block.get("type") == "toolCall":
                        if block.get("name") == "web_search":
                            has_web_search = True
                        if block.get("name") == "web_fetch":
                            has_web_fetch = True
        elif entry.get("type") == "model_change":
            provider = entry.get("provider", "?")

    fu_lower = first_user.strip().lower()

    # ── MOVE: current digest phases (local-llm + web tools) ──
    if provider == "local-llm" and (has_web_search or has_web_fetch):
        return "MOVE", "digest phase (local-llm + web tools)"

    # ── MOVE: hyperliquid SDK agent ──
    if "hyperliquid-run/SKILL.md" in first_user:
        return "MOVE", "hyperliquid SDK agent"

    # ── MOVE: dependabot agent ──
    if "automated dependency maintenance agent" in fu_lower:
        return "MOVE", "dependabot agent"

    # ── MOVE: old-format digests (cloud model, "daily ... news curator") ──
    if "daily" in fu_lower and "news curator" in fu_lower and "research" in fu_lower:
        return "MOVE", "old-format digest (cloud model)"

    # ── MOVE: homelab maintenance / update-check agent ──
    if "homelab maintenance agent" in fu_lower:
        return "MOVE", "homelab update-check agent"

    # ── MOVE: dependabot sandbox tests ──
    if fu_lower.startswith("run:") or fu_lower.startswith("run these") or fu_lower.startswith("run this"):
        return "MOVE", "dependabot sandbox test"
    if fu_lower.startswith("try to run"):
        return "MOVE", "dependabot sandbox test"
    if fu_lower.startswith("search the web for") and "ruby gem" in fu_lower:
        return "MOVE", "dependabot sandbox test"
    if fu_lower.startswith("test mode") and ("do not push" in fu_lower or "don't push" in fu_lower):
        return "MOVE", "dependabot sandbox test"
    if fu_lower.startswith("use web_search to search for"):
        return "MOVE", "llm smoke test"

    # ── MOVE: old local-llm digest (no web tools but has news curator pattern) ──
    if provider == "local-llm" and "news curator" in fu_lower:
        return "MOVE", "old local-llm digest"

    # ── DELETE: trivial throwaways ──
    trivial = {
        "hello", "test", "testing", "hi", "hey", "say ok",
    }
    if fu_lower.rstrip(".") in trivial:
        return "DELETE", f"trivial: {first_user[:80]}"

    if fu_lower.startswith("say hello") or fu_lower.startswith("say the word hello"):
        return "DELETE", f"trivial: {first_user[:80]}"
    if fu_lower.startswith("say just the word") or fu_lower.startswith("say 'hello"):
        return "DELETE", f"trivial: {first_user[:80]}"
    if fu_lower.startswith("what date is today"):
        return "DELETE", f"trivial: {first_user[:80]}"
    if fu_lower.startswith("read the file /home/carter/agents.md using the read tool and tell"):
        return "DELETE", f"model test: read AGENTS.md heading"

    # ── DELETE: model testing noise ──
    if "write a python function that calculates fibonacci" in fu_lower:
        return "DELETE", f"model test: fibonacci"
    if fu_lower.startswith("explain the entire history of the roman empire"):
        return "DELETE", f"model test: roman empire"
    if "tell me everything i need to know about react" in fu_lower:
        return "DELETE", f"model test: react dump"

    # ── DELETE: "Say hello and confirm you are GLM 5.2" ──
    if "say hello and confirm" in fu_lower:
        return "DELETE", f"trivial model check: {first_user[:80]}"

    # ── KEEP: everything else (interactive multi-turn + legitimate one-shots) ──
    return "KEEP", f"interactive (users={user_count})"


def main():
    parser = argparse.ArgumentParser(description="Migrate Pi sessions")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform moves/deletes (default: dry run)",
    )
    args = parser.parse_args()

    if not SRC_DIR.exists():
        print(f"Source directory does not exist: {SRC_DIR}")
        sys.exit(1)

    files = sorted(SRC_DIR.glob("*.jsonl"))
    actions = {"MOVE": [], "DELETE": [], "KEEP": [], "ERROR": []}

    for f in files:
        action, reason = classify(f)
        actions[action].append((f, reason))

    print(f"Total sessions: {len(files)}")
    print(f"  MOVE to automated:  {len(actions['MOVE'])}")
    print(f"  DELETE (trash):     {len(actions['DELETE'])}")
    print(f"  KEEP (interactive): {len(actions['KEEP'])}")
    if actions["ERROR"]:
        print(f"  ERRORS:             {len(actions['ERROR'])}")

    print(f"\n{'DRY RUN' if not args.execute else 'EXECUTING'}")

    if not args.execute:
        print("\n=== Sessions that would be DELETED ===")
        for f, reason in actions["DELETE"]:
            print(f"  {f.name}: {reason}")

        print(f"\n=== Sessions that would be MOVED ({len(actions['MOVE'])}) ===")
        print("  (showing first 5)")
        for f, reason in actions["MOVE"][:5]:
            print(f"  {f.name}: {reason}")
        if len(actions["MOVE"]) > 5:
            print(f"  ... and {len(actions['MOVE']) - 5} more")

        print(f"\nSessions that will be KEPT: {len(actions['KEEP'])}")
        if actions["KEEP"]:
            print(f"  (showing first 3)")
            for f, reason in actions["KEEP"][:3]:
                print(f"  {f.name}: {reason}")
            if len(actions["KEEP"]) > 3:
                print(f"  ... and {len(actions['KEEP']) - 3} more")

        print("\nRun with --execute to apply changes.")
        return

    # ── Execute ──
    # Move automated sessions
    if actions["MOVE"]:
        DST_DIR.mkdir(parents=True, exist_ok=True)
        for f, reason in actions["MOVE"]:
            dst = DST_DIR / f.name
            shutil.move(str(f), str(dst))
            print(f"MOVED: {f.name} -> {dst}")

    # Delete trash
    for f, reason in actions["DELETE"]:
        os.remove(str(f))
        print(f"DELETED: {f.name}")

    # Report
    print(f"\nDone. Moved {len(actions['MOVE'])}, deleted {len(actions['DELETE'])}, kept {len(actions['KEEP'])}.")

    # Verify final state
    remaining = sorted(SRC_DIR.glob("*.jsonl"))
    print(f"\nRemaining in --home-carter--: {len(remaining)} sessions")
    if DST_DIR.exists():
        moved_count = len(sorted(DST_DIR.glob("*.jsonl")))
        print(f"Total in sessions-automated: {moved_count} sessions")


if __name__ == "__main__":
    main()
