#!/usr/bin/env python3
"""
Deterministic multi-phase email digest runner.

Orchestrates a pipeline of focused LLM calls to research, judge, curate,
and write a daily news digest. Each phase has a narrow, focused prompt so
the local Qwen model can handle it reliably.

Usage:
    python3 ~/scripts/digest_runner.py ai-tech
    python3 ~/scripts/digest_runner.py agentic-platform  # (avoid during testing)
    python3 ~/scripts/digest_runner.py gaming
    python3 ~/scripts/digest_runner.py world

Architecture:
    Phase 0: Setup (Python) — load config, template, stories-in-flight
    Phase 1: Research (3 pi -p, parallel) — web_search for stories
    Phase 2: Judge research (direct API) — filter against date/relevance rules
    Phase 3: Rank URLs (Python) — sort by impact, cap at N
    Phase 4: Fetch + Summarize (N pi -p, parallel) — web_fetch each article
    Phase 5: Judge summaries (direct API) — verify accuracy/faithfulness
    Phase 6: Curate (direct API) — dedupe, cross-ref, rank, update stories-in-flight
    Phase 7: Write (direct API) — fill HTML template
    Phase 8: Send + Archive (Python) — email, save artifacts, write stories-in-flight
    Phase 9: Summary (direct API) — write .md for future dedup

Idempotent: if a phase output already exists, it's skipped (allows resume).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# ── Paths ──────────────────────────────────────────────────────────────────
DIGESTS_DIR = Path.home() / "digests"
TEMPLATE_PATH = DIGESTS_DIR / "template.html"
SEND_DIGEST_SCRIPT = Path.home() / "scripts" / "send_digest.py"

# ── LLM Proxy ──────────────────────────────────────────────────────────────
LLM_PROXY_URL = "http://localhost:8081/v1/chat/completions"
MODEL_REASONING = "qwen-3.6-35b-q6"       # reasoning ON — used for all phases
MODEL_FAST = "qwen-3.6-35b-q6-fast"        # reasoning OFF — fallback only
DEFAULT_TIMEOUT = 900                        # generous for slow local model
RESEARCH_TIMEOUT = 1800                      # 30 min for research pi -p calls
FETCH_TIMEOUT = 900                          # 15 min per article fetch

# ── Concurrency ────────────────────────────────────────────────────────────
MAX_PARALLEL_RESEARCH = 1   # llama.cpp is single-request; keep sequential
MAX_PARALLEL_FETCH = 1


# ═══════════════════════════════════════════════════════════════════════════
# Topic definitions
# ═══════════════════════════════════════════════════════════════════════════

TOPICS: dict[str, dict[str, Any]] = {
    "ai-tech": {
        "title": "AI & Tech Digest",
        "recipients": ["carter2099@pm.me"],
        "category": "ai-tech",
        "research_angles": [
            {
                "id": "models-releases",
                "prompt": (
                    "Search for AI model releases, major LLM announcements, and significant "
                    "model updates from the last 24 hours. Check sources like TechCrunch AI section "
                    "(https://techcrunch.com/category/artificial-intelligence/), The Verge AI "
                    "(https://www.theverge.com/ai-artificial-intelligence), Ars Technica AI "
                    "(https://arstechnica.com/ai/), and Hacker News (https://news.ycombinator.com/).\n\n"
                    "For each story found, use web_fetch to read the actual article and extract:\n"
                    "- Title\n"
                    "- URL (the exact URL you fetched — do not guess or construct)\n"
                    "- Source domain (e.g. techcrunch.com)\n"
                    "- Publication date (from the article, ISO format if available)\n"
                    "- 1-2 sentence factual summary (no opinion, just what happened)\n"
                    "- Category: Model Releases, AI Infrastructure, or Research\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "If a source fails to load, try another. Prioritize stories from today. "
                    "Only include stories you actually fetched and confirmed."
                ),
            },
            {
                "id": "platforms-tools",
                "prompt": (
                    "Search for agentic AI platform news, developer tools, open source AI projects, "
                    "and coding agent developments from the last 24 hours. Check TechCrunch, "
                    "The Verge, Ars Technica, Hacker News, and dev.to.\n\n"
                    "For each story found, use web_fetch to read the actual article and extract:\n"
                    "- Title\n"
                    "- URL (the exact URL you fetched — do not guess or construct)\n"
                    "- Source domain\n"
                    "- Publication date (from the article, ISO format if available)\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Agentic/Agent Platforms, Open Source, or Tools & Developer\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "Prioritize stories from today. Only include stories you actually fetched "
                    "and confirmed. If a source fails, try another."
                ),
            },
            {
                "id": "industry-community",
                "prompt": (
                    "Search for AI industry news, funding announcements, policy/regulation, major "
                    "company moves, and notable community discussions from the last 24 hours. "
                    "Check TechCrunch, The Verge, Ars Technica, Hacker News, and Reddit r/MachineLearning.\n\n"
                    "For each story found, use web_fetch to read the actual article and extract:\n"
                    "- Title\n"
                    "- URL (the exact URL you fetched — do not guess or construct)\n"
                    "- Source domain\n"
                    "- Publication date (from the article, ISO format if available)\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Industry News, Policy, Funding, or Community\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "Prioritize stories from today. Only include stories you actually fetched "
                    "and confirmed. If a source fails, try another."
                ),
            },
        ],
        "judgment_rules": (
            "For each finding, evaluate against these rules. Be strict — it's better to drop "
            "a marginal story than to include a bad one.\n\n"
            "1. DATE CHECK: Was this published or substantially updated in the last 24 hours? "
            "If the article has no clear date or the date is older than 24 hours, mark 'drop' "
            "with reason 'date_unclear' or 'too_old'.\n"
            "2. SOURCE CHECK: Is this from a known reputable outlet? TechCrunch, The Verge, "
            "Ars Technica, Wired, ZDNet, VentureBeat, Hacker News (as a discussion link, "
            "not the original source), official company blogs, GitHub repos with significant "
            "activity, academic papers on arxiv. Personal blogs are OK if they have substance. "
            "Content farms, SEO spam, and low-quality aggregators should be dropped with "
            "reason 'unreliable_source'.\n"
            "3. RELEVANCE CHECK: Is this about AI, tech, developer tools, or the tech industry? "
            "If it's general business news, politics, or non-tech topics, drop with reason "
            "'not_relevant'.\n"
            "4. DUPLICATE CHECK: Is this the same underlying story as another finding? "
            "If yes, mark the lower-quality one as 'drop' with reason 'duplicate_of:<other_finding_index>'.\n"
            "5. SUBSTANCE CHECK: Does this story have actual news value? Press releases with "
            "no new information, minor version bumps, and 'X company announced they will announce "
            "something' should be dropped with reason 'no_substance'.\n\n"
            "Return ONLY the findings that pass ALL checks with a 'keep' verdict."
        ),
        "categories": [
            "Model Releases", "Agentic/Agent Platforms", "Open Source",
            "Tools & Developer", "Industry News", "Policy", "Funding",
            "AI Infrastructure", "Research", "Community",
        ],
    },
    "agentic-platform": {
        "title": "Agentic Platform Digest",
        "recipients": ["carter2099@pm.me"],  # second recipient added at send time from env
        "category": "agentic-platform",
        "research_angles": [
            {
                "id": "platforms-features",
                "prompt": (
                    "Search for agentic AI platform news: new features, launches, and major "
                    "updates from platforms like Claude Code, Codex, Cursor, omp, Pi, Aider, "
                    "OpenCode, Windsurf, Copilot, and other coding agent platforms. "
                    "Focus on the last 24 hours.\n\n"
                    "For each story found, use web_fetch to read the actual article and extract:\n"
                    "- Title\n"
                    "- URL (exact URL you fetched)\n"
                    "- Source domain\n"
                    "- Publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Platform Updates, New Features, or Launches\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "Only include stories you actually fetched and confirmed."
                ),
            },
            {
                "id": "ecosystem-tools",
                "prompt": (
                    "Search for agentic AI ecosystem news: MCP servers and tools, agent SDKs, "
                    "orchestration frameworks, workflow engines, evaluation benchmarks, "
                    "and notable community projects from the last 24 hours. "
                    "Check GitHub trending, Hacker News, dev.to, and AI newsletters.\n\n"
                    "For each story found, use web_fetch to read and extract:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: MCP/Ecosystem, SDKs & Frameworks, Benchmarks, or Community Projects\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "Only include stories you actually fetched and confirmed."
                ),
            },
            {
                "id": "techniques-research",
                "prompt": (
                    "Search for advances in agentic AI techniques: multi-agent patterns, "
                    "deterministic orchestration, agent evaluation methods, prompting strategies, "
                    "context management, and relevant research papers from the last 24 hours.\n\n"
                    "For each finding, use web_fetch to read and extract:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Techniques & Patterns, Research, or Evaluation\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "Only include findings you actually fetched and confirmed."
                ),
            },
        ],
        "judgment_rules": (
            "For each finding, evaluate against these rules:\n\n"
            "1. DATE CHECK: Published/updated in the last 24 hours? Drop if unclear or too old.\n"
            "2. SOURCE CHECK: Reputable? Drop content farms and low-quality aggregators.\n"
            "3. RELEVANCE CHECK: About agentic platforms, coding agents, multi-agent systems, "
            "MCP ecosystem, or agent development tooling? Drop if tangentially about AI in general.\n"
            "4. DUPLICATE CHECK: Same story as another? Keep the best version.\n"
            "5. SUBSTANCE CHECK: Actual news? Drop 'we're excited to announce we raised a seed round' "
            "and other empty announcements.\n\n"
            "Return ONLY findings that pass with 'keep' verdict."
        ),
        "categories": [
            "Platform Updates", "New Features", "Launches", "MCP/Ecosystem",
            "SDKs & Frameworks", "Benchmarks", "Techniques & Patterns",
            "Research", "Evaluation", "Community Projects",
        ],
    },
    "gaming": {
        "title": "Gaming Digest",
        "recipients": ["carter2099@pm.me"],
        "category": "gaming-digest",
        "research_angles": [
            {
                "id": "releases-announcements",
                "prompt": (
                    "Search for gaming news from the last 24 hours: game releases, major updates, "
                    "patches, DLC announcements, and platform news (Steam, Epic, console). "
                    "Check Kotaku, IGN, PC Gamer, Eurogamer, GameSpot, and gaming subreddits.\n\n"
                    "For each story, use web_fetch to read and extract:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Releases, Updates & Patches, DLC/Expansions, or Platform News\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "Only include stories you actually fetched and confirmed."
                ),
            },
            {
                "id": "industry-esports",
                "prompt": (
                    "Search for gaming industry news from the last 24 hours: studio news, "
                    "esports results, industry trends, hardware, and major community events. "
                    "Check gaming news sites and relevant subreddits.\n\n"
                    "For each story, use web_fetch to read and extract:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Industry, Esports, Hardware, or Community\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "Only include stories you actually fetched and confirmed."
                ),
            },
            {
                "id": "indie-highlights",
                "prompt": (
                    "Search for notable indie game news from the last 24 hours: new indie releases, "
                    "early access launches, Steam Next Fest highlights, and indie dev stories. "
                    "Check Steam new releases, indie game subreddits, and gaming news sites.\n\n"
                    "For each story, use web_fetch to read and extract:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Indie, Early Access, or Dev Stories\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "Only include stories you actually fetched and confirmed."
                ),
            },
        ],
        "judgment_rules": (
            "For each finding, evaluate:\n\n"
            "1. DATE CHECK: Last 24 hours? Drop if unclear or old.\n"
            "2. SOURCE CHECK: Reputable gaming press or official sources? Drop spam/content farms.\n"
            "3. RELEVANCE CHECK: About video games, gaming industry, or gaming hardware? "
            "Not general entertainment.\n"
            "4. DUPLICATE CHECK: Same story? Keep best version.\n"
            "5. SUBSTANCE CHECK: 'Game X tweeted an emoji' is not news. Drop empty stories.\n\n"
            "Return only findings that pass with 'keep'."
        ),
        "categories": [
            "Releases", "Updates & Patches", "DLC/Expansions", "Platform News",
            "Industry", "Esports", "Hardware", "Indie", "Early Access",
            "Dev Stories", "Community",
        ],
    },
    "world": {
        "title": "World Digest",
        "recipients": ["carter2099@pm.me"],
        "category": "world-digest",
        "research_angles": [
            {
                "id": "us-news",
                "prompt": (
                    "Search for major U.S. news from the last 24 hours: politics, policy, "
                    "economy, Supreme Court, Congress, executive actions. Check AP News, "
                    "Reuters, NPR, BBC US section, and major newspaper sites.\n\n"
                    "For each story, use web_fetch to read and extract:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary (strictly factual, no editorializing)\n"
                    "- Category: Politics, Policy, Economy, Judiciary, or Executive\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "Only include stories you actually fetched and confirmed."
                ),
            },
            {
                "id": "world-affairs",
                "prompt": (
                    "Search for major international news from the last 24 hours: geopolitics, "
                    "conflicts, diplomacy, international organizations, global economy. "
                    "Check AP News, Reuters, BBC World, Al Jazeera, and major outlets.\n\n"
                    "For each story, use web_fetch to read and extract:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Geopolitics, Conflict, Diplomacy, Global Economy, or International\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "Only include stories you actually fetched and confirmed."
                ),
            },
            {
                "id": "science-culture",
                "prompt": (
                    "Search for notable science, technology, health, environment, and cultural "
                    "news from the last 24 hours. Check major outlets, science journals' news "
                    "sections, and reputable science news sites.\n\n"
                    "For each story, use web_fetch to read and extract:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Science, Health, Environment, Technology, or Culture\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "Only include stories you actually fetched and confirmed."
                ),
            },
        ],
        "judgment_rules": (
            "For each finding, evaluate:\n\n"
            "1. DATE CHECK: Last 24 hours? Drop if unclear or old.\n"
            "2. SOURCE CHECK: Reputable news organization? Drop blogs posing as news, "
            "content farms, and known misinformation sources.\n"
            "3. RELEVANCE CHECK: Significant U.S. or world event? Not local crime, "
            "celebrity gossip, or sports (unless major international significance).\n"
            "4. DUPLICATE CHECK: Same story? Keep best version.\n"
            "5. SUBSTANCE CHECK: Is this actually news? 'Politician says something' "
            "without significant context or consequence is not news.\n\n"
            "Return only findings that pass with 'keep'."
        ),
        "categories": [
            "Politics", "Policy", "Economy", "Judiciary", "Executive",
            "Geopolitics", "Conflict", "Diplomacy", "Global Economy",
            "International", "Science", "Health", "Environment",
            "Technology", "Culture",
        ],
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# Utility: LLM calls
# ═══════════════════════════════════════════════════════════════════════════

def _call_llm_proxy(
    system: str,
    user: str,
    model: str = MODEL_REASONING,
    temperature: float = 0.3,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Call the local Qwen model via llm-proxy. Returns response text."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    resp = requests.post(LLM_PROXY_URL, json=payload, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    return body["choices"][0]["message"]["content"]


def _call_pi_p(
    prompt: str,
    model: str = MODEL_REASONING,
    timeout: int = RESEARCH_TIMEOUT,
    append_system: str | None = None,
) -> str:
    """Call pi -p (headless) for steps that need web_search/web_fetch tools.

    Returns the raw stdout. pi -p needs generous timeouts because the
    local model is slow (sometimes <30 tok/s) and web fetches add latency.
    """
    cmd = ["pi", "-p", "--provider", "local-llm", "--model", model]
    if append_system:
        cmd.extend(["--append-system-prompt", append_system])

    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "HOME": str(Path.home())},
    )
    # pi -p outputs to stdout; stderr may have warnings
    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(f"pi -p failed (rc={result.returncode}): {result.stderr[:500]}")
    return result.stdout


def _extract_json(text: str, label: str = "output") -> Any:
    """Extract JSON from LLM output. Tries markdown fences first, then raw JSON."""
    # Try ```json ... ``` fence
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try to find a JSON object or array in the text
    # Look for { ... } or [ ... ] at the start or after newlines
    for pattern in [r"\{[\s\S]*\}", r"\[[\s\S]*\]"]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue

    # Last resort: try the whole text
    text_stripped = text.strip()
    if text_stripped.startswith("{") or text_stripped.startswith("["):
        try:
            return json.loads(text_stripped)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract JSON from {label}. Raw text (first 500 chars):\n{text[:500]}")


# ═══════════════════════════════════════════════════════════════════════════
# Phase implementations
# ═══════════════════════════════════════════════════════════════════════════

def phase_1_research(topic: dict, run_dir: Path) -> list[dict]:
    """Phase 1: Parallel research agents via pi -p.

    Each research angle gets its own pi -p call. They use web_search and
    web_fetch to find stories. Returns merged list of findings.
    """
    output_path = run_dir / "01-research-raw.json"
    if output_path.exists():
        print(f"  [skip] Phase 1 output exists: {output_path}")
        return json.loads(output_path.read_text())

    system_prompt = (
        "You are a research assistant for a daily news digest. Your job is to search "
        "the web for recent news stories and report your findings in structured JSON.\n\n"
        "IMPORTANT: Do NOT use web_fetch to read articles. Only use web_search to find "
        "stories by their titles and URLs. The articles will be fetched later by a "
        "separate process. Your job is discovery, not deep reading.\n\n"
        "PREFER PRIMARY SOURCES: Link directly to the original article on the publisher's "
        "site (e.g. techcrunch.com, theverge.com, arstechnica.com, reuters.com). "
        "Avoid news aggregators, roundup sites, and link-blog posts — find the real "
        "source behind the story.\n\n"
        "Use web_search with 2-3 different queries to find stories from the last 24 hours. "
        "After searching, output your findings as a JSON array wrapped in ```json fences. "
        "Each finding must have these fields:\n"
        '  {"title": "...", "url": "...", "source_domain": "...", '
        '"date_published": "YYYY-MM-DD or empty if unknown from search snippet", '
        '"summary": "1-sentence summary from search result", '
        '"category": "...", "importance": "high|medium|low"}\n\n'
        "Never construct URLs — only use URLs that appeared in web_search results. "
        "Target 5-8 findings. Be quick — search, compile, output JSON."
    )

    def _research_one(angle: dict) -> list[dict]:
        label = f"research:{angle['id']}"
        print(f"  [run ] {label}")
        t0 = time.time()
        try:
            raw = _call_pi_p(angle["prompt"], model=MODEL_REASONING, timeout=RESEARCH_TIMEOUT,
                             append_system=system_prompt)
            findings = _extract_json(raw, f"{label} output")
            elapsed = time.time() - t0
            print(f"  [done] {label} — {len(findings)} findings in {elapsed:.0f}s")
            return findings
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  [FAIL] {label} — {e} ({elapsed:.0f}s)")
            return []

    findings: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_RESEARCH) as pool:
        futures = {pool.submit(_research_one, a): a for a in topic["research_angles"]}
        for future in as_completed(futures):
            findings.extend(future.result())

    output_path.write_text(json.dumps(findings, indent=2))
    print(f"  Phase 1 done: {len(findings)} total findings")
    return findings


def phase_2_judge_research(topic: dict, findings: list[dict], run_dir: Path) -> list[dict]:
    """Phase 2: One LLM call judges all research findings against rules.

    Returns list of kept findings (those passing all checks).
    """
    output_path = run_dir / "02-research-judged.json"
    if output_path.exists():
        print(f"  [skip] Phase 2 output exists: {output_path}")
        return json.loads(output_path.read_text())

    print(f"  [run ] judge_research — {len(findings)} findings to evaluate")
    t0 = time.time()

    findings_json = json.dumps(findings, indent=2)

    system = (
        "You are a strict editor for a daily news digest. Your job is to filter "
        "research findings against quality rules. Be harsh — a false positive (bad "
        "story included) is worse than a false negative (good story missed).\n\n"
        "You will receive a JSON array of research findings and a set of rules. "
        "For each finding, evaluate it against every rule and output a verdict.\n\n"
        "Output a JSON object with two arrays:\n"
        '  {"kept": [...], "rejected": [{"finding": ..., "reason": "..."}, ...]}\n'
        "Wrap your output in ```json fences."
    )

    user = (
        f"## Rules\n\n{topic['judgment_rules']}\n\n"
        f"## Findings to evaluate\n\n{findings_json}\n\n"
        "Evaluate each finding against every rule. Output the kept and rejected arrays "
        "in ```json fences. Include a clear reason for each rejection."
    )

    try:
        raw = _call_llm_proxy(system, user, model=MODEL_REASONING)
        result = _extract_json(raw, "judge_research output")
        kept = result.get("kept", [])
        rejected = result.get("rejected", [])
        elapsed = time.time() - t0
        print(f"  [done] judge_research — {len(kept)} kept, {len(rejected)} rejected ({elapsed:.0f}s)")
        for r in rejected:
            finding = r.get("finding", {})
            reason = r.get("reason", "unspecified")
            print(f"    ✗ {finding.get('title', '?')[:60]}: {reason}")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  [FAIL] judge_research — {e} ({elapsed:.0f}s), keeping all findings")
        kept = findings
        rejected = []

    output = {"kept": kept, "rejected": rejected}
    output_path.write_text(json.dumps(output, indent=2))
    return kept


def phase_3_rank(topic: dict, findings: list[dict], run_dir: Path) -> list[dict]:
    """Phase 3: Python-side ranking. Sort by importance, cap at top N.

    No LLM call — deterministic.
    """
    output_path = run_dir / "03-urls-ranked.json"
    if output_path.exists():
        print(f"  [skip] Phase 3 output exists: {output_path}")
        return json.loads(output_path.read_text())

    importance_order = {"high": 0, "medium": 1, "low": 2}
    ranked = sorted(findings, key=lambda f: importance_order.get(f.get("importance", "low"), 2))

    # Cap at 15 — enough for a daily digest
    ranked = ranked[:15]

    output_path.write_text(json.dumps(ranked, indent=2))
    print(f"  Phase 3 done: {len(ranked)} URLs ranked (capped from {len(findings)})")
    return ranked


def phase_4_fetch(topic: dict, findings: list[dict], run_dir: Path) -> list[dict]:
    """Phase 4: Fetch each article and write detailed summaries.

    Parallel pi -p calls, each fetching one URL.
    """
    output_path = run_dir / "04-fetch-summaries.json"
    if output_path.exists():
        print(f"  [skip] Phase 4 output exists: {output_path}")
        return json.loads(output_path.read_text())

    system_prompt = (
        "You are a research assistant. Your job is to read ONE article via web_fetch "
        "and produce a detailed, factual summary. Do not search — just fetch the URL "
        "given and summarize.\n\n"
        "After fetching, output your result as a single JSON object wrapped in ```json "
        "fences with these fields:\n"
        '  {"title": "article title", "url": "the URL you fetched", '
        '"date_confirmed": "YYYY-MM-DD or empty if not found in article", '
        '"author": "author name or empty", '
        '"summary": "2-4 sentence detailed summary capturing the main points", '
        '"key_details": ["bullet point 1", "bullet point 2", ...], '
        '"fetch_success": true|false}\n\n'
        "If the page fails to load or is not an article, set fetch_success=false "
        "and explain briefly in the summary field."
    )

    def _fetch_one(finding: dict) -> dict:
        url = finding.get("url", "")
        title = finding.get("title", "unknown")
        label = f"fetch:{title[:50]}"
        print(f"  [run ] {label}")
        t0 = time.time()
        prompt = (
            f"Fetch this article: {url}\n\n"
            f"Title from research: {title}\n\n"
            "Use web_fetch to read the article. Then output your summary as JSON "
            "wrapped in ```json fences."
        )
        try:
            raw = _call_pi_p(prompt, model=MODEL_REASONING, timeout=FETCH_TIMEOUT,
                             append_system=system_prompt)
            result = _extract_json(raw, f"{label} output")
            elapsed = time.time() - t0
            status = "✓" if result.get("fetch_success", True) else "✗"
            print(f"  [done] {label} — {status} ({elapsed:.0f}s)")
            return {**finding, **result}
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  [FAIL] {label} — {e} ({elapsed:.0f}s)")
            return {**finding, "fetch_success": False,
                    "summary": f"Fetch failed: {str(e)[:100]}", "key_details": [],
                    "date_confirmed": "", "author": ""}

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_FETCH) as pool:
        futures = {pool.submit(_fetch_one, f): f for f in findings}
        for future in as_completed(futures):
            results.append(future.result())

    # Preserve original order
    url_order = {f.get("url"): i for i, f in enumerate(findings)}
    results.sort(key=lambda r: url_order.get(r.get("url"), 999))

    output_path.write_text(json.dumps(results, indent=2))
    successful = sum(1 for r in results if r.get("fetch_success", True))
    print(f"  Phase 4 done: {successful}/{len(results)} fetches successful")
    return results


def phase_5_judge_summaries(topic: dict, summaries: list[dict], run_dir: Path) -> list[dict]:
    """Phase 5: Judge summary accuracy and faithfulness.

    One LLM call reviews all summaries and flags issues.
    """
    output_path = run_dir / "05-summaries-judged.json"
    if output_path.exists():
        print(f"  [skip] Phase 5 output exists: {output_path}")
        return json.loads(output_path.read_text())

    # Only judge successful fetches
    to_judge = [s for s in summaries if s.get("fetch_success", True)]
    failed = [s for s in summaries if not s.get("fetch_success", True)]

    if not to_judge:
        print("  Phase 5: no successful fetches to judge")
        return summaries

    print(f"  [run ] judge_summaries — {len(to_judge)} summaries to evaluate")
    t0 = time.time()

    summaries_json = json.dumps(to_judge, indent=2)

    system = (
        "You are a strict editor verifying AI-written summaries. You receive article "
        "summaries and judge whether each is accurate and faithful to what the article "
        "likely contains.\n\n"
        "For each summary, evaluate:\n"
        "1. DATE_CHECK: Does the date_confirmed match what you'd expect from a real "
        "article published today? If date is empty or looks fabricated, flag it.\n"
        "2. FAITHFULNESS: Does the summary contain plausible facts, or does it read "
        "like hallucinated/generic filler? Signs of hallucination: vague claims without "
        "specifics, details that seem wrong for the source, overly confident statements "
        "that sound made up.\n"
        "3. COMPLETENESS: Does the summary capture what the article is actually about? "
        "A summary that misses the main point is unhelpful.\n"
        "4. OVERALL: verdict = 'keep' | 'fix' (minor issues, note them) | 'drop' "
        "(unrecoverable — hallucinated, wrong, or empty)\n\n"
        "Output a JSON array of judgments wrapped in ```json fences, one per summary:\n"
        '  [{"url": "...", "verdict": "keep|fix|drop", "issues": ["issue 1", ...], '
        '"fixed_summary": "if fix, corrected summary, else empty"}, ...]\n\n'
        "Be suspicious. Summaries that sound too generic or lack specific names, "
        "numbers, or concrete claims are likely hallucinated — drop them."
    )

    user = (
        f"## Summaries to judge\n\n{summaries_json}\n\n"
        "Judge each summary. Output a JSON array of judgments in ```json fences. "
        "Err on the side of dropping questionable summaries."
    )

    try:
        raw = _call_llm_proxy(system, user, model=MODEL_REASONING)
        judgments = _extract_json(raw, "judge_summaries output")
        if not isinstance(judgments, list):
            judgments = [judgments]

        # Apply judgments
        judged_map = {j.get("url", ""): j for j in judgments}
        results = []
        for s in summaries:
            url = s.get("url", "")
            j = judged_map.get(url, {})
            verdict = j.get("verdict", "keep")
            if s.get("fetch_success") is False:
                verdict = "drop"
            if verdict == "fix" and j.get("fixed_summary"):
                s["summary"] = j["fixed_summary"]
            s["judge_verdict"] = verdict
            s["judge_issues"] = j.get("issues", [])
            results.append(s)

        kept = sum(1 for r in results if r.get("judge_verdict") == "keep")
        fixed = sum(1 for r in results if r.get("judge_verdict") == "fix")
        dropped = sum(1 for r in results if r.get("judge_verdict") == "drop")
        elapsed = time.time() - t0
        print(f"  [done] judge_summaries — {kept} keep, {fixed} fix, {dropped} drop ({elapsed:.0f}s)")
        for r in results:
            if r.get("judge_verdict") in ("fix", "drop"):
                issues = "; ".join(r.get("judge_issues", ["unspecified"]))
                print(f"    {r['judge_verdict']} {r.get('title', '?')[:60]}: {issues[:120]}")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  [FAIL] judge_summaries — {e} ({elapsed:.0f}s), keeping all summaries")
        for s in summaries:
            s.setdefault("judge_verdict", "keep")
            s.setdefault("judge_issues", [])
        results = summaries

    output_path.write_text(json.dumps(results, indent=2))
    return results


def phase_6_curate(topic: dict, summaries: list[dict], run_dir: Path,
                   stories_in_flight: dict) -> tuple[list[dict], dict, list[dict]]:
    """Phase 6: Curate — dedupe, cross-reference, rank, flag gaps, build Recent & Relevant.

    Returns (fresh_stories, updated_stories_in_flight, recent_stories).
    """
    output_path = run_dir / "06-curated.json"
    if output_path.exists():
        print(f"  [skip] Phase 6 output exists: {output_path}")
        data = json.loads(output_path.read_text())
        return data["fresh"], data.get("stories_in_flight", stories_in_flight), data["recent"]

    kept = [s for s in summaries if s.get("judge_verdict") in ("keep", "fix")]
    dropped = [s for s in summaries if s.get("judge_verdict") == "drop"]

    print(f"  [run ] curate — {len(kept)} stories to curate, {len(dropped)} dropped")
    t0 = time.time()

    kept_json = json.dumps(kept, indent=2)
    sif_json = json.dumps(stories_in_flight, indent=2)

    system = (
        "You are the lead editor of a daily news digest. You receive a set of "
        "vetted, detailed article summaries and a 'stories-in-flight' tracker of "
        "evolving stories from previous days. Your job is to curate the final "
        "story selection.\n\n"
        "Tasks:\n"
        "1. DEDUPLICATE: If two summaries cover the same underlying story, merge them "
        "(keep the best summary, note the other URL as related).\n"
        "2. CROSS-REFERENCE: Identify connections between stories (e.g. 'the HN "
        "discussion of the TechCrunch article above'). Add a 'related_to' field.\n"
        "3. RANK BY IMPACT: Assign a final rank (1 = most important). 5-7 fresh "
        "stories for the main section, ordered by importance.\n"
        "4. FLAG GAPS: What important story might be missing? Add a 'gaps' note.\n"
        "5. UPDATE STORIES-IN-FLIGHT: For each story already in the tracker, check if\n"
        "today's findings add meaningful new developments. If so, update the story's\n"
        "'latest_dev' and set 'last_updated' to today's date (YYYY-MM-DD) — this\n"
        "resets the auto-cool clock and keeps the story active.\n\n"
        "You may manually set status to 'cooled' if a story has definitively resolved\n"
        "(e.g. a bill was signed into law, a trial reached a verdict, a product shipped).\n"
        "Otherwise, leave the status as-is — the system auto-cools stories with no\n"
        "updates after 7 days and auto-prunes cooled stories after 14 days.\n\n"
        "Add NEW evolving stories to the tracker: major announcements, unfolding events,\n"
        "controversies, multi-day stories. Each needs: title, url, first_seen (today),\n"
        "last_updated (today), latest_dev (1-sentence summary of what's new today),\n"
        "status: 'active', category.\n"
        "6. BUILD RECENT & RELEVANT: Select 2-3 stories from the updated tracker with\n"
        "status 'active' (not 'cooled') for the 'Recent & Relevant' section. For each,\n"
        "include a WHY line explaining what changed or why it's still relevant.\n"
        "Recent stories should be DIFFERENT from the fresh stories above — they are\n"
        "ongoing narratives, not today's headlines.\n\n"
        "Output a JSON object wrapped in ```json fences with this structure:\n"
        '  {\n'
        '    "fresh": [\n'
        '      {"rank": 1, "title": "...", "url": "...", "category": "...",\n'
        '       "summary": "2-3 sentence editorial summary", "related_to": null|1,\n'
        '       "related_urls": ["..."]},\n'
        '      ...\n'
        '    ],\n'
        '    "recent": [\n'
        '      {"title": "...", "url": "...", "category": "...",\n'
        '       "summary": "1-2 sentences (what the story IS, not what changed)",\n'
        '       "why_still_relevant": "what changed or why it matters now"},\n'
        '      ...\n'
        '    ],\n'
        '    "stories_in_flight": <updated tracker object>,\n'
        '    "gaps": "string describing missing stories",\n'
        '    "intro_hook": "2-3 sentence editorial intro for the email"\n'
        '  }\n\n'
        "Keep summaries tight — these are read on mobile. Prioritize substance over hype.\n"
        "IMPORTANT: Recent & Relevant stories must be DIFFERENT from Fresh stories. "
        "Fresh = today's news. Recent = ongoing narratives from earlier days in the tracker. "
        "Do not put the same story in both sections."
    )

    dropped_json = json.dumps(
        [{"title": d.get("title"), "url": d.get("url"),
          "reason": d.get("judge_issues", [])} for d in dropped],
        indent=2)

    user = (
        f"## Vetted Summaries (kept after judgment)\n\n{kept_json}\n\n"
        f"## Dropped Summaries (for reference, do not include)\n\n"
        f"{dropped_json}\n\n"
        f"## Stories In Flight (from previous days)\n\n{sif_json}\n\n"
        "Curate the final selection. Output the JSON object in ```json fences."
    )

    try:
        raw = _call_llm_proxy(system, user, model=MODEL_REASONING)
        result = _extract_json(raw, "curate output")
        fresh = result.get("fresh", kept[:7])
        recent = result.get("recent", [])
        updated_sif = result.get("stories_in_flight", stories_in_flight)
        gaps = result.get("gaps", "")
        intro = result.get("intro_hook", "")
        elapsed = time.time() - t0
        print(f"  [done] curate — {len(fresh)} fresh, {len(recent)} recent ({elapsed:.0f}s)")
        if gaps:
            print(f"    Gaps: {gaps[:200]}")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  [FAIL] curate — {e} ({elapsed:.0f}s), using raw summaries")
        fresh = kept[:7]
        recent = []
        updated_sif = stories_in_flight
        intro = ""

    # Attach intro to output for Phase 7
    output = {
        "fresh": fresh,
        "recent": recent,
        "stories_in_flight": updated_sif,
        "intro_hook": intro,
        "gaps": gaps if 'gaps' in dir() else "",
    }
    output_path.write_text(json.dumps(output, indent=2))
    return fresh, updated_sif, recent


def phase_7_write(topic: dict, fresh: list[dict], recent: list[dict],
                  intro_hook: str, run_dir: Path) -> str:
    """Phase 7: Write the HTML email.

    One LLM call fills the HTML template with curated stories.
    """
    output_path = run_dir / "digest.html"
    if output_path.exists():
        print(f"  [skip] Phase 7 output exists: {output_path}")
        return output_path.read_text()

    print(f"  [run ] write_html — {len(fresh)} fresh, {len(recent)} recent")
    t0 = time.time()

    template = TEMPLATE_PATH.read_text()
    today_str = datetime.now().strftime("%B %d, %Y")

    # Pre-fill the template header
    html = template.replace("{{DIGEST_TITLE}}", topic["title"])
    html = html.replace("{{DATE}}", today_str)

    curated_json = json.dumps({"fresh": fresh, "recent": recent}, indent=2)

    system = (
        "You are an HTML email writer for a daily digest. You receive curated story "
        "data and an HTML template with placeholders. Fill in the placeholders and "
        "output the complete HTML.\n\n"
        "CRITICAL RULES:\n"
        "- Every story link must use the exact URL provided — do not alter, guess, or construct URLs.\n"
        "- Use the EXACT story block HTML from the template comments for each story.\n"
        "- For Recent & Relevant stories, include the WHY line variant with the purple italic text.\n"
        "- Keep summaries concise (2-3 sentences max). These are read on mobile.\n"
        "- Do not add any stories beyond what's in the curated data.\n"
        "- Do not modify the template structure, styling, or layout.\n"
        "- Output ONLY the complete HTML document — no explanations, no markdown wrappers."
    )

    default_intro = (
        "No intro provided — write a brief 2-3 sentence "
        "editorial intro setting the tone for today's digest."
    )

    user = (
        f"## Intro\n{intro_hook or default_intro}\n\n"
        f"## Fresh Stories (use the standard story block for each)\n{curated_json}\n\n"
        f"## HTML Template (fill {{INTRO}}, {{FRESH_STORIES}}, {{RECENT_STORIES}})\n\n{html}\n\n"
        "Fill the placeholders with the curated stories. Output the complete HTML document."
    )

    try:
        raw = _call_llm_proxy(system, user, model=MODEL_REASONING)
        # The model should output the complete HTML. Strip any markdown fences.
        html_output = re.sub(r"^```html?\s*\n?", "", raw.strip())
        html_output = re.sub(r"\n?```\s*$", "", html_output)
        elapsed = time.time() - t0
        output_path.write_text(html_output)
        print(f"  [done] write_html — {len(html_output)} chars ({elapsed:.0f}s)")
        return html_output
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  [FAIL] write_html — {e} ({elapsed:.0f}s)")
        raise


def phase_8_send_archive(topic: dict, html: str, stories_in_flight: dict,
                         run_dir: Path, digest_dir: Path) -> None:
    """Phase 8: Send email, archive HTML, write stories-in-flight.

    No LLM call — pure Python.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Write HTML to temp file for send_digest.py
    temp_html = digest_dir / ".daily_digest.html"
    temp_html.write_text(html)

    # Send email
    recipients = topic["recipients"].copy()

    # For agentic-platform, add the second recipient from .smtp_config
    if topic["category"] == "agentic-platform":
        smtp_config = Path.home() / "scripts" / ".smtp_config"
        if smtp_config.exists():
            for line in smtp_config.read_text().splitlines():
                if line.startswith("AGENTIC_CC="):
                    cc = line.split("=", 1)[1].strip()
                    if cc:
                        recipients.append(cc)
                    break

    subject = f"{topic['title']} — {today_str}"
    print(f"  [run ] send_email to {recipients}")
    try:
        subprocess.run(
            ["python3", str(SEND_DIGEST_SCRIPT),
             "--subject", subject,
             "--body-file", str(temp_html),
             "--to"] + recipients,
            check=True,
            capture_output=True,
            text=True,
        )
        print(f"  [done] send_email — sent to {', '.join(recipients)}")
    except subprocess.CalledProcessError as e:
        print(f"  [FAIL] send_email — {e.stderr[:300]}")

    # Archive HTML
    archive_path = digest_dir / f"{today_str}.html"
    shutil.copy(temp_html, archive_path)
    print(f"  [done] archived HTML → {archive_path}")

    # Write updated stories-in-flight
    sif_path = digest_dir / "stories-in-flight.json"
    sif_path.write_text(json.dumps(stories_in_flight, indent=2))
    print(f"  [done] stories-in-flight updated")

    # Save curated.json into digest dir for future reference
    curated_src = run_dir / "06-curated.json"
    if curated_src.exists():
        shutil.copy(curated_src, run_dir / "curated_copy.json")


def phase_9_summary(topic: dict, fresh: list[dict], recent: list[dict],
                    run_dir: Path, digest_dir: Path) -> None:
    """Phase 9: Write the .md summary for future dedup.

    One LLM call, lightweight. Output goes to run_dir; Phase 8 copies it.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    output_path = run_dir / "summary.md"
    digest_md_path = digest_dir / f"{today_str}.md"
    if output_path.exists():
        print(f"  [skip] Phase 9 output exists: {output_path}")
        return

    print(f"  [run ] summary_md")
    t0 = time.time()

    fresh_json = json.dumps(fresh, indent=2)
    recent_json = json.dumps(recent, indent=2)

    system = (
        "You are writing a concise markdown summary of today's email digest for "
        "archival and future deduplication. Output ONLY the markdown, no explanations."
    )

    user = (
        f"Write a markdown summary of today's {topic['title']} in this exact format:\n\n"
        f"# {topic['title']} — {today_str}\n"
        f"**Sent to:** {', '.join(topic['recipients'])}\n\n"
        "## Fresh\n"
        "- [Story title](URL) — one-line summary\n"
        "- [Story title](URL) — one-line summary\n\n"
        "## Recent & Relevant\n"
        "- [Story title](URL) — one-line summary (why still relevant)\n\n"
        "## Coverage Gaps\n"
        "- Any notable stories or angles that were missed today\n\n"
        "IMPORTANT: Every story MUST include its URL as a markdown link `[title](URL)`. "
        "This is used by the dedup system in future runs. Never omit the URL.\n\n"
        f"## Fresh Stories Data\n\n{fresh_json}\n\n"
        f"## Recent Stories Data\n\n{recent_json}"
    )

    try:
        raw = _call_llm_proxy(system, user, model=MODEL_REASONING)
        # Clean up any markdown fences
        md_output = re.sub(r"^```(?:markdown)?\s*\n?", "", raw.strip())
        md_output = re.sub(r"\n?```\s*$", "", md_output)
        output_path.write_text(md_output + "\n")
        elapsed = time.time() - t0
        print(f"  [done] summary_md — {len(md_output)} chars ({elapsed:.0f}s)")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  [FAIL] summary_md — {e} ({elapsed:.0f}s)")
        # Fallback: write minimal summary from structured data
        lines = [
            f"# {topic['title']} — {today_str}",
            f"**Sent to:** {', '.join(topic['recipients'])}",
            "",
            "## Fresh",
        ]
        for s in fresh[:10]:
            lines.append(f"- [{s.get('title', '?')}]({s.get('url', '#')}) — {s.get('summary', '')[:100]}")
        lines.append("")
        lines.append("## Recent & Relevant")
        for s in recent[:5]:
            lines.append(f"- [{s.get('title', '?')}]({s.get('url', '#')}) — {s.get('summary', '')[:100]}")
        output_path.write_text("\n".join(lines) + "\n")

    # Copy to digest root dir for cross-run dedup
    if output_path.exists():
        shutil.copy(output_path, digest_md_path)


# ═══════════════════════════════════════════════════════════════════════════
# Stories-in-flight management
# ═══════════════════════════════════════════════════════════════════════════

# ── Stories-in-flight pruning ──────────────────────────────────────────────
COOL_AFTER_DAYS = 7    # auto-set status to "cooled" if no updates in 7 days
PRUNE_AFTER_DAYS = 14  # remove cooled stories entirely after 14 days


def load_and_prune_stories_in_flight(digest_dir: Path) -> dict:
    """Load the cross-day story tracker and apply deterministic pruning.

    Two rules (Python-side, not LLM-dependent):
    1. AUTO-COOL: Any story with status "active" and last_updated older than
       COOL_AFTER_DAYS → set status to "cooled". This removes it from the
       "Recent & Relevant" candidate pool.
    2. AUTO-PRUNE: Any story with status "cooled" and last_updated older than
       PRUNE_AFTER_DAYS → remove from the tracker entirely.

    The Phase 6 curation LLM can still revive stories by updating last_updated
    and setting status back to "active" when new developments appear.
    """
    path = digest_dir / "stories-in-flight.json"
    if not path.exists():
        return {"stories": []}

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError):
        return {"stories": []}

    today = datetime.now(timezone.utc).date()
    stories = data.get("stories", [])
    kept = []
    auto_cooled = 0
    auto_pruned = 0

    for s in stories:
        # Parse last_updated date
        last_str = s.get("last_updated", "")
        try:
            last_date = datetime.strptime(last_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            # Can't parse date — keep it but flag
            s["status"] = "cooled"
            auto_cooled += 1
            kept.append(s)
            continue

        age_days = (today - last_date).days
        status = s.get("status", "active")

        # Rule 1: Auto-cool stale active stories
        if status == "active" and age_days >= COOL_AFTER_DAYS:
            s["status"] = "cooled"
            auto_cooled += 1
            kept.append(s)
            continue

        # Rule 2: Auto-prune old cooled stories
        if status == "cooled" and age_days >= PRUNE_AFTER_DAYS:
            auto_pruned += 1
            continue

        kept.append(s)

    if auto_cooled > 0:
        print(f"  Auto-cooled {auto_cooled} stale stories (> {COOL_AFTER_DAYS}d no updates)")
    if auto_pruned > 0:
        print(f"  Auto-pruned {auto_pruned} expired stories (> {PRUNE_AFTER_DAYS}d cooled)")

    data["stories"] = kept
    return data


def cleanup_old_artifacts(digest_dir: Path, max_age_days: int = 14):
    """Remove run directories older than max_age_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    for child in digest_dir.iterdir():
        if child.is_dir() and child.name != "stories-in-flight":
            try:
                # Parse date from dir name (YYYY-MM-DD)
                date = datetime.strptime(child.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if date < cutoff:
                    shutil.rmtree(child)
                    print(f"  Cleaned up old run dir: {child.name}")
            except (ValueError, OSError):
                pass


# ═══════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ═══════════════════════════════════════════════════════════════════════════

def run_digest(category: str, dry_run: bool = False) -> None:
    """Run the full digest pipeline for a topic category."""
    if category not in TOPICS:
        print(f"Unknown topic: {category}")
        print(f"Available: {', '.join(TOPICS)}")
        sys.exit(1)

    topic = TOPICS[category]
    today_str = datetime.now().strftime("%Y-%m-%d")
    digest_dir = DIGESTS_DIR / topic["category"]
    run_dir = digest_dir / today_str
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  {topic['title']} — {today_str}")
    print(f"  Run dir: {run_dir}")
    print(f"{'='*60}\n")

    overall_start = time.time()

    # Phase 0: Setup
    print("── Phase 0: Setup ──")
    stories_in_flight = load_and_prune_stories_in_flight(digest_dir)
    active_stories = [s for s in stories_in_flight.get("stories", [])
                      if s.get("status") == "active"]
    print(f"  Stories in flight: {len(active_stories)} active")
    cleanup_old_artifacts(digest_dir)

    try:
        # Phase 1: Research
        print("\n── Phase 1: Research ──")
        findings = phase_1_research(topic, run_dir)
        if not findings:
            print("  WARNING: No research findings. Digest will be empty.")
            # Continue anyway — later phases handle empty input

        # Phase 2: Judge Research
        print("\n── Phase 2: Judge Research ──")
        if findings:
            kept_findings = phase_2_judge_research(topic, findings, run_dir)
        else:
            kept_findings = []

        # Phase 3: Rank URLs
        print("\n── Phase 3: Rank URLs ──")
        if kept_findings:
            ranked = phase_3_rank(topic, kept_findings, run_dir)
        else:
            ranked = []

        # Phase 4: Fetch + Summarize
        print("\n── Phase 4: Fetch & Summarize ──")
        if ranked:
            summaries = phase_4_fetch(topic, ranked, run_dir)
        else:
            summaries = []

        # Phase 5: Judge Summaries
        print("\n── Phase 5: Judge Summaries ──")
        if summaries:
            judged = phase_5_judge_summaries(topic, summaries, run_dir)
        else:
            judged = []

        # Phase 6: Curate
        print("\n── Phase 6: Curate ──")
        if judged:
            fresh, stories_in_flight, recent = phase_6_curate(
                topic, judged, run_dir, stories_in_flight)
        else:
            fresh, recent = [], []

        # Phase 7: Write
        print("\n── Phase 7: Write HTML ──")
        curated_data = json.loads((run_dir / "06-curated.json").read_text()) \
            if (run_dir / "06-curated.json").exists() else {}
        intro_hook = curated_data.get("intro_hook", "")
        if fresh:
            html = phase_7_write(topic, fresh, recent, intro_hook, run_dir)
        else:
            # Minimal fallback HTML
            html = (
                f'<html><body><h1>{topic["title"]}</h1>'
                f'<p>{today_str}</p><p>No stories found today.</p></body></html>'
            )
            (run_dir / "digest.html").write_text(html)

        # Phase 8: Send + Archive
        print("\n── Phase 8: Send & Archive ──")
        if dry_run:
            print("  [skip] DRY RUN — skipping email send")
            # Still archive locally
            today_str = datetime.now().strftime("%Y-%m-%d")
            archive_path = digest_dir / f"{today_str}.html"
            shutil.copy(run_dir / "digest.html", archive_path)
            print(f"  [done] archived HTML → {archive_path}")
            # Still update stories-in-flight
            sif_path = digest_dir / "stories-in-flight.json"
            sif_path.write_text(json.dumps(stories_in_flight, indent=2))
            print(f"  [done] stories-in-flight updated")
        else:
            phase_8_send_archive(topic, html, stories_in_flight, run_dir, digest_dir)

        # Phase 9: Summary
        print("\n── Phase 9: Summary ──")
        phase_9_summary(topic, fresh, recent, run_dir, digest_dir)

    except Exception as e:
        print(f"\n  FATAL: {e}")
        traceback.print_exc()
        sys.exit(1)

    overall_elapsed = time.time() - overall_start
    print(f"\n{'='*60}")
    print(f"  Digest complete in {overall_elapsed:.0f}s ({overall_elapsed/60:.1f} min)")
    print(f"{'='*60}\n")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deterministic multi-phase digest runner")
    parser.add_argument("topic", choices=list(TOPICS) + ["all"],
                        help="Topic to run (or 'all' for every topic)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run pipeline but skip email send (Phase 8)")
    args = parser.parse_args()

    if args.topic == "all":
        for cat in TOPICS:
            if cat == "agentic-platform":
                print(f"Skipping {cat} (has CC'd recipient — run manually)")
                continue
            run_digest(cat, dry_run=args.dry_run)
    else:
        run_digest(args.topic, dry_run=args.dry_run)
