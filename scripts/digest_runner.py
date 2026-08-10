#!/usr/bin/env python3
"""
Deterministic 9-phase email digest runner.

Independent model work is bounded and connected by deterministic Python contracts.
Caps, caching, and two-worker concurrency prevent one topic from starving the others
within the systemd timeout.

Architecture, stories-in-flight mechanics, and debugging:
  ~/notes/docs/homelab/email-digests.md

Usage:
    python3 ~/scripts/digest_runner.py ai-tech
    python3 ~/scripts/digest_runner.py ai-tech --dry-run
    python3 ~/scripts/digest_runner.py all

Phases:
    1. Research        — omp -p web_search (3 angles, concurrency 2)
    2. Judge Research  — batched LLM: Python date pre-tag + LLM quality filter
    3. Rank URLs       — Python: early cross-topic dedup, rank, and caps
    4. Fetch + Summarize — cached omp -p web_fetch (concurrency 2, ≤17 total)
    5. Judge Summaries — batched LLM: accuracy/fidelity check
    6. Curate          — proposal → Python validation → independent critic → state apply
    7. Write HTML      — final intro call + deterministic escaped template rendering
    8. Send + Archive  — pure Python
    9. Summary         — one lightweight LLM call
"""

import argparse
import copy
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

try:
    import yaml
except ImportError:
    yaml = None
import requests

# ── Paths ──────────────────────────────────────────────────────────────────
DIGESTS_DIR = Path.home() / "digests"
TEMPLATE_PATH = DIGESTS_DIR / "template.html"
SEND_DIGEST_SCRIPT = Path.home() / "scripts" / "send_digest.py"
DIGEST_OMP_SANDBOX = Path.home() / "scripts" / "digest-omp-sandbox.ts"
ARTICLE_CACHE_DIR = DIGESTS_DIR / ".article-cache"

# ── LLM Proxy ──────────────────────────────────────────────────────────────
LLM_PROXY_URL = "http://localhost:8081/v1/chat/completions"
MODEL = "openai-codex/gpt-5.6-luna:high"       # OMP-based primary
MODEL_FALLBACK = "mimo-v2.5"                   # API fallback via opencode-go
MODEL_REVIEWER = "deepseek-v4-flash"            # independent API critic
DEFAULT_TIMEOUT = 900
EDITORIAL_TIMEOUT = 300
INTRO_TIMEOUT = 90
RESEARCH_TIMEOUT = 1800
FETCH_TIMEOUT = 900

# ── Test mode ─────────────────────────────────────────────────────────────
TEST_MODE: bool = False
TEST_LABEL: str | None = None
MODEL_OVERRIDE: str | None = None

# ── Upstream outage tracking ──────────────────────────────────────────────
# Set by phase_1_research when ALL research angles fail — either API
# connectivity errors or HTTP 200 calls that return empty findings (degraded
# LLM stage). Checked in run_digest Phase 7 to annotate empty digests with
# an explanation instead of "No stories found today."
_UPSTREAM_OUTAGE: bool = False
_RESEARCH_FAILURES: list[str] = []
_RESEARCH_SUCCESSES: int = 0

# ── Provider detection (cached from omp models.yml) ───────────────────────
_MODEL_PROVIDER_CACHE: dict[str, dict] = {}
_OMP_MODELS_YML = Path.home() / ".omp" / "agent" / "models.yml"
_OMP_CONFIG_YML = Path.home() / ".omp" / "agent" / "config.yml"


def _load_providers() -> dict[str, dict]:
    """Load provider definitions from omp's models.yml + config.yml.

    Returns {provider_name: {baseUrl, models: set[str]}}.
    """
    providers: dict[str, dict] = {}

    # models.yml has explicit model lists per provider
    if _OMP_MODELS_YML.exists() and yaml:
        raw = yaml.safe_load(_OMP_MODELS_YML.read_text())
        for pname, pconf in (raw or {}).get("providers", {}).items():
            models = set()
            for m in (pconf.get("models") or []):
                mid = (m.get("id") or "").strip()
                if mid:
                    models.add(mid)
            providers[pname] = {
                "baseUrl": (pconf.get("baseUrl") or "").rstrip("/"),
                "models": models,
            }

    # config.yml may declare additional providers (e.g. local-llm)
    if _OMP_CONFIG_YML.exists() and yaml:
        raw = yaml.safe_load(_OMP_CONFIG_YML.read_text())
        for pname, pconf in (raw or {}).get("providers", {}).items():
            if pname not in providers:
                providers[pname] = {
                    "baseUrl": (pconf.get("baseUrl") or "").rstrip("/"),
                    "models": set(),
                }

    # Infer opencode-go provider from modelRoles if not present
    if _OMP_CONFIG_YML.exists() and yaml:
        raw = yaml.safe_load(_OMP_CONFIG_YML.read_text())
        for role, model_spec in (raw or {}).get("modelRoles", {}).items():
            if "/" in (model_spec or ""):
                prov = model_spec.split("/")[0]
                if prov not in providers:
                    providers[prov] = {
                        "baseUrl": f"http://localhost:8082/v1",
                        "models": set(),
                    }

    return providers


def _detect_model_provider(model_id: str) -> dict:
    """Return {provider, chat_url} for a model by reading omp's models.yml.

    Result is cached so the file is only parsed once.
    Falls back to local-llm on any error or unknown model.
    """
    if model_id in _MODEL_PROVIDER_CACHE:
        return _MODEL_PROVIDER_CACHE[model_id]

    providers = _load_providers()

    # Try exact model match in a provider's explicit list
    for pname, pinfo in providers.items():
        if model_id in pinfo["models"]:
            info = {
                "provider": pname,
                "chat_url": f"{pinfo['baseUrl']}/chat/completions",
            }
            _MODEL_PROVIDER_CACHE[model_id] = info
            return info

    # Handle qualified name (provider/model)
    if "/" in model_id:
        prov, mname = model_id.split("/", 1)
        if prov in providers:
            info = {
                "provider": prov,
                "chat_url": f"{providers[prov]['baseUrl']}/chat/completions",
            }
            _MODEL_PROVIDER_CACHE[model_id] = info
            return info

    # Fallback: assume opencode-go (primary API provider for digest models)
    # Previously fell back to local-llm, which caused silent routing of API models
    # (deepseek-v4-flash, mimo-v2.5) to the gaming rig's llama.cpp — resulting in
    # command failures when the local provider didn't have those models.
    if providers and "opencode-go" in providers:
        fb = {
            "provider": "opencode-go",
            "chat_url": f"{providers['opencode-go']['baseUrl']}/chat/completions",
        }
    else:
        fb = {
            "provider": "opencode-go",
            "chat_url": "http://localhost:8082/v1/chat/completions",
        }
    _MODEL_PROVIDER_CACHE[model_id] = fb
    return fb


def _effective_model(requested: str) -> str:
    """Return the effective model, respecting --model override."""
    return MODEL_OVERRIDE if MODEL_OVERRIDE else requested

# Bound independent inference calls; two overlaps latency without creating a burst workload.
MAX_PARALLEL_RESEARCH = 2
MAX_PARALLEL_FETCH = 2
ARTICLE_CACHE_TTL_HOURS = 24
ARTICLE_CACHE_VERSION = 1
FETCH_PROMPT_VERSION = 2

# ── Search Health Monitoring ──────────────────────────────────────────────
SEARXNG_URL = "http://localhost:8080"
HEALTH_LOG_PATH = DIGESTS_DIR / ".search-health.log"
MAX_ENGINE_ERRORS_BEFORE_HALT = 100  # per-engine suspended errors in 1h
MIN_WORKING_ENGINES = 2               # minimum engines returning results


def check_search_health(label: str = "") -> dict[str, Any]:
    """Check SearXNG health and return a status dict.

    Performs a test search and checks engine config. Returns:
        {
            "ok": True/False,
            "results": count,
            "engines_working": [names],
            "engines_suspended": [(name, reason), ...],
            "recent_errors": count (1h),
            "recommendation": "ok" | "warn" | "halt",
        }
    """
    status: dict[str, Any] = {
        "ok": False, "results": 0, "engines_working": [],
        "engines_suspended": [], "recent_errors": 0,
        "recommendation": "ok", "label": label,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        # 1. Test search
        resp = requests.get(
            f"{SEARXNG_URL}/search",
            params={"q": "test news today", "format": "json", "language": "en"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        status["results"] = len(results)

        engines_seen: set[str] = set()
        for r in results:
            engines_seen.add(r.get("engine", "?"))
        status["engines_working"] = sorted(engines_seen)

        unresponsive = data.get("unresponsive_engines", [])
        status["engines_suspended"] = [
            {"engine": e[0], "reason": e[1]} for e in unresponsive
        ]

        # 2. Check engine config for enabled general/web engines
        cfg_resp = requests.get(f"{SEARXNG_URL}/config?format=json", timeout=10)
        cfg = cfg_resp.json()
        enabled_general = 0
        for e in cfg.get("engines", []):
            cats = e.get("categories", [])
            if ("general" in cats or "web" in cats) and e.get("enabled"):
                enabled_general += 1
        status["enabled_general_engines"] = enabled_general

        # 3. Check recent SearXNG errors via docker logs (fast grep)
        try:
            result = subprocess.run(
                ["docker", "logs", "searxng", "--since", "1h"],
                capture_output=True, text=True, timeout=10,
            )
            error_count = result.stdout.count("ERROR:searx.engines")
            status["recent_errors"] = error_count
        except Exception:
            status["recent_errors"] = -1  # couldn't check

        # 4. Determine recommendation
        working_count = len(status["engines_working"])
        suspended_count = len(status["engines_suspended"])

        if working_count == 0 or (working_count < MIN_WORKING_ENGINES and suspended_count > 3):
            status["recommendation"] = "halt"
            status["ok"] = False
        elif suspended_count >= 3 or status.get("recent_errors", 0) > MAX_ENGINE_ERRORS_BEFORE_HALT:
            status["recommendation"] = "warn"
            status["ok"] = True
        else:
            status["recommendation"] = "ok"
            status["ok"] = True

    except Exception as e:
        status["error"] = str(e)[:200]
        status["recommendation"] = "warn"
        status["ok"] = False

    # 5. Log to health file
    try:
        with open(HEALTH_LOG_PATH, "a") as f:
            f.write(json.dumps(status) + "\n")
    except Exception:
        pass

    # 6. Print summary
    emoji = {"ok": "✓", "warn": "⚠", "halt": "✕"}.get(status["recommendation"], "?")
    print(f"  [health:{label}] {emoji} {status['results']} results from "
          f"{status['engines_working']} | "
          f"{len(status['engines_suspended'])} suspended | "
          f"{status.get('recent_errors', '?')} errors/1h | "
          f"rec: {status['recommendation']}")

    return status

# ── Batching ───────────────────────────────────────────────────────────────
BATCH_SIZE = 10  # findings/summaries per LLM call in phases 2 and 5

# ── Caps ───────────────────────────────────────────────────────────────────
FRESH_CAP = 12      # Pool A: max fresh findings passed to Phase 4
ONGOING_CAP = 5     # Pool B: max ongoing articles passed to Phase 4
SIF_CAP = 3         # Pool C: max stories-in-flight passed directly to Phase 6

# ── Stories-in-flight constants ────────────────────────────────────────────
COOL_AFTER_DAYS = 5     # auto-set status to "cooled" if no updates in 5 days
PRUNE_AFTER_DAYS = 7    # remove stories entirely after 7 days since first_seen

# ── Cross-day dedup constants ──────────────────────────────────────────────
CROSS_DAY_DEDUP_DAYS = 5  # block URLs covered in the last N days' digests;
                          # matches the ongoing research window (5 days)


# ═══════════════════════════════════════════════════════════════════════════
# Importance rubric — shared rules + per-topic specifics
# ═══════════════════════════════════════════════════════════════════════════

IMPORTANCE_RUBRIC_SHARED = (
    "IMPORTANCE RUBRIC (shared — applies to every topic):\n"
    "- high — front-page / lead-story material. Major consequence, broad impact, "
    "or significant change to the landscape. Would you open the email with it?\n"
    "- medium — notable and worth including. Meaningful to people who follow the "
    "space, but not a lead story.\n"
    "- low — incremental, niche, or minor. Worth including only on a slow day. "
    "First to get capped.\n"
)

IMPORTANCE_RUBRIC_SPECIFIC: dict[str, str] = {
    "ai-tech": (
        "PER-TOPIC IMPORTANCE:\n"
        "- high: Major model release (GPT/Claude-tier), $100M+ funding, landmark regulation, "
        "significant breach.\n"
        "- medium: New tool/feature from known player, $10M+ round, research paper with "
        "practical impact, notable acquisition.\n"
        "- low: Minor version bumps, small rounds, speculative reports, "
        "\"X announced they will announce\".\n"
    ),
    "agentic-platform": (
        "PER-TOPIC IMPORTANCE:\n"
        "- high: Breaking change to a major platform (Claude Code, Codex, Copilot), "
        "new agent architecture that meaningfully changes capabilities, critical vulnerability.\n"
        "- medium: New feature in a known platform, MCP/server tool releases, "
        "interesting benchmark result, SDK release.\n"
        "- low: Minor patch notes, small community projects, pre-announcements without substance.\n"
    ),
    "gaming": (
        "PER-TOPIC IMPORTANCE:\n"
        "- high: AAA release or announcement, major studio news (closure, acquisition), "
        "platform-shifting event, esports championship result.\n"
        "- medium: Notable indie release, significant patch/expansion, industry trend piece, "
        "hardware news.\n"
        "- low: Minor updates, DLC announcements, rumors, small esports events.\n"
    ),
    "world": (
        "PER-TOPIC IMPORTANCE:\n"
        "- high: Armed conflict escalation, major election result, natural disaster with "
        "casualties, significant policy change, international crisis.\n"
        "- medium: Diplomatic development, economic data release, legislative progress, "
        "notable protest or speech.\n"
        "- low: Process stories, incremental political maneuvering, local-interest pieces.\n"
    ),
    "ai-hardware": (
        "PER-TOPIC IMPORTANCE:\n"
        "- high: Flagship accelerator launch (NVIDIA/AMD datacenter-class), $1B+ chip or "
        "datacenter deal, export control change, major supply disruption (HBM, CoWoS, "
        "TSMC capacity).\n"
        "- medium: Notable benchmark or perf-per-watt result, consumer GPU launch, hyperscaler "
        "capex update, startup silicon milestone, memory pricing shift.\n"
        "- low: Unconfirmed leaks/rumors, minor product refreshes, incremental firmware/driver "
        "news.\n"
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
# Topic definitions
# ═══════════════════════════════════════════════════════════════════════════

TOPICS: dict[str, dict[str, Any]] = {
    "ai-tech": {
        "title": "AI & Tech Digest",
        "recipients": ["carter2099@pm.me"],
        "category": "ai-tech",
        "importance_rubric_specific": IMPORTANCE_RUBRIC_SPECIFIC["ai-tech"],
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
                    "Only include stories you actually fetched and confirmed. "
                    "Avoid low-quality aggregators (e.g. buildfastwithai.com) that repackage other outlets' content."
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
                    "and confirmed. If a source fails, try another. "
                    "Avoid low-quality aggregators (e.g. buildfastwithai.com) that repackage other outlets' content."
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
                    "and confirmed. If a source fails, try another. "
                    "Avoid low-quality aggregators (e.g. buildfastwithai.com) that repackage other outlets' content."
                ),
            },
        ],
        "judgment_rules": (
            "For each finding, evaluate against these rules and assign a verdict.\n\n"
            "1. SOURCE CHECK (HARD RULE): buildfastwithai.com is a low-quality aggregator "
            "that repackages other outlets' reporting without original content. ANY finding "
            "from buildfastwithai.com or similar aggregators MUST be dropped with reason "
            "'unreliable_source'. This takes precedence over all other rules. Only accept "
            "stories from known reputable outlets: TechCrunch, The Verge, Ars Technica, "
            "Wired, ZDNet, VentureBeat, Hacker News, official company blogs, GitHub repos "
            "with significant activity, academic papers on arxiv. Personal blogs are OK if "
            "they have substance.\n"
            "2. RELEVANCE CHECK: Is this about AI, tech, developer tools, or the tech industry? "
            "If it's general business news, politics, or non-tech topics, drop with reason 'not_relevant'.\n"
            "3. DUPLICATE CHECK: Is this the same underlying story as another finding? "
            "If yes, mark the lower-quality one as drop with reason 'duplicate_of:<other_finding_index>'.\n"
            "4. SUBSTANCE CHECK: Does this story have actual news value? Press releases with "
            "no new information, minor version bumps, and 'X company announced they will announce "
            "something' should be dropped with reason 'no_substance'.\n"
            "5. IMPORTANCE REVIEW: Review the importance label from research. Adjust if the "
            "story's significance differs from the initial estimate.\n\n"
            "CRITICAL: The date has ALREADY been checked by a pre-processor. You do NOT need "
            "to re-check dates. All findings you receive have been pre-filtered for freshness. "
            "Focus on source quality, relevance, duplicates, substance, and importance accuracy.\n\n"
            "Output each finding in the 'approved' or 'rejected' array based on your verdict."
        ),
        "categories": [
            "Model Releases", "Agentic/Agent Platforms", "Open Source",
            "Tools & Developer", "Industry News", "Policy", "Funding",
            "AI Infrastructure", "Research", "Community",
        ],
    },
    "agentic-platform": {
        "title": "Agentic Platform Digest",
        "recipients": ["carter2099@pm.me"],
        "category": "agentic-platform",
        "importance_rubric_specific": IMPORTANCE_RUBRIC_SPECIFIC["agentic-platform"],
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
            "For each finding, evaluate against these rules and assign a verdict.\n\n"
            "1. SOURCE CHECK: Reputable? Tech blogs, official docs, GitHub repos, company blogs "
            "are good. Drop content farms and low-quality aggregators.\n"
            "2. RELEVANCE CHECK: About agentic platforms, coding agents, multi-agent systems, "
            "MCP ecosystem, agent dev tooling, or AI agent research? Drop general AI news "
            "without an agent angle.\n"
            "3. DUPLICATE CHECK: Same story? Keep the best version, drop duplicates.\n"
            "4. SUBSTANCE CHECK: Actual news or meaningful analysis? Drop empty announcements.\n"
            "5. IMPORTANCE REVIEW: Review and adjust the importance label from research.\n\n"
            "CRITICAL: The date has ALREADY been checked by a pre-processor. You do NOT need "
            "to re-check dates. Focus on source, relevance, duplicates, substance, and importance.\n\n"
            "Output each finding in the 'approved' or 'rejected' array based on your verdict."
        ),
        "categories": [
            "Platform Updates", "New Features", "Launches", "MCP/Ecosystem",
            "SDKs & Frameworks", "Benchmarks", "Techniques & Patterns",
            "Research", "Evaluation", "Community Projects",
        ],
    },
    "ai-hardware": {
        "title": "AI Hardware Digest",
        "recipients": ["carter2099@pm.me"],
        "category": "ai-hardware",
        "importance_rubric_specific": IMPORTANCE_RUBRIC_SPECIFIC["ai-hardware"],
        "research_angles": [
            {
                "id": "accelerators-silicon",
                "prompt": (
                    "Search for AI accelerator and silicon news from the last 24 hours: new GPUs, "
                    "TPUs, NPUs, and custom AI ASICs from NVIDIA, AMD, Intel, Google, AWS, Meta, "
                    "Microsoft, and silicon startups (Cerebras, Groq, Tenstorrent, SambaNova). "
                    "Check Tom's Hardware (https://www.tomshardware.com/), SemiAnalysis "
                    "(https://semianalysis.com/), The Next Platform "
                    "(https://www.nextplatform.com/), Ars Technica (https://arstechnica.com/), "
                    "and Hacker News (https://news.ycombinator.com/).\n\n"
                    "For each story found, use web_fetch to read the actual article and extract:\n"
                    "- Title\n"
                    "- URL (the exact URL you fetched — do not guess or construct)\n"
                    "- Source domain (e.g. tomshardware.com)\n"
                    "- Publication date (from the article, ISO format if available)\n"
                    "- 1-2 sentence factual summary (no opinion, just what happened)\n"
                    "- Category: Accelerators & Silicon or Custom/Startup Silicon\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "If a source fails to load, try another. Prioritize stories from today. "
                    "Only include stories you actually fetched and confirmed."
                ),
            },
            {
                "id": "datacenter-infrastructure",
                "prompt": (
                    "Search for AI datacenter and infrastructure hardware news from the last 24 "
                    "hours: HBM and memory (SK Hynix, Samsung, Micron), interconnect and "
                    "networking (NVLink, InfiniBand, Ethernet, optical), servers and rack "
                    "systems, power and cooling, hyperscaler datacenter buildouts and capex, "
                    "and the fab supply chain (TSMC, CoWoS, advanced packaging). Check The Next "
                    "Platform (https://www.nextplatform.com/), ServeTheHome "
                    "(https://www.servethehome.com/), Data Center Dynamics "
                    "(https://www.datacenterdynamics.com/), SemiAnalysis "
                    "(https://semianalysis.com/), and Reuters technology "
                    "(https://www.reuters.com/technology/).\n\n"
                    "For each story found, use web_fetch to read the actual article and extract:\n"
                    "- Title\n"
                    "- URL (the exact URL you fetched — do not guess or construct)\n"
                    "- Source domain\n"
                    "- Publication date (from the article, ISO format if available)\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Memory & HBM, Networking & Interconnect, Datacenter & Power, "
                    "or Supply Chain & Fabs\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "If a source fails to load, try another. Prioritize stories from today. "
                    "Only include stories you actually fetched and confirmed."
                ),
            },
            {
                "id": "consumer-edge",
                "prompt": (
                    "Search for consumer and edge AI hardware news from the last 24 hours: "
                    "consumer GPUs (GeForce, Radeon, Arc), AI PC processors and NPUs "
                    "(Snapdragon X, Intel Core Ultra, AMD Ryzen AI), Apple silicon for local "
                    "inference, workstation and homelab AI hardware, and edge AI devices. "
                    "Check Tom's Hardware (https://www.tomshardware.com/), TechPowerUp "
                    "(https://www.techpowerup.com/), Ars Technica (https://arstechnica.com/), "
                    "The Verge (https://www.theverge.com/), and Hacker News "
                    "(https://news.ycombinator.com/).\n\n"
                    "For each story found, use web_fetch to read the actual article and extract:\n"
                    "- Title\n"
                    "- URL (the exact URL you fetched — do not guess or construct)\n"
                    "- Source domain\n"
                    "- Publication date (from the article, ISO format if available)\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Consumer & Edge\n"
                    "- Estimated importance: high / medium / low\n\n"
                    "If a source fails to load, try another. Prioritize stories from today. "
                    "Only include stories you actually fetched and confirmed."
                ),
            },
        ],
        "judgment_rules": (
            "For each finding, evaluate against these rules and assign a verdict.\n\n"
            "1. SOURCE CHECK: Is this from a known reputable outlet? Tom's Hardware, "
            "SemiAnalysis, The Next Platform, ServeTheHome, Data Center Dynamics, TechPowerUp, "
            "Ars Technica, The Verge, Reuters, Bloomberg, Hacker News, official company "
            "newsrooms and blogs. Personal blogs are OK if they have substance. Content farms, "
            "SEO spam, rumor sites with no track record, and low-quality aggregators should be "
            "dropped with reason 'unreliable_source'.\n"
            "2. RELEVANCE CHECK: Is this about hardware that enables AI — accelerators, "
            "silicon, memory, networking, datacenter infrastructure, fabs, or consumer/edge "
            "AI hardware? Pure software, model releases, and AI application news belong to "
            "other digests — drop with reason 'not_relevant'. General PC/tech news with no "
            "AI angle is also not_relevant.\n"
            "3. DUPLICATE CHECK: Is this the same underlying story as another finding? "
            "If yes, mark the lower-quality one as drop with reason 'duplicate_of:<other_finding_index>'.\n"
            "4. SUBSTANCE CHECK: Does this story have actual news value? Press releases with "
            "no new information, unconfirmed leaks without evidence, and 'X announced they "
            "will announce something' should be dropped with reason 'no_substance'.\n"
            "5. IMPORTANCE REVIEW: Review the importance label from research. Adjust if the "
            "story's significance differs from the initial estimate.\n\n"
            "CRITICAL: The date has ALREADY been checked by a pre-processor. You do NOT need "
            "to re-check dates. All findings you receive have been pre-filtered for freshness. "
            "Focus on source quality, relevance, duplicates, substance, and importance accuracy.\n\n"
            "Output each finding in the 'approved' or 'rejected' array based on your verdict."
        ),
        "categories": [
            "Accelerators & Silicon", "Custom/Startup Silicon", "Memory & HBM",
            "Networking & Interconnect", "Datacenter & Power", "Supply Chain & Fabs",
            "Consumer & Edge", "Policy & Export Controls",
        ],
    },
    "gaming": {
        "title": "Gaming Digest",
        "recipients": ["carter2099@pm.me"],
        "category": "gaming-digest",
        "importance_rubric_specific": IMPORTANCE_RUBRIC_SPECIFIC["gaming"],
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
            "For each finding, evaluate and assign a verdict.\n\n"
            "1. SOURCE CHECK: Reputable gaming press or official sources? Drop spam/content farms.\n"
            "2. RELEVANCE CHECK: About video games, gaming industry, or gaming hardware? "
            "Not general entertainment.\n"
            "3. DUPLICATE CHECK: Same story? Keep best version, drop duplicates.\n"
            "4. SUBSTANCE CHECK: 'Game X tweeted an emoji' is not news. Drop empty stories.\n"
            "5. IMPORTANCE REVIEW: Review and adjust the importance label from research.\n\n"
            "CRITICAL: The date has ALREADY been checked by a pre-processor. You do NOT need "
            "to re-check dates. Focus on source, relevance, duplicates, substance, and importance.\n\n"
            "Output each finding in the 'approved' or 'rejected' array based on your verdict."
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
        "importance_rubric_specific": IMPORTANCE_RUBRIC_SPECIFIC["world"],
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
            "For each finding, evaluate and assign a verdict.\n\n"
            "1. SOURCE CHECK: Reputable news organization? Drop blogs posing as news, "
            "content farms, and known misinformation sources.\n"
            "2. RELEVANCE CHECK: Significant U.S. or world event? Not local crime, "
            "celebrity gossip, or sports (unless major international significance).\n"
            "3. DUPLICATE CHECK: Same story? Keep best version, drop duplicates.\n"
            "4. SUBSTANCE CHECK: Is this actually news? 'Politician says something' "
            "without significant context or consequence is not news.\n"
            "5. IMPORTANCE REVIEW: Review and adjust the importance label from research.\n\n"
            "CRITICAL: The date has ALREADY been checked by a pre-processor. You do NOT need "
            "to re-check dates. Focus on source, relevance, duplicates, substance, and importance.\n\n"
            "Output each finding in the 'approved' or 'rejected' array based on your verdict."
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
# Utility: importance rubric injection
# ═══════════════════════════════════════════════════════════════════════════

def _importance_rubric_text(topic: dict) -> str:
    """Build the full importance rubric for a topic (shared + specific)."""
    specific = topic.get("importance_rubric_specific", "")
    return f"{IMPORTANCE_RUBRIC_SHARED}\n{specific}" if specific else IMPORTANCE_RUBRIC_SHARED


# ═══════════════════════════════════════════════════════════════════════════
# Utility: LLM calls
# ═══════════════════════════════════════════════════════════════════════════

def _date_context() -> str:
    """Return a date context string injected into every LLM call."""
    now = datetime.now()
    return (
        f"Today's date is {now.strftime('%Y-%m-%d')} "
        f"({now.strftime('%A')}). "
        f"The current time is {now.strftime('%H:%M')} UTC. "
        f"All date checks should use this as the reference point. "
        f"'Last 24 hours' means stories published on or after "
        f"{(now - timedelta(days=1)).strftime('%Y-%m-%d')}."
    )


def _call_llm_proxy(
    system: str,
    user: str,
    model: str = MODEL,
    temperature: float = 0.3,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Call Luna through OMP, or an API fallback through chat completions."""
    eff_model = _effective_model(model)
    if eff_model.startswith("openai-codex/"):
        return _call_omp_no_tools(system, user, eff_model, timeout)
    provider_info = _detect_model_provider(eff_model)
    date_prefix = _date_context()
    payload: dict[str, Any] = {
        "model": eff_model,
        "messages": [
            {"role": "system", "content": f"{date_prefix}\n\n{system}"},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    resp = requests.post(provider_info["chat_url"], json=payload, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    return body["choices"][0]["message"]["content"]

def _omp_agent_environment() -> dict[str, str]:
    """Return the minimal environment needed by a network-only digest agent."""
    allowed = {
        "DBUS_SESSION_BUS_ADDRESS",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TMPDIR",
        "TZ",
        "USER",
        "XDG_RUNTIME_DIR",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["HOME"] = str(Path.home())
    return env


def _call_omp_no_tools(
    system: str,
    user: str,
    model: str,
    timeout: int,
) -> str:
    """Run a transformation-only OMP call without host or web tools."""
    import tempfile

    full_system = f"{_date_context()}\n\n{system}"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="omp_digest_transform_", delete=False
    ) as tf:
        tf.write(user)
        prompt_file = tf.name

    try:
        cmd = [
            "omp", "-p",
            "--model", model,
            "--system-prompt", full_system,
            "--config", str(Path.home() / ".omp/agent/headless-override.yml"),
            "--no-session",
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-rules",
            "--no-lsp",
            "--no-pty",
            f"@{prompt_file}",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd="/tmp",
            env=_omp_agent_environment(),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"omp -p failed (rc={result.returncode}): {result.stderr[:500]}"
            )
        return result.stdout
    finally:
        try:
            Path(prompt_file).unlink()
        except OSError:
            pass


def _call_omp_p(
    prompt: str,
    model: str = MODEL,
    timeout: int = RESEARCH_TIMEOUT,
    append_system: str | None = None,
) -> str:
    """Call omp -p (headless) for steps that need web_search/web_fetch tools.

    Returns the raw stdout. Uses provider/model format so omp routes to the
    correct provider. Prompt is written to a temp file and passed via @file
    (omp doesn't reliably extract URLs from stdin; @file works correctly).
    """
    import tempfile
    eff_model = _effective_model(model)
    if "/" in eff_model:
        omp_model = eff_model
    else:
        provider_info = _detect_model_provider(eff_model)
        omp_model = f"{provider_info['provider']}/{eff_model}"
    date_prefix = _date_context()
    full_system = f"{date_prefix}\n\n{append_system}" if append_system else date_prefix

    # Write prompt to temp file — omp's web_fetch needs the URL in a file/arg,
    # not stdin, to reliably trigger the tool.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="omp_digest_", delete=False
    ) as tf:
        tf.write(prompt)
        prompt_file = tf.name

    try:
        cmd = [
            "omp", "-p",
            "--model", omp_model,
            "--session-dir", str(Path.home() / ".omp/agent/sessions-automated"),
            "--config", str(Path.home() / ".omp/agent/headless-override.yml"),
            "--append-system-prompt", full_system,
            "--tools", "read,web_search",
            "--no-extensions",
            "--extension", str(DIGEST_OMP_SANDBOX),
            "--no-skills",
            "--no-rules",
            "--no-lsp",
            "--no-pty",
            f"@{prompt_file}",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd="/tmp",
            env=_omp_agent_environment(),
        )
        if result.returncode != 0 and not result.stdout.strip():
            raise RuntimeError(f"omp -p failed (rc={result.returncode}): {result.stderr[:500]}")
        return result.stdout
    finally:
        try:
            Path(prompt_file).unlink()
        except OSError:
            pass
def _extract_json(text: str, label: str = "output") -> Any:
    """Extract JSON from LLM output. Tries markdown fences first, then raw JSON."""
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    for pattern in [r"\{[\s\S]*\}", r"\[[\s\S]*\]"]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue

    text_stripped = text.strip()
    if text_stripped.startswith("{") or text_stripped.startswith("["):
        try:
            return json.loads(text_stripped)
        except json.JSONDecodeError:
            pass

    # Fallback: scan for JSON after common introductory keywords
    # Catches cases where the model writes prose before the JSON payload
    for keyword in ["Results:", "Output:", "Findings:", "JSON:", "Here is", "Here's", "following"]:
        idx = text.find(keyword)
        if idx >= 0:
            # Try to find JSON after the keyword line
            remainder = text[idx + len(keyword):]
            for pattern in [r"\{.*\}", r"\[.*\]"]:
                m = re.search(pattern, remainder, re.DOTALL)
                if m:
                    try:
                        return json.loads(m.group(0))
                    except json.JSONDecodeError:
                        continue

    raise ValueError(f"Could not extract JSON from {label}. Raw text (first 500 chars):\n{text[:500]}")


_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref_src",
}


def _normalize_url(url: str) -> str:
    """Return a stable dedup/cache key while preserving case-sensitive URL paths."""
    raw = (url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return raw.lower().rstrip("/")
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return raw.lower().rstrip("/")
    try:
        port = parts.port
    except ValueError:
        return raw.lower().rstrip("/")
    if port and not ((parts.scheme == "http" and port == 80)
                     or (parts.scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in _TRACKING_QUERY_KEYS
    ]
    query.sort()
    suffix = f"?{urlencode(query, doseq=True)}" if query else ""
    return f"{host}{path}{suffix}"


def _article_cache_path(url: str, cache_dir: Path | None = None) -> Path:
    key = hashlib.sha256(_normalize_url(url).encode()).hexdigest()
    return (cache_dir or ARTICLE_CACHE_DIR) / f"{key}.json"


def _load_article_cache(
    url: str,
    *,
    model: str,
    cache_dir: Path | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Load a same-day article summary produced by the same model/prompt contract."""
    if not _normalize_url(url):
        return None
    path = _article_cache_path(url, cache_dir)
    try:
        entry = json.loads(path.read_text())
        fetched_at = datetime.fromisoformat(entry["fetched_at"])
        current = now or datetime.now(timezone.utc)
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age = current - fetched_at
        if not timedelta(0) <= age <= timedelta(hours=ARTICLE_CACHE_TTL_HOURS):
            return None
        if entry.get("version") != ARTICLE_CACHE_VERSION:
            return None
        if entry.get("prompt_version") != FETCH_PROMPT_VERSION:
            return None
        if entry.get("model") != _effective_model(model):
            return None
        if entry.get("normalized_url") != _normalize_url(url):
            return None
        result = entry.get("result")
        return result if isinstance(result, dict) else None
    except (AttributeError, OSError, KeyError, TypeError, ValueError):
        return None


def _save_article_cache(
    url: str,
    result: dict,
    *,
    model: str,
    cache_dir: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Atomically cache successful topic-neutral article fetch/summary output."""
    if not result.get("fetch_success", True) or not _normalize_url(url):
        return
    path = _article_cache_path(url, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "version": ARTICLE_CACHE_VERSION,
        "prompt_version": FETCH_PROMPT_VERSION,
        "model": _effective_model(model),
        "normalized_url": _normalize_url(url),
        "fetched_at": (now or datetime.now(timezone.utc)).isoformat(),
        "result": {k: v for k, v in result.items() if k != "cache_hit"},
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(json.dumps(entry, indent=2))
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _prune_article_cache(
    *,
    cache_dir: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Delete expired or malformed cache entries; return the number removed."""
    root = cache_dir or ARTICLE_CACHE_DIR
    current = now or datetime.now(timezone.utc)
    removed = 0
    try:
        paths = list(root.glob("*.json"))
    except OSError:
        return 0
    for path in paths:
        try:
            entry = json.loads(path.read_text())
            fetched_at = datetime.fromisoformat(entry["fetched_at"])
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            age = current - fetched_at
            valid = (
                timedelta(0) <= age <= timedelta(hours=ARTICLE_CACHE_TTL_HOURS)
                and entry.get("version") == ARTICLE_CACHE_VERSION
                and entry.get("prompt_version") == FETCH_PROMPT_VERSION
            )
        except (AttributeError, OSError, KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _load_cross_topic_urls(
    topic: dict,
    run_dir: Path,
    *,
    digests_dir: Path | None = None,
) -> set[str]:
    """Load URLs already selected by earlier topics for this run date."""
    blocked: set[str] = set()
    root = digests_dir or DIGESTS_DIR
    current_category = topic["category"]
    for config in TOPICS.values():
        if config["category"] == current_category:
            continue
        curated_path = root / config["category"] / run_dir.name / "06-curated.json"
        try:
            data = json.loads(curated_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for story in data.get("fresh", []) + data.get("ongoing", []):
            normalized = _normalize_url(story.get("url", ""))
            if normalized:
                blocked.add(normalized)
    return blocked


def _load_recent_covered_urls(digest_dir: Path, today: date, days: int) -> set[str]:
    """Collect URLs this digest already covered in the previous `days` days.

    Two per-day sources:
      1. <date>/06-curated.json — structured fresh+ongoing URLs from the run
         dir (authoritative; run dirs are auto-cleaned after 14 days).
      2. <date>.md — the archived Phase 9 summary, with URLs extracted from
         markdown links (fallback when the run dir no longer exists).

    Returns a set of normalized URLs. Phase 2 uses this to keep non-SIF stories
    from re-entering the digest on consecutive days (cross-day dedup).
    """
    covered: set[str] = set()
    for i in range(1, days + 1):
        day = today - timedelta(days=i)
        day_str = day.isoformat()
        curated = digest_dir / day_str / "06-curated.json"
        if curated.exists():
            try:
                data = json.loads(curated.read_text())
                for story in data.get("fresh", []) + data.get("ongoing", []):
                    url = _normalize_url(story.get("url", ""))
                    if url:
                        covered.add(url)
                continue  # structured curated data is authoritative for this day
            except (json.JSONDecodeError, ValueError):
                pass
        md_file = digest_dir / f"{day_str}.md"
        if md_file.exists():
            for m in re.finditer(r"\[[^\]]*\]\((https?://[^)\s]+)\)", md_file.read_text()):
                url = _normalize_url(m.group(1))
                if url:
                    covered.add(url)
    return covered


def _refetch_article_date(url: str, title: str) -> str | None:
    """Re-fetch an article to independently extract its publication date.

    Uses a lightweight omp -p call that only extracts the date from the page
    (no summary, no analysis). Returns date string (YYYY-MM-DD) or None on failure.
    """
    system = (
        "You are extracting a publication date from a news article. "
        "Fetch the page, find the visible publication date (article header, "
        "byline, or metadata), and output ONLY the date. Do not summarize. "
        "Be quick.\n\n"
        "Output a JSON object wrapped in ```json fences:\n"
        '{"date_confirmed": "YYYY-MM-DD"}\n\n'
        "If no publication date is visible anywhere on the page, use empty string."
    )
    prompt = (
        f"Fetch this article: {url}\n\n"
        "Extract ONLY the publication date from the page. Output the JSON."
    )
    try:
        raw = _call_omp_p(prompt, model=MODEL, timeout=600,
                         append_system=system)
        result = _extract_json(raw, f"date-refetch:{title[:40]}")
        dc = (result.get("date_confirmed") or "").strip()
        return dc if dc else None
    except Exception:
        return None



def _parse_date(date_str: str | None) -> datetime | None:
    """Parse a date string into a UTC-aware datetime. Returns None on failure."""
    if not date_str or not isinstance(date_str, str):
        return None
    for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S", "%B %d, %Y", "%b %d, %Y"]:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _batch(items: list[Any], size: int = BATCH_SIZE) -> list[list[Any]]:
    """Split items into batches of at most `size`."""
    return [items[i:i + size] for i in range(0, len(items), size)]


# ═══════════════════════════════════════════════════════════════════════════
# Phase implementations
# ═══════════════════════════════════════════════════════════════════════════

def phase_1_research(topic: dict, run_dir: Path) -> list[dict]:
    """Phase 1: Parallel research agents via omp -p.

    Each research angle gets its own omp -p call. They use web_search and
    web_fetch to find stories. Returns merged list of findings.
    """
    global _UPSTREAM_OUTAGE, _RESEARCH_FAILURES, _RESEARCH_SUCCESSES
    _RESEARCH_FAILURES = []
    _RESEARCH_SUCCESSES = 0
    _UPSTREAM_OUTAGE = False

    output_path = run_dir / "01-research-raw.json"
    if output_path.exists():
        print(f"  [skip] Phase 1 output exists: {output_path}")
        return json.loads(output_path.read_text())

    rubric = _importance_rubric_text(topic)

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
        "Target 5-8 findings. Be quick — search, compile, output JSON.\n\n"
        f"{rubric}"
    )

    def _research_one(angle: dict) -> list[dict]:
        global _RESEARCH_SUCCESSES  # += below rebinds; must be global here too
        label = f"research:{angle['id']}"
        print(f"  [run ] {label}")
        t0 = time.time()
        def _attempt() -> list[dict]:
            raw = _call_omp_p(angle["prompt"], model=MODEL, timeout=RESEARCH_TIMEOUT,
                             append_system=system_prompt)
            return _extract_json(raw, f"{label} output")

        try:
            findings = _attempt()
            failure_msg = None
        except Exception as e:
            # Per-angle retry: a failed extraction must not silently drop this
            # angle — previously the whole section was lost whenever a sibling
            # angle produced findings (the run-level fallback retry at the
            # digest level only fires when ALL angles yield zero findings).
            # Retry once with the same model.
            print(f"  [retry] {label} — attempt 1 failed: {e}; retrying once")
            check_search_health(f"retry-{angle['id']}")
            try:
                findings = _attempt()
                failure_msg = None
            except Exception as e2:
                findings = []
                failure_msg = str(e2)

        elapsed = time.time() - t0
        if findings:
            print(f"  [done] {label} — {len(findings)} findings in {elapsed:.0f}s")
            _RESEARCH_SUCCESSES += 1
        else:
            if failure_msg is None:
                # HTTP 200 but empty result — the LLM stage degraded rather than
                # finding nothing. Count as a failure so an all-empty run trips
                # the _UPSTREAM_OUTAGE annotation below instead of emailing a
                # misleading "No stories found today" digest.
                failure_msg = "empty research results (LLM returned no findings)"
            print(f"  [FAIL] {label} — {failure_msg} ({elapsed:.0f}s)")
            check_search_health(f"fail-{angle['id']}")
            _RESEARCH_FAILURES.append(failure_msg)
        # Check search health after each angle
        h = check_search_health(f"after-{angle['id']}")
        if h.get("recommendation") == "halt":
            print(f"  *** HALT during {label}: search health critical ***")
        return findings

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_RESEARCH) as pool:
        per_angle = list(pool.map(_research_one, topic["research_angles"]))
    findings = [finding for angle_findings in per_angle for finding in angle_findings]

    # Filter out non-dict artifacts (LLMs sometimes produce stray strings)
    artifacts = [f for f in findings if not isinstance(f, dict)]
    findings = [f for f in findings if isinstance(f, dict)]
    if artifacts:
        print(f"  Filtered {len(artifacts)} non-dict artifact(s): {artifacts}")

    # Detect upstream outage: all angles failed with connectivity errors,
    # OR all HTTP 200 calls returned empty findings (degraded LLM stage).
    if not findings and _RESEARCH_FAILURES and _RESEARCH_SUCCESSES == 0:
        err_msg = " ".join(_RESEARCH_FAILURES).lower()
        if any(kw in err_msg for kw in ["502", "503", "connection refused",
                                         "connection reset", "upstream",
                                         "timeout", "econnrefused",
                                         "empty research results"]):
            _UPSTREAM_OUTAGE = True
            print(f"  *** UPSTREAM OUTAGE: All {len(_RESEARCH_FAILURES)} research angle(s) "
                  f"failed (connectivity errors or empty LLM results)")

    output_path.write_text(json.dumps(findings, indent=2))
    print(f"  Phase 1 done: {len(findings)} total findings")
    return findings


def phase_2_judge_research(topic: dict, findings: list[dict], run_dir: Path,
                           stories_in_flight: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Phase 2: Python date pre-tagging + batched LLM judge.

    1. Python parses date_published → tags each finding as fresh, ongoing, or too_old.
       too_old findings are dropped without touching the LLM.
    2. If stories_in_flight is provided, active SIF entries are included in the
       judgment prompt so the LLM can reject findings that cover the same topic
       as an already-tracked story (cross-run dedup).
    3. Findings are split into batches of BATCH_SIZE.
    4. Each batch gets one LLM call with the topic's judgment rules + importance rubric.
    5. Python merges batch results, handling cross-batch duplicates.

    Returns (fresh_findings, ongoing_findings).
    """
    output_path = run_dir / "02-research-judged.json"
    if output_path.exists():
        print(f"  [skip] Phase 2 output exists: {output_path}")
        data = json.loads(output_path.read_text())
        return data.get("fresh", []), data.get("ongoing", [])

    print(f"  [run ] judge_research — {len(findings)} findings to evaluate")
    t0 = time.time()

    # ── Step 1: Python date pre-tagging ──
    now = datetime.now(timezone.utc)
    today = now.date()
    yesterday = today - timedelta(days=1)
    ongoing_cutoff_date = today - timedelta(days=5)

    pre_tagged: list[dict] = []
    too_old_count = 0

    for f in findings:
        pub_date = _parse_date(f.get("date_published"))
        if pub_date is None:
            too_old_count += 1
            continue
        pub_calendar_date = pub_date.date()
        if pub_calendar_date >= yesterday:
            f["date_tag"] = "fresh"
            pre_tagged.append(f)
        elif pub_calendar_date >= ongoing_cutoff_date:
            f["date_tag"] = "ongoing"
            pre_tagged.append(f)
        else:
            too_old_count += 1

    print(f"  Date pre-tag: {sum(1 for f in pre_tagged if f['date_tag'] == 'fresh')} fresh, "
          f"{sum(1 for f in pre_tagged if f['date_tag'] == 'ongoing')} ongoing, "
          f"{too_old_count} too_old (dropped)")

    if not pre_tagged:
        print(f"  [done] judge_research — all findings too old or no date")
        output = {"fresh": [], "ongoing": [], "rejected": []}
        output_path.write_text(json.dumps(output, indent=2))
        return [], []

    # Cross-day dedup: collect URLs this digest covered on previous days so the
    # same story can't reappear in consecutive digests unless SIF-tracked.
    cross_day_blocked = _load_recent_covered_urls(run_dir.parent, today, CROSS_DAY_DEDUP_DAYS)
    cross_day_context = ""
    if cross_day_blocked:
        print(f"  Cross-day dedup: {len(cross_day_blocked)} URLs covered in previous "
              f"{CROSS_DAY_DEDUP_DAYS} days")
        cross_day_context = (
            "## Stories Already Covered in Previous Digests (do NOT select these)\n"
            "The following URLs were already covered in this digest on a previous "
            "day. If a finding has the SAME URL as any of these, mark it as rejected "
            "with reason 'already_covered_previous_day' — the same story should not "
            "appear in consecutive digests.\n\n"
            + "\n".join(f'  - "{u}"' for u in sorted(cross_day_blocked)) + "\n\n"
        )

    # Build SIF context string for cross-run dedup
    sif_context = ""
    if stories_in_flight:
        active_sif = [s for s in stories_in_flight.get("stories", [])
                      if s.get("status") == "active"]
        if active_sif:
            sif_lines = []
            for s in active_sif:
                title = s.get("title", "?")[:80]
                sif_lines.append(f'  - "{title}"')
            sif_context = (
                "## Stories Already in Flight (do NOT re-add these topics)\n"
                "The following stories are already being tracked from previous days. "
                "If a finding covers the SAME TOPIC as any of these (even with a "
                "different URL or from a different source), mark it as rejected "
                "with reason 'already_tracked_in_sif'. The same underlying story "
                "should not appear as a fresh finding — ongoing updates go through "
                "the stories-in-flight tracker, not via new fresh entries.\n\n"
                + "\n".join(sif_lines) + "\n\n"
            )

    # ── Step 2: Batch LLM calls ──
    rubric = _importance_rubric_text(topic)
    batches = _batch(pre_tagged, BATCH_SIZE)
    print(f"  Batched into {len(batches)} LLM call(s) ({BATCH_SIZE}/batch)")

    all_approved: list[dict] = []
    all_rejected: list[dict] = []

    system = (
        "You are a strict editor for a daily news digest. Your job is to filter "
        "research findings against quality rules. Be harsh — a false positive (bad "
        "story included) is worse than a false negative (good story missed).\n\n"
        "You will receive a JSON array of research findings and a set of rules. "
        "For each finding, evaluate it against every rule and output a verdict.\n\n"
        "Output a JSON object with two arrays wrapped in ```json fences:\n"
        '  {\n'
        '    "approved": [<findings that pass all quality checks>],\n'
        '    "rejected": [{"finding": ..., "reason": "..."}, ...]\n'
        '  }\n'
    )

    for batch_idx, batch in enumerate(batches):
        batch_json = json.dumps(batch, indent=2)
        user = (
            f"{cross_day_context}"
            f"{sif_context}"
            f"## Rules\n\n{topic['judgment_rules']}\n\n"
            f"## Importance Rubric\n\n{rubric}\n\n"
            f"## Findings to evaluate (batch {batch_idx + 1}/{len(batches)})\n\n"
            f"{batch_json}\n\n"
            "Evaluate each finding against every rule. Output the approved and "
            "rejected arrays in ```json fences. Include a clear reason for each rejection."
        )

        try:
            raw = _call_llm_proxy(system, user, model=MODEL)
            result = _extract_json(raw, f"judge_research batch {batch_idx + 1}")
            batch_approved = result.get("approved", [])
            batch_rejected = result.get("rejected", [])
            # Normalize: LLM sometimes returns bare strings instead of dicts
            batch_approved = [f if isinstance(f, dict) else {"title": str(f)} for f in batch_approved]
            batch_rejected = [r if isinstance(r, dict) else {"finding": {"title": str(r)}, "reason": "unknown"} for r in batch_rejected]
            all_approved.extend(batch_approved)
            all_rejected.extend(batch_rejected)
            print(f"  Batch {batch_idx + 1}: {len(batch_approved)} approved, {len(batch_rejected)} rejected")
        except Exception as e:
            print(f"  [FAIL] judge_research batch {batch_idx + 1} — {e}, treating all as approved")
            all_approved.extend(batch)

    # ── Step 3: Python merge — cross-batch + cross-day duplicate detection ──
    seen_urls: set[str] = set()
    deduped_approved: list[dict] = []
    dedup_rejected: list[dict] = []

    for f in all_approved:
        url = _normalize_url(f.get("url", ""))
        if url and url in seen_urls:
            dedup_rejected.append({"finding": f, "reason": "cross_batch_duplicate"})
        elif url and url in cross_day_blocked:
            dedup_rejected.append({"finding": f, "reason": "already_covered_previous_day"})
        else:
            if url:
                seen_urls.add(url)
            deduped_approved.append(f)

    if dedup_rejected:
        n_cross_day = sum(1 for r in dedup_rejected
                          if r.get("reason") == "already_covered_previous_day")
        n_batch = len(dedup_rejected) - n_cross_day
        if n_batch:
            print(f"  Cross-batch dedup: removed {n_batch} duplicates")
        if n_cross_day:
            print(f"  Cross-day dedup: removed {n_cross_day} stories already covered on previous days")

    # Split by date_tag
    fresh = [f for f in deduped_approved if f.get("date_tag") == "fresh"]
    ongoing = [f for f in deduped_approved if f.get("date_tag") == "ongoing"]

    elapsed = time.time() - t0
    print(f"  [done] judge_research — {len(fresh)} fresh, {len(ongoing)} ongoing, "
          f"{len(all_rejected) + len(dedup_rejected)} rejected ({elapsed:.0f}s)")
    for r in all_rejected[:5]:
        finding = r.get("finding", {}) if isinstance(r, dict) else {}
        reason = r.get("reason", "unspecified") if isinstance(r, dict) else "unknown"
        title = finding.get('title', '?') if isinstance(finding, dict) else str(finding)[:60]
        print(f"    ✗ {title[:60]}: {reason}")
    if len(all_rejected) > 5:
        print(f"    ... and {len(all_rejected) - 5} more rejected")

    output = {"fresh": fresh, "ongoing": ongoing, "rejected": all_rejected + dedup_rejected}
    output_path.write_text(json.dumps(output, indent=2))
    return fresh, ongoing


def phase_3_rank(
    topic: dict,
    fresh: list[dict],
    ongoing: list[dict],
    stories_in_flight: dict,
    run_dir: Path,
) -> tuple[list[dict], list[dict]]:
    """Phase 3: Python-side ranking with caps.

    Pool A: Fresh findings
      - Sort by importance (high → med → low), date_published recency as tiebreaker
      - Cap: FRESH_CAP (12)

    Pool B: Ongoing articles (2-5 day old articles from Phase 2)
      - Sort by date_published recency primary, importance as tiebreaker
      - Cap: ONGOING_CAP (5)

    Pool C: Stories-in-flight — does NOT enter Phase 4
      - Sort by last_updated descending
      - Cap: SIF_CAP (3)
      - Passed directly to Phase 6 with existing summaries + latest_dev fields

    Returns (phase_4_queue, sif_candidates).
    Phase 4 queue = Pool A + Pool B, with fresh first.
    """
    output_path = run_dir / "03-urls-ranked.json"
    if output_path.exists():
        print(f"  [skip] Phase 3 output exists: {output_path}")
        data = json.loads(output_path.read_text())
        return data.get("phase_4_queue", []), data.get("sif_candidates", [])

    importance_order = {"high": 0, "medium": 1, "low": 2}

    # Tag each finding with source_verdict for downstream phases
    for f in fresh:
        f["source_verdict"] = "fresh"
    for o in ongoing:
        o["source_verdict"] = "ongoing"

    # Remove stories already selected by an earlier topic before any article fetch.
    other_topic_urls = _load_cross_topic_urls(topic, run_dir)
    cross_topic_rejected = [
        {**item, "rejection_reason": "already selected by another digest today"}
        for item in fresh + ongoing
        if _normalize_url(item.get("url", "")) in other_topic_urls
    ]
    eligible_fresh = [
        item for item in fresh
        if _normalize_url(item.get("url", "")) not in other_topic_urls
    ]
    eligible_ongoing = [
        item for item in ongoing
        if _normalize_url(item.get("url", "")) not in other_topic_urls
    ]
    if cross_topic_rejected:
        print(f"  [Phase 3 cross-dedup] skipped {len(cross_topic_rejected)} "
              "already-selected URL(s) before fetch")

    # Pool A: stable importance-first ranking, then publication recency.
    pool_a = sorted(eligible_fresh, key=lambda f: f.get("date_published", ""), reverse=True)
    pool_a = sorted(pool_a, key=lambda f: importance_order.get(f.get("importance", "low"), 2))
    pool_a = pool_a[:FRESH_CAP]

    # Pool B: publication recency first, then importance.
    pool_b = sorted(eligible_ongoing,
                    key=lambda f: importance_order.get(f.get("importance", "low"), 2))
    pool_b = sorted(pool_b, key=lambda f: f.get("date_published", ""), reverse=True)
    pool_b = pool_b[:ONGOING_CAP]

    # Pool C bypasses Phase 4 but still obeys the same cross-topic exclusion.
    active_sif = [
        story for story in stories_in_flight.get("stories", [])
        if story.get("status") == "active"
        and _normalize_url(story.get("url", "")) not in other_topic_urls
    ]
    pool_c = sorted(
        active_sif, key=lambda s: s.get("last_updated", ""), reverse=True
    )[:SIF_CAP]

    phase_4_queue = pool_a + pool_b

    output = {
        "phase_4_queue": phase_4_queue,
        "sif_candidates": pool_c,
        "pool_a": pool_a,
        "pool_b": pool_b,
        "cross_topic_rejected": cross_topic_rejected,
    }
    output_path.write_text(json.dumps(output, indent=2))
    print(f"  Phase 3 done: Pool A={len(pool_a)} fresh, Pool B={len(pool_b)} ongoing, "
          f"Pool C={len(pool_c)} SIF (bypass Phase 4) → {len(phase_4_queue)} total for fetch")
    return phase_4_queue, pool_c

def phase_4_fetch(topic: dict, findings: list[dict], run_dir: Path) -> list[dict]:
    """Fetch and summarize articles with a shared cache and two-worker bound."""
    output_path = run_dir / "04-fetch-summaries.json"
    if output_path.exists():
        print(f"  [skip] Phase 4 output exists: {output_path}")
        return json.loads(output_path.read_text())
    pruned_cache_entries = _prune_article_cache()
    if pruned_cache_entries:
        print(f"  [cache] pruned {pruned_cache_entries} expired/invalid entry(s)")

    system_prompt = (
        "You are a research assistant. Read ONE article via web_fetch and produce a "
        "topic-neutral, detailed factual summary. Do not search.\n\n"
        "Output one JSON object in ```json fences with these fields:\n"
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
        source = finding.get("source_verdict", "?")
        cached = _load_article_cache(url, model=MODEL)
        if cached is not None:
            print(f"  [cache] [{source}] {label}")
            return {**finding, **cached, "url": url, "cache_hit": True}

        print(f"  [run ] [{source}] {label}")
        started = time.time()
        prompt = (
            f"Fetch this article: {url}\n\n"
            f"Title from research: {title}\n\n"
            "Use web_fetch to read the article. Then output your summary as JSON "
            "wrapped in ```json fences."
        )
        try:
            raw = _call_omp_p(
                prompt, model=MODEL, timeout=FETCH_TIMEOUT,
                append_system=system_prompt,
            )
            result = _extract_json(raw, f"{label} output")
            if not isinstance(result, dict):
                raise ValueError(
                    f"fetch output is not a JSON object (got {type(result).__name__})")
            result["url"] = url
            try:
                _save_article_cache(url, result, model=MODEL)
            except OSError as cache_error:
                print(f"  [cache warn] {label} — {cache_error}")
            elapsed = time.time() - started
            status = "✓" if result.get("fetch_success", True) else "✗"
            print(f"  [done] {label} — {status} ({elapsed:.0f}s)")
            return {**finding, **result, "url": url, "cache_hit": False}
        except Exception as error:
            elapsed = time.time() - started
            print(f"  [FAIL] {label} — {error} ({elapsed:.0f}s)")
            return {
                **finding,
                "fetch_success": False,
                "summary": f"Fetch failed: {str(error)[:100]}",
                "key_details": [],
                "date_confirmed": "",
                "author": "",
                "cache_hit": False,
            }

    workers = min(MAX_PARALLEL_FETCH, max(1, len(findings)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_fetch_one, findings))

    output_path.write_text(json.dumps(results, indent=2))
    successful = sum(1 for result in results if result.get("fetch_success", True))
    cache_hits = sum(1 for result in results if result.get("cache_hit"))
    print(f"  Phase 4 done: {successful}/{len(results)} fetches successful, "
          f"{cache_hits} cache hit(s), concurrency={workers}")
    return results


def phase_5_judge_summaries(topic: dict, summaries: list[dict], run_dir: Path) -> list[dict]:
    """Phase 5: Python date validation + batched LLM judge of summary accuracy.

    1. Python validates date_confirmed against calendar thresholds,
       cross-referencing with source_verdict (set by Phase 3):
       - date >= yesterday → ok (fresh, as expected)
       - date 2-5 days old + source_verdict=ongoing → ok (legitimate Pool B)
       - date 2-5 days old + source_verdict=fresh → drop (Phase 1/2 misclassified)
       - date >5 days old → auto-drop regardless
       - date missing → targeted re-fetch for date extraction, then re-check
    2. Surviving summaries go through batched LLM judge for faithfulness
       and completeness (date already verified, not re-checked).
    3. Python merges results.
    """
    output_path = run_dir / "05-summaries-judged.json"
    if output_path.exists():
        print(f"  [skip] Phase 5 output exists: {output_path}")
        return json.loads(output_path.read_text())

    to_judge = [s for s in summaries if s.get("fetch_success", True)]
    failed = [s for s in summaries if not s.get("fetch_success", True)]

    if not to_judge:
        print("  Phase 5: no successful fetches to judge")
        return summaries

    print(f"  [run ] judge_summaries — {len(to_judge)} summaries to evaluate")
    t0 = time.time()

    # ── Step 1: Python date validation ──
    # Uses date_confirmed from Phase 4's actual article fetch — an independent
    # source from Phase 1's date_published. Cross-references with source_verdict
    # (set by Phase 3) to avoid penalizing legitimate ongoing articles.
    # Re-fetches only when date_confirmed is missing.
    now = datetime.now(timezone.utc)
    today = now.date()
    yesterday = today - timedelta(days=1)
    stale_cutoff = today - timedelta(days=5)

    validated: list[dict] = []
    date_dropped: list[dict] = []
    need_refetch: list[dict] = []

    for s in to_judge:
        dc = (s.get("date_confirmed") or "").strip()
        parsed = _parse_date(dc)
        source = s.get("source_verdict", "fresh")
        if parsed is not None:
            d = parsed.date()
            if d >= yesterday:
                validated.append(s)
            elif d >= stale_cutoff and source == "ongoing":
                # Legitimate ongoing article — was intentionally included in Pool B
                validated.append(s)
            elif d >= stale_cutoff and source == "fresh":
                # Phase 1/2 tagged as fresh but Phase 4's fetch shows it's 2-5d old
                age = (today - d).days
                s["judge_verdict"] = "drop"
                s["judge_issues"] = [f"date_mismatch: tagged fresh but confirmed {dc} is {age}d old"]
                date_dropped.append(s)
            else:
                age = (today - d).days
                s["judge_verdict"] = "drop"
                s["judge_issues"] = [f"date_stale: confirmed {dc} is {age}d old (>5d cutoff)"]
                date_dropped.append(s)
        else:
            need_refetch.append(s)

    # Re-fetch dates for articles where Phase 4 didn't extract one
    if need_refetch:
        print(f"  Date validation: {len(need_refetch)} article(s) need date re-fetch")
        for s in need_refetch:
            url = s.get("url", "")
            title = s.get("title", "unknown")
            label = f"date-refetch:{title[:40]}"
            print(f"  [run ] {label}")
            t_refetch = time.time()
            try:
                refetched = _refetch_article_date(url, title)
                elapsed = time.time() - t_refetch
                source = s.get("source_verdict", "fresh")
                if refetched:
                    s["date_confirmed"] = refetched
                    parsed = _parse_date(refetched)
                    if parsed:
                        d = parsed.date()
                        if d >= yesterday:
                            validated.append(s)
                            print(f"  [done] {label} → {refetched} (fresh) ({elapsed:.0f}s)")
                        elif d >= stale_cutoff and source == "ongoing":
                            validated.append(s)
                            print(f"  [done] {label} → {refetched} (ok, ongoing) ({elapsed:.0f}s)")
                        elif d >= stale_cutoff and source == "fresh":
                            age = (today - d).days
                            s["judge_verdict"] = "drop"
                            s["judge_issues"] = [f"date_mismatch: tagged fresh but confirmed {refetched} is {age}d old"]
                            date_dropped.append(s)
                            print(f"  [done] {label} → {refetched} (mismatch, auto-dropped) ({elapsed:.0f}s)")
                        else:
                            age = (today - d).days
                            s["judge_verdict"] = "drop"
                            s["judge_issues"] = [f"date_stale: confirmed {refetched} is {age}d old (>5d cutoff)"]
                            date_dropped.append(s)
                            print(f"  [done] {label} → {refetched} (stale, auto-dropped) ({elapsed:.0f}s)")
                    else:
                        validated.append(s)
                        print(f"  [done] {label} → unparseable, passing to LLM ({elapsed:.0f}s)")
                else:
                    validated.append(s)
                    print(f"  [done] {label} → no date found, passing to LLM ({elapsed:.0f}s)")
            except Exception as e:
                elapsed = time.time() - t_refetch
                print(f"  [FAIL] {label} — {e} ({elapsed:.0f}s), passing to LLM")
                validated.append(s)

    print(f"  Date validation: {len(validated)} pass, "
          f"{len(date_dropped)} auto-dropped (stale/mismatch), {len(need_refetch)} refetched")

    # ── Step 2: LLM judge (date pre-validated, no speculative DATE_CHECK) ──
    if validated:
        batches = _batch(validated, BATCH_SIZE)
        print(f"  Batched into {len(batches)} LLM call(s) ({BATCH_SIZE}/batch)")
    else:
        batches = []

    system = (
        "You are a strict editor verifying AI-written summaries. You receive article "
        "summaries and judge whether each is accurate and faithful to what the article "
        "likely contains.\n\n"
        "NOTE: Publication dates have ALREADY been independently verified by fetching "
        "each article and extracting its visible publication date. Do NOT re-check dates.\n\n"
        "For each summary, evaluate:\n"
        "1. FAITHFULNESS: Does the summary contain plausible facts, or does it read "
        "like hallucinated/generic filler? Signs of hallucination: vague claims without "
        "specifics, details that seem wrong for the source, overly confident statements "
        "that sound made up.\n"
        "2. COMPLETENESS: Does the summary capture what the article is actually about? "
        "A summary that misses the main point is unhelpful.\n"
        "3. OVERALL: verdict = 'keep' | 'fix' (minor issues, note them) | 'drop' "
        "(unrecoverable — hallucinated, wrong, or empty)\n\n"
        "Output a JSON array of judgments wrapped in ```json fences, one per summary:\n"
        '  [{"url": "...", "verdict": "keep|fix|drop", "issues": ["issue 1", ...], '
        '"fixed_summary": "if fix, corrected summary, else empty"}, ...]\n\n'
        "Be suspicious. Summaries that sound too generic or lack specific names, "
        "numbers, or concrete claims are likely hallucinated — drop them."
    )

    all_judgments: list[dict] = []

    for batch_idx, batch in enumerate(batches):
        batch_json = json.dumps(batch, indent=2)
        user = (
            f"## Summaries to judge (batch {batch_idx + 1}/{len(batches)})\n\n"
            f"{batch_json}\n\n"
            "Judge each summary. Output a JSON array of judgments in ```json fences. "
            "Err on the side of dropping questionable summaries."
        )

        try:
            raw = _call_llm_proxy(system, user, model=MODEL)
            judgments = _extract_json(raw, f"judge_summaries batch {batch_idx + 1}")
            if not isinstance(judgments, list):
                judgments = [judgments]
            all_judgments.extend(judgments)
            print(f"  Batch {batch_idx + 1}: {len(judgments)} judgments received")
        except Exception as e:
            print(f"  [FAIL] judge_summaries batch {batch_idx + 1} — {e}, keeping all in batch")
            for s in batch:
                all_judgments.append({"url": s.get("url", ""), "verdict": "keep", "issues": [], "fixed_summary": ""})

    # ── Step 3: Apply judgments ──
    judged_map = {j.get("url", ""): j for j in all_judgments}
    results = []
    for s in summaries:
        url = s.get("url", "")
        # Preserve pre-set verdicts from date validation (already dropped)
        if s.get("judge_verdict"):
            results.append(s)
            continue
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

    output_path.write_text(json.dumps(results, indent=2))
    return results


def _editorial_candidate_id(candidate: dict) -> str:
    identity = _normalize_url(candidate.get("url", "")) or candidate.get("title", "")
    return f"candidate-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"


def _clean_editorial_text(value: Any, fallback: str = "", limit: int = 1200) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return (text or fallback.strip())[:limit]


def _summarize_model_error(error: Exception) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        return f"timed out after {error.timeout}s"
    return " ".join(str(error).split())[:500]


def _prepare_editorial_candidates(
    summaries: list[dict],
    blocked_urls: set[str],
) -> tuple[list[dict], list[dict]]:
    kept = [item for item in summaries if item.get("judge_verdict") in ("keep", "fix")]
    rejected = [item for item in summaries if item.get("judge_verdict") == "drop"]
    seen: set[str] = set()
    eligible: list[dict] = []
    for item in kept:
        normalized = _normalize_url(item.get("url", ""))
        if not normalized or normalized in seen or normalized in blocked_urls:
            continue
        seen.add(normalized)
        candidate = copy.deepcopy(item)
        candidate["candidate_id"] = _editorial_candidate_id(candidate)
        eligible.append(candidate)

    importance_order = {"high": 0, "medium": 1, "low": 2}
    eligible = sorted(
        eligible, key=lambda item: item.get("date_published", ""), reverse=True
    )
    eligible = sorted(
        eligible,
        key=lambda item: importance_order.get(item.get("importance", "low"), 2),
    )
    return eligible[:15], rejected


def _validate_editorial_proposal(
    proposal: dict,
    candidates: list[dict],
    sif_candidates: list[dict],
    stories_in_flight: dict,
    blocked_urls: set[str] | None = None,
) -> tuple[dict, list[str]]:
    """Validate model IDs/transitions and return a bounded, source-backed proposal."""
    if not isinstance(proposal, dict):
        raise ValueError("editorial proposal must be a JSON object")

    blocked = blocked_urls or set()
    warnings: list[str] = []
    candidate_by_id = {
        candidate["candidate_id"]: candidate
        for candidate in candidates
        if candidate.get("candidate_id")
    }
    tracker_by_url = {
        _normalize_url(story.get("url", "")): story
        for story in stories_in_flight.get("stories", [])
        if _normalize_url(story.get("url", ""))
    }
    ongoing_by_url = {
        _normalize_url(story.get("url", "")): story
        for story in sif_candidates
        if _normalize_url(story.get("url", ""))
        and _normalize_url(story.get("url", "")) not in blocked
    }

    fresh: list[dict] = []
    selected_ids: set[str] = set()
    raw_fresh = proposal.get("selected_fresh", [])
    if not isinstance(raw_fresh, list):
        warnings.append("selected_fresh was not a list")
        raw_fresh = []
    for item in raw_fresh:
        if not isinstance(item, dict):
            warnings.append("ignored non-object fresh selection")
            continue
        candidate_id = item.get("candidate_id", "")
        source = candidate_by_id.get(candidate_id)
        if source is None:
            warnings.append(f"ignored unknown candidate_id {candidate_id!r}")
            continue
        normalized = _normalize_url(source.get("url", ""))
        if normalized in blocked:
            warnings.append(f"ignored cross-topic duplicate {candidate_id}")
            continue
        if candidate_id in selected_ids:
            warnings.append(f"ignored duplicate fresh selection {candidate_id}")
            continue
        selected_ids.add(candidate_id)
        related = _normalize_url(item.get("related_story_url", ""))
        if related and related not in tracker_by_url:
            warnings.append(f"removed unknown related story from {candidate_id}")
            related = ""
        fresh.append({
            "candidate_id": candidate_id,
            "rank": len(fresh) + 1,
            "editorial_summary": _clean_editorial_text(
                item.get("editorial_summary", item.get("summary")),
                source.get("summary", ""),
            ),
            "selection_reason": _clean_editorial_text(
                item.get("selection_reason", item.get("reason")), limit=400
            ),
            "related_story_url": tracker_by_url[related].get("url", "") if related else None,
        })
        if len(fresh) == 7:
            if len(raw_fresh) > 7:
                warnings.append("capped selected_fresh at 7")
            break

    ongoing: list[dict] = []
    selected_ongoing: set[str] = set()
    fresh_urls = {
        _normalize_url(candidate_by_id[item["candidate_id"]].get("url", ""))
        for item in fresh
    }
    raw_ongoing = proposal.get("selected_ongoing", [])
    if not isinstance(raw_ongoing, list):
        warnings.append("selected_ongoing was not a list")
        raw_ongoing = []
    for item in raw_ongoing:
        if not isinstance(item, dict):
            warnings.append("ignored non-object ongoing selection")
            continue
        normalized = _normalize_url(item.get("story_url", item.get("url", "")))
        source = ongoing_by_url.get(normalized)
        if source is None:
            warnings.append(f"ignored unknown ongoing story {normalized!r}")
            continue
        if normalized in selected_ongoing or normalized in fresh_urls:
            warnings.append(f"ignored duplicate ongoing story {normalized}")
            continue
        selected_ongoing.add(normalized)
        ongoing.append({
            "story_url": source.get("url", ""),
            "rank": len(ongoing) + 1,
            "summary": _clean_editorial_text(
                item.get("summary"), source.get("latest_dev", "")
            ),
            "why_still_relevant": _clean_editorial_text(
                item.get("why_still_relevant"), source.get("latest_dev", ""), 600
            ),
        })
        if len(ongoing) == 3:
            if len(raw_ongoing) > 3:
                warnings.append("capped selected_ongoing at 3")
            break

    state_proposals: list[dict] = []
    raw_state = proposal.get(
        "story_state_proposals", proposal.get("state_proposals", [])
    )
    if not isinstance(raw_state, list):
        warnings.append("story_state_proposals was not a list")
        raw_state = []
    for item in raw_state:
        if not isinstance(item, dict):
            warnings.append("ignored non-object state proposal")
            continue
        operation = item.get("operation")
        evidence = item.get("evidence_candidate_ids", [])
        if not isinstance(evidence, list):
            evidence = []
        evidence = [
            candidate_id for candidate_id in evidence
            if candidate_id in selected_ids
        ]
        latest_dev = _clean_editorial_text(item.get("latest_dev"), limit=800)
        importance = item.get("importance", "medium")
        if importance not in ("high", "medium", "low"):
            importance = "medium"
        status = item.get("status", "active")
        if status not in ("active", "cooled"):
            status = "active"

        if operation == "add":
            candidate_id = item.get("candidate_id", "")
            if candidate_id not in selected_ids:
                warnings.append(
                    f"ignored tracker add for unselected candidate {candidate_id!r}")
                continue
            source = candidate_by_id[candidate_id]
            if _normalize_url(source.get("url", "")) in tracker_by_url:
                warnings.append(f"ignored tracker add for existing story {candidate_id}")
                continue
            state_proposals.append({
                "operation": "add",
                "candidate_id": candidate_id,
                "evidence_candidate_ids": [candidate_id],
                "latest_dev": latest_dev or source.get("summary", ""),
                "importance": importance,
                "status": "active",
            })
        elif operation == "update":
            normalized = _normalize_url(item.get("story_url", ""))
            source = tracker_by_url.get(normalized)
            if source is None or not evidence or not latest_dev:
                warnings.append(
                    f"ignored unsupported tracker update for {normalized!r}")
                continue
            state_proposals.append({
                "operation": "update",
                "story_url": source.get("url", ""),
                "evidence_candidate_ids": evidence,
                "latest_dev": latest_dev,
                "importance": importance,
                "status": status,
            })
        else:
            warnings.append(f"ignored unknown state operation {operation!r}")

    domains: dict[str, int] = {}
    for item in fresh:
        source = candidate_by_id[item["candidate_id"]]
        domain = source.get("source_domain") or urlsplit(source.get("url", "")).hostname or ""
        domains[domain] = domains.get(domain, 0) + 1
    concentrated = sorted(domain for domain, count in domains.items() if domain and count > 2)
    if concentrated:
        warnings.append(f"source concentration above 2: {', '.join(concentrated)}")
    if candidates and not fresh:
        warnings.append("proposal selected no valid fresh stories")

    return {
        "selected_fresh": fresh,
        "selected_ongoing": ongoing,
        "story_state_proposals": state_proposals,
        "rejected": proposal.get("rejected", []),
        "gaps": _clean_editorial_text(proposal.get("gaps"), limit=800),
        "balance_summary": _clean_editorial_text(
            proposal.get("balance_summary"), limit=600
        ),
    }, warnings


def _raw_editorial_proposal(
    candidates: list[dict],
    sif_candidates: list[dict],
) -> dict:
    """Build a source-only last-resort proposal after both curation models fail."""
    return {
        "selected_fresh": [
            {
                "candidate_id": candidate["candidate_id"],
                "rank": index,
                "editorial_summary": candidate.get("summary", ""),
                "selection_reason": "deterministic fallback",
                "related_story_url": None,
            }
            for index, candidate in enumerate(candidates[:7], 1)
        ],
        "selected_ongoing": [
            {
                "story_url": story.get("url", ""),
                "rank": index,
                "summary": story.get("latest_dev", ""),
                "why_still_relevant": story.get("latest_dev", ""),
            }
            for index, story in enumerate(sif_candidates[:3], 1)
        ],
        "story_state_proposals": [],
        "rejected": [],
        "gaps": "Curation models unavailable; source-ranked fallback used.",
        "balance_summary": "",
    }


def _apply_editorial_patches(
    proposal: dict,
    review: dict,
) -> tuple[dict, list[dict], list[str]]:
    """Apply only the critic's bounded list operations; validation follows."""
    patched = copy.deepcopy(proposal)
    applied: list[dict] = []
    warnings: list[str] = []
    changes = review.get("changes", [])
    if not isinstance(changes, list):
        return patched, applied, ["critic changes was not a list"]

    def replace_by(items: list[dict], key: str, value: str, replacement: dict) -> bool:
        for index, item in enumerate(items):
            current = item.get(key, "")
            matches = (
                _normalize_url(current) == _normalize_url(value)
                if key == "story_url" else current == value
            )
            if matches:
                items[index] = replacement
                return True
        return False

    for change in changes[:20]:
        if not isinstance(change, dict):
            warnings.append("ignored non-object critic change")
            continue
        operation = change.get("operation")
        item = change.get("item")
        changed = False
        if operation == "remove_fresh":
            candidate_id = change.get("candidate_id", "")
            before = len(patched["selected_fresh"])
            patched["selected_fresh"] = [
                entry for entry in patched["selected_fresh"]
                if entry.get("candidate_id") != candidate_id
            ]
            changed = len(patched["selected_fresh"]) != before
        elif operation == "add_fresh" and isinstance(item, dict):
            patched["selected_fresh"].append(item)
            changed = True
        elif operation == "replace_fresh" and isinstance(item, dict):
            changed = replace_by(
                patched["selected_fresh"], "candidate_id",
                change.get("candidate_id", ""), item,
            )
        elif operation == "move_fresh":
            candidate_id = change.get("candidate_id", "")
            position = change.get("position")
            if isinstance(position, int) and position >= 1:
                matches = [
                    entry for entry in patched["selected_fresh"]
                    if entry.get("candidate_id") == candidate_id
                ]
                if matches:
                    patched["selected_fresh"] = [
                        entry for entry in patched["selected_fresh"]
                        if entry.get("candidate_id") != candidate_id
                    ]
                    patched["selected_fresh"].insert(
                        min(position - 1, len(patched["selected_fresh"])), matches[0]
                    )
                    changed = True
        elif operation == "remove_ongoing":
            story_url = change.get("story_url", "")
            before = len(patched["selected_ongoing"])
            patched["selected_ongoing"] = [
                entry for entry in patched["selected_ongoing"]
                if _normalize_url(entry.get("story_url", "")) != _normalize_url(story_url)
            ]
            changed = len(patched["selected_ongoing"]) != before
        elif operation == "add_ongoing" and isinstance(item, dict):
            patched["selected_ongoing"].append(item)
            changed = True
        elif operation == "replace_ongoing" and isinstance(item, dict):
            changed = replace_by(
                patched["selected_ongoing"], "story_url",
                change.get("story_url", ""), item,
            )
        elif operation == "remove_state":
            index = change.get("index")
            if isinstance(index, int) and 0 <= index < len(patched["story_state_proposals"]):
                patched["story_state_proposals"].pop(index)
                changed = True
        elif operation == "add_state" and isinstance(item, dict):
            patched["story_state_proposals"].append(item)
            changed = True
        elif operation == "replace_state" and isinstance(item, dict):
            index = change.get("index")
            if isinstance(index, int) and 0 <= index < len(patched["story_state_proposals"]):
                patched["story_state_proposals"][index] = item
                changed = True
        if changed:
            applied.append(change)
        else:
            warnings.append(f"ignored invalid critic operation {operation!r}")
    return patched, applied, warnings


def _apply_story_state_proposals(
    stories_in_flight: dict,
    proposal: dict,
    candidates: list[dict],
    today_str: str,
) -> dict:
    """Apply only validated add/update operations to a copied tracker."""
    updated = copy.deepcopy(stories_in_flight)
    stories = updated.setdefault("stories", [])
    candidate_by_id = {
        candidate["candidate_id"]: candidate for candidate in candidates
    }
    story_by_url = {
        _normalize_url(story.get("url", "")): story
        for story in stories
        if _normalize_url(story.get("url", ""))
    }
    for operation in proposal.get("story_state_proposals", []):
        if operation["operation"] == "add":
            source = candidate_by_id.get(operation["candidate_id"])
            if source is None:
                continue
            normalized = _normalize_url(source.get("url", ""))
            if normalized in story_by_url:
                continue
            story = {
                "title": source.get("title", ""),
                "url": source.get("url", ""),
                "category": source.get("category", ""),
                "status": "active",
                "latest_dev": operation.get("latest_dev", source.get("summary", "")),
                "last_updated": today_str,
                "importance": operation.get("importance", source.get("importance", "medium")),
                "first_seen": today_str,
            }
            stories.append(story)
            story_by_url[normalized] = story
        elif operation["operation"] == "update":
            story = story_by_url.get(_normalize_url(operation.get("story_url", "")))
            if story is None:
                continue
            story["latest_dev"] = operation["latest_dev"]
            story["last_updated"] = today_str
            story["importance"] = operation["importance"]
            story["status"] = operation["status"]
    return updated


def _materialize_editorial_selection(
    proposal: dict,
    candidates: list[dict],
    stories_in_flight: dict,
) -> tuple[list[dict], list[dict]]:
    candidate_by_id = {
        candidate["candidate_id"]: candidate for candidate in candidates
    }
    story_by_url = {
        _normalize_url(story.get("url", "")): story
        for story in stories_in_flight.get("stories", [])
    }
    fresh: list[dict] = []
    for selection in proposal["selected_fresh"]:
        source = copy.deepcopy(candidate_by_id[selection["candidate_id"]])
        source.update({
            "rank": len(fresh) + 1,
            "summary": selection["editorial_summary"],
            "selection_reason": selection["selection_reason"],
            "related_story_url": selection["related_story_url"],
        })
        fresh.append(source)

    ongoing: list[dict] = []
    for selection in proposal["selected_ongoing"]:
        source = copy.deepcopy(
            story_by_url[_normalize_url(selection["story_url"])]
        )
        source.update({
            "rank": len(ongoing) + 1,
            "summary": selection["summary"],
            "why_still_relevant": selection["why_still_relevant"],
        })
        ongoing.append(source)
    return fresh, ongoing


def _model_attempts(*models: str) -> list[tuple[str, str]]:
    attempts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for requested in models:
        effective = _effective_model(requested)
        if effective not in seen:
            seen.add(effective)
            attempts.append((requested, effective))
    return attempts


def phase_6_curate(
    topic: dict,
    summaries: list[dict],
    sif_candidates: list[dict],
    stories_in_flight: dict,
    run_dir: Path,
) -> tuple[list[dict], dict, list[dict]]:
    """Propose, validate, independently review, then apply editorial state changes."""
    output_path = run_dir / "06-curated.json"
    if output_path.exists():
        print(f"  [skip] Phase 6 output exists: {output_path}")
        data = json.loads(output_path.read_text())
        return data["fresh"], data.get("stories_in_flight", stories_in_flight), data["ongoing"]

    started = time.time()
    today_str = datetime.now().strftime("%Y-%m-%d")
    blocked_urls = _load_cross_topic_urls(topic, run_dir)
    candidates, dropped = _prepare_editorial_candidates(summaries, blocked_urls)
    sif_candidates = [
        story for story in sif_candidates
        if _normalize_url(story.get("url", "")) not in blocked_urls
    ]
    print(f"  [6a prep] {len(candidates)} candidates, {len(sif_candidates)} SIF, "
          f"{len(blocked_urls)} cross-topic URL(s) blocked")

    system = (
        "You are the lead editor of a daily news digest. Make one coherent editorial "
        "proposal from vetted candidates and existing stories in flight. Selection, "
        "ranking, source/topic balance, ongoing-story connections, and state proposals "
        "are interdependent. Do not write an intro and do not replace the tracker.\n\n"
        "Select 5-7 fresh stories when enough good candidates exist and 2-3 ongoing "
        "stories only from the supplied active SIF candidates. Prefer source_verdict=fresh; "
        "use older ongoing candidates only when needed. Every selection must use the exact "
        "candidate_id or story_url supplied. State changes are proposals only. An update "
        "needs selected candidate evidence; an add must reference a selected candidate.\n\n"
        "Output one JSON object in ```json fences:\n"
        '{"selected_fresh":[{"candidate_id":"...","rank":1,'
        '"editorial_summary":"2-3 factual sentences","selection_reason":"...",'
        '"related_story_url":null}],'
        '"selected_ongoing":[{"story_url":"...","rank":1,'
        '"summary":"what the story is","why_still_relevant":"what changed"}],'
        '"story_state_proposals":[{"operation":"add|update",'
        '"candidate_id":"for add","story_url":"for update",'
        '"evidence_candidate_ids":["..."],"latest_dev":"...",'
        '"importance":"high|medium|low","status":"active|cooled"}],'
        '"rejected":[{"candidate_id":"...","reason":"..."}],'
        '"gaps":"...","balance_summary":"..."}'
    )
    user = (
        f"## Date\n{today_str}\n\n"
        f"## Vetted candidates\n{json.dumps(candidates, indent=2)}\n\n"
        f"## Active SIF candidates for ongoing selection\n"
        f"{json.dumps(sif_candidates, indent=2)}\n\n"
        f"## Full tracker for connections and proposed updates\n"
        f"{json.dumps(stories_in_flight, indent=2)}\n\n"
        f"## Importance rubric\n{_importance_rubric_text(topic)}\n\n"
        f"## Dropped summaries; never select\n"
        f"{json.dumps([{'title': item.get('title'), 'url': item.get('url'), 'reason': item.get('judge_issues', [])} for item in dropped], indent=2)}"
    )

    proposal: dict | None = None
    proposal_model = ""
    proposal_warnings: list[str] = []
    proposal_errors: list[str] = []
    for requested_model, effective_model in _model_attempts(MODEL, MODEL_FALLBACK):
        try:
            raw = _call_llm_proxy(
                system, user, model=requested_model, timeout=EDITORIAL_TIMEOUT
            )
            parsed = _extract_json(raw, f"editorial proposal ({effective_model})")
            validated, warnings = _validate_editorial_proposal(
                parsed, candidates, sif_candidates, stories_in_flight, blocked_urls
            )
            if candidates and not validated["selected_fresh"]:
                raise ValueError("model selected no valid fresh stories")
            proposal = validated
            proposal_model = effective_model
            proposal_warnings = warnings
            break
        except Exception as error:
            error_summary = _summarize_model_error(error)
            proposal_errors.append(f"{effective_model}: {error_summary}")
            print(f"  [6b retry] editorial proposal failed with "
                  f"{effective_model}: {error_summary}")

    proposal_status = "model"
    if proposal is None:
        proposal_status = "raw_fallback"
        proposal = _raw_editorial_proposal(candidates, sif_candidates)
        proposal, proposal_warnings = _validate_editorial_proposal(
            proposal, candidates, sif_candidates, stories_in_flight, blocked_urls
        )
        print("  [6b degraded] both curation models failed; using source-ranked fallback")

    (run_dir / "06a-editorial-proposal.json").write_text(json.dumps({
        "status": proposal_status,
        "model": proposal_model,
        "errors": proposal_errors,
        "validation_warnings": proposal_warnings,
        "proposal": proposal,
    }, indent=2))

    final_proposal = proposal
    review_status = "skipped_raw_fallback"
    review_model = ""
    review_errors: list[str] = []
    review_warnings: list[str] = []
    review_result: dict = {"verdict": "not_run", "changes": []}
    applied_changes: list[dict] = []
    if proposal_status == "model":
        critic_system = (
            "You are the independent critic for a daily digest. Review the proposed "
            "selection, ranking, source/topic balance, ongoing links, and persistent "
            "story-state proposals. Return bounded changes only; never rewrite the whole "
            "proposal. Check for a missed stronger candidate, unsupported connections, "
            "source concentration, stale material, and state changes without evidence.\n\n"
            "Allowed operations: remove_fresh, add_fresh, replace_fresh, move_fresh, "
            "remove_ongoing, add_ongoing, replace_ongoing, remove_state, add_state, "
            "replace_state. add/replace operations put the proposed object in item. "
            "remove/replace/move fresh identifies candidate_id; ongoing identifies "
            "story_url; state operations use a zero-based index. move_fresh also supplies "
            "a one-based position. Output JSON: "
            '{"verdict":"approve|approve_with_changes|reject","changes":[],'
            '"notes":"..."}'
        )
        critic_user = (
            f"## Candidates\n{json.dumps(candidates, indent=2)}\n\n"
            f"## SIF candidates\n{json.dumps(sif_candidates, indent=2)}\n\n"
            f"## Current tracker\n{json.dumps(stories_in_flight, indent=2)}\n\n"
            f"## Proposal\n{json.dumps(proposal, indent=2)}\n\n"
            f"## Deterministic warnings\n{json.dumps(proposal_warnings, indent=2)}"
        )
        critic_models = (MODEL_REVIEWER, MODEL_FALLBACK)
        critic_rejected = False
        for requested_model, effective_model in _model_attempts(*critic_models):
            try:
                raw = _call_llm_proxy(
                    critic_system, critic_user, model=requested_model,
                    timeout=EDITORIAL_TIMEOUT,
                )
                parsed_review = _extract_json(raw, f"editorial critic ({effective_model})")
                if not isinstance(parsed_review, dict):
                    raise ValueError("critic output must be a JSON object")
                review_result = parsed_review
                verdict = parsed_review.get("verdict")
                if verdict not in ("approve", "approve_with_changes", "reject"):
                    raise ValueError(f"unknown critic verdict {verdict!r}")
                if verdict == "reject":
                    critic_rejected = True
                    raise ValueError("critic rejected the editorial proposal")
                patched, applied, patch_warnings = _apply_editorial_patches(
                    proposal, parsed_review
                )
                validated, validation_warnings = _validate_editorial_proposal(
                    patched, candidates, sif_candidates, stories_in_flight, blocked_urls
                )
                if candidates and not validated["selected_fresh"]:
                    raise ValueError("critic changes removed every valid fresh story")
                final_proposal = validated
                review_result = parsed_review
                applied_changes = applied
                review_warnings = patch_warnings + validation_warnings
                review_model = effective_model
                review_status = "reviewed"
                break
            except Exception as error:
                error_summary = _summarize_model_error(error)
                review_errors.append(f"{effective_model}: {error_summary}")
                print(f"  [6d retry] editorial critic failed with "
                      f"{effective_model}: {error_summary}")
        if review_status != "reviewed":
            if critic_rejected:
                final_proposal = _raw_editorial_proposal(candidates, sif_candidates)
                final_proposal, fallback_warnings = _validate_editorial_proposal(
                    final_proposal, candidates, sif_candidates,
                    stories_in_flight, blocked_urls,
                )
                review_warnings.extend(fallback_warnings)
                review_status = "rejected_fallback"
                print("  [6d degraded] critic rejected proposal; using source-ranked fallback")
            else:
                review_status = "unavailable"
                print("  [6d degraded] critic unavailable; using validated editorial proposal")

    (run_dir / "06b-editorial-review.json").write_text(json.dumps({
        "status": review_status,
        "model": review_model,
        "errors": review_errors,
        "review": review_result,
        "applied_changes": applied_changes,
        "validation_warnings": review_warnings,
    }, indent=2))

    updated_sif = _apply_story_state_proposals(
        stories_in_flight, final_proposal, candidates, today_str
    )
    fresh, ongoing = _materialize_editorial_selection(
        final_proposal, candidates, updated_sif
    )
    output = {
        "fresh": fresh,
        "ongoing": ongoing,
        "stories_in_flight": updated_sif,
        "gaps": final_proposal["gaps"],
        "balance_summary": final_proposal["balance_summary"],
        "editorial": {
            "proposal_status": proposal_status,
            "proposal_model": proposal_model,
            "review_status": review_status,
            "review_model": review_model,
            "degraded": (
                proposal_status != "model"
                or proposal_model != _effective_model(MODEL)
                or review_status != "reviewed"
            ),
        },
    }
    (run_dir / "06c-editorial-final.json").write_text(json.dumps({
        "proposal": final_proposal,
        "output": output,
    }, indent=2))
    output_path.write_text(json.dumps(output, indent=2))
    elapsed = time.time() - started
    print(f"  [done] curate — {len(fresh)} fresh, {len(ongoing)} ongoing, "
          f"review={review_status} ({elapsed:.0f}s)")
    return fresh, updated_sif, ongoing


def _validate_intro(intro: str, stories: list[dict]) -> tuple[bool, str]:
    text = _clean_editorial_text(intro, limit=700)
    if len(text) < 40:
        return False, "intro is too short"
    if re.search(r"https?://|www\.", text, re.IGNORECASE):
        return False, "intro contains a URL"
    if "<" in text or ">" in text:
        return False, "intro contains HTML"
    source_text = " ".join(
        f"{story.get('title', '')} {story.get('summary', '')} "
        f"{story.get('why_still_relevant', '')}"
        for story in stories
    )
    source_numbers = set(re.findall(r"\b\d[\d,.]*%?\b", source_text))
    intro_numbers = set(re.findall(r"\b\d[\d,.]*%?\b", text))
    unsupported = sorted(intro_numbers - source_numbers)
    if unsupported:
        return False, f"intro introduced unsupported numbers: {', '.join(unsupported)}"
    entity_pattern = r"\b[A-Z][A-Za-z0-9’'-]{1,}\b"
    source_entities = {
        re.sub(r"[’']s$", "", entity.casefold())
        for entity in re.findall(entity_pattern, source_text)
    }
    intro_entities = {
        re.sub(r"[’']s$", "", entity.casefold())
        for entity in re.findall(entity_pattern, text)
        if (
            re.sub(r"[’']s$", "", entity).isupper()
            or any(
                char.isupper()
                for char in re.sub(r"[’']s$", "", entity)[1:]
            )
        )
    }
    unsupported_entities = sorted(intro_entities - source_entities)
    if unsupported_entities:
        return False, (
            "intro introduced unsupported names: "
            + ", ".join(unsupported_entities)
        )
    return True, ""


def _fallback_intro(fresh: list[dict], ongoing: list[dict]) -> str:
    stories = fresh or ongoing
    if not stories:
        return "No stories were selected for today’s digest."
    lead = stories[0].get("title", "the lead story")
    if len(stories) == 1:
        return (
            f"Today’s digest focuses on {lead}. Read on for the verified details "
            "and why the development matters."
        )
    secondary = " and ".join(
        story.get("title", "") for story in stories[1:3] if story.get("title")
    )
    return (
        f"Today’s digest leads with {lead}. Also in focus: {secondary}."
    )


def _generate_final_intro(
    topic: dict,
    fresh: list[dict],
    ongoing: list[dict],
    run_dir: Path,
) -> str:
    """Generate copy only after selection review; deterministic HTML comes later."""
    artifact_path = run_dir / "07-intro.json"
    if artifact_path.exists():
        data = json.loads(artifact_path.read_text())
        return data.get("intro", _fallback_intro(fresh, ongoing))

    stories = fresh + ongoing
    system = (
        "Write a concise 2-3 sentence editorial introduction for a daily digest. "
        "Use only facts, entities, and numbers in the approved story data. Do not add "
        "URLs, HTML, new reporting, or claims about rejected stories. Output one JSON "
        'object: {"intro":"..."}'
    )
    user = (
        f"Digest: {topic['title']}\n\n"
        f"Approved stories:\n{json.dumps(stories, indent=2)}"
    )
    errors: list[str] = []
    intro = ""
    model_used = ""
    status = "model"
    for requested_model, effective_model in _model_attempts(MODEL, MODEL_FALLBACK):
        try:
            raw = _call_llm_proxy(
                system, user, model=requested_model, timeout=INTRO_TIMEOUT
            )
            result = _extract_json(raw, f"final intro ({effective_model})")
            if not isinstance(result, dict):
                raise ValueError("intro output must be a JSON object")
            candidate = _clean_editorial_text(result.get("intro"), limit=700)
            valid, reason = _validate_intro(candidate, stories)
            if not valid:
                raise ValueError(reason)
            intro = candidate
            model_used = effective_model
            break
        except Exception as error:
            error_summary = _summarize_model_error(error)
            errors.append(f"{effective_model}: {error_summary}")
            print(f"  [7 retry] intro failed with {effective_model}: {error_summary}")
    if not intro:
        intro = _fallback_intro(fresh, ongoing)
        status = "deterministic_fallback"
    artifact_path.write_text(json.dumps({
        "intro": intro,
        "status": status,
        "model": model_used,
        "errors": errors,
    }, indent=2))
    return intro


def _render_story_block(story: dict, *, ongoing: bool = False) -> str:
    url = html.escape(str(story.get("url", "")), quote=True)
    title = html.escape(str(story.get("title", "")))
    category = html.escape(str(story.get("category", "")))
    summary = html.escape(str(story.get("summary", "")))
    why = ""
    if ongoing:
        why_text = html.escape(str(story.get("why_still_relevant", "")))
        why = (
            '\n    <p style="margin:2px 0 0; color:#7c6bbf; font-size:13px; '
            f'font-style:italic;">↳ {why_text}</p>'
        )
    return (
        "<tr>\n"
        '  <td style="padding:8px 32px;">\n'
        '    <p style="margin:0 0 2px;">\n'
        f'      <a href="{url}" style="color:#1a1a2e; font-size:15px; '
        f'font-weight:600; text-decoration:none;">{title}</a>\n'
        f'      <span style="color:#999; font-size:12px; font-weight:400;">'
        f" · {category}</span>\n"
        "    </p>\n"
        f'    <p style="margin:0; color:#555; font-size:14px; '
        f'line-height:1.5;">{summary}</p>{why}\n'
        "  </td>\n"
        "</tr>"
    )


def _empty_section_block(message: str) -> str:
    return (
        '<tr><td style="padding:8px 32px; color:#777; font-size:14px;">'
        f"{html.escape(message)}</td></tr>"
    )


def _render_digest_html(
    topic: dict,
    fresh: list[dict],
    ongoing: list[dict],
    intro: str,
    *,
    notice: str = "",
) -> str:
    template = TEMPLATE_PATH.read_text()
    template = re.sub(
        r"\n<!--\nSTORY BLOCK TEMPLATE[\s\S]*?-->\s*$", "\n", template
    )
    intro_text = f"{notice} {intro}".strip()
    fresh_html = "\n".join(
        _render_story_block(story) for story in fresh
    ) or _empty_section_block("No fresh stories selected today.")
    ongoing_html = "\n".join(
        _render_story_block(story, ongoing=True) for story in ongoing
    ) or _empty_section_block("No ongoing stories selected today.")
    replacements = {
        "{{DIGEST_TITLE}}": html.escape(str(topic["title"])),
        "{{DATE}}": html.escape(datetime.now().strftime("%B %d, %Y")),
        "{{INTRO}}": html.escape(intro_text),
        "{{FRESH_STORIES}}": fresh_html,
        "{{ONGOING_STORIES}}": ongoing_html,
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def phase_7_write(
    topic: dict,
    fresh: list[dict],
    ongoing: list[dict],
    run_dir: Path,
    *,
    notice: str = "",
) -> str:
    """Generate the approved intro, then render the email deterministically."""
    output_path = run_dir / "digest.html"
    if output_path.exists():
        print(f"  [skip] Phase 7 output exists: {output_path}")
        return output_path.read_text()

    print(f"  [run ] write_html — {len(fresh)} fresh, {len(ongoing)} ongoing")
    started = time.time()
    intro = _generate_final_intro(topic, fresh, ongoing, run_dir)
    rendered = _render_digest_html(
        topic, fresh, ongoing, intro, notice=notice
    )
    output_path.write_text(rendered)
    elapsed = time.time() - started
    print(f"  [done] write_html — deterministic render, {len(rendered)} chars "
          f"({elapsed:.0f}s)")
    return rendered

def phase_8_send_archive(topic: dict, html: str, stories_in_flight: dict,
                         run_dir: Path, digest_dir: Path,
                         fresh: list[dict] | None = None,
                         ongoing: list[dict] | None = None,
                         send_on_empty: bool = False) -> None:
    """Phase 8: Send email, archive HTML, write stories-in-flight.

    No LLM call — pure Python. In test mode, email is sent with a [TEST]
    subject prefix and archived to the test run_dir.

    Skip-send on empty: when there are no fresh and no ongoing stories the
    email is NOT sent (a "<p>No stories found today.</p>" digest is a bug,
    not content). The HTML is still archived so the run leaves a record.
    send_on_empty=True forces the send regardless — used for the upstream
    outage notification, which is a deliberate alert rather than an empty
    digest.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Copy curated_copy.json FIRST — not gated by idempotent resume below,
    # so a partial re-run doesn't silently drop the curated snapshot.
    curated_src = run_dir / "06-curated.json"
    if curated_src.exists():
        shutil.copy(curated_src, run_dir / "curated_copy.json")
    else:
        print(f"  [WARN] 06-curated.json missing — curated_copy.json not written")

    # Archive path: prod → digest_dir with date, test → run_dir
    archive_path = digest_dir / f"{today_str}.html" if not TEST_MODE else run_dir / "digest.html"

    # Always write temp HTML first (needed for email body and archive)
    temp_html = digest_dir / ".daily_digest.html"
    temp_html.write_text(html)

    # Only send email if archive doesn't already exist (idempotent resume
    # guard) AND the digest has content (skip-send on empty: no fresh and no
    # ongoing stories — unless send_on_empty, e.g. outage notification).
    archive_already_exists = archive_path.exists()
    empty_digest = not (fresh or ongoing) and not send_on_empty
    if (archive_already_exists and not TEST_MODE) or empty_digest:
        if empty_digest:
            print("  [skip] send_email — empty digest (no fresh/ongoing stories); archived only")
        else:
            print(f"  [skip] send_email — archive already exists: {archive_path}")
    else:
        recipients = topic["recipients"].copy()

        if topic["category"] == "agentic-platform":
            smtp_config = Path.home() / "scripts" / ".smtp_config"
            if smtp_config.exists():
                for line in smtp_config.read_text().splitlines():
                    if line.startswith("AGENTIC_CC="):
                        cc = line.split("=", 1)[1].strip()
                        if cc:
                            recipients.append(cc)
                        break

        prefix = "[TEST] " if TEST_MODE else ""
        subject = f"{prefix}{topic['title']} — {today_str}"
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

    # Always archive the latest HTML (overwrite stale/empty archive from prior run)
    shutil.copy(temp_html, archive_path)
    print(f"  [done] archived HTML → {archive_path}")

    sif_path = digest_dir / "stories-in-flight.json"
    sif_path.write_text(json.dumps(stories_in_flight, indent=2))
    print(f"  [done] stories-in-flight updated")



def phase_9_summary(topic: dict, fresh: list[dict], ongoing: list[dict],
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

    if not fresh and not ongoing:
        # Empty digest: never send empty data to the LLM. The hard "every story
        # MUST include its URL" constraint makes it fabricate placeholder
        # stories with example.com URLs. Write an honest summary directly.
        md_output = (
            f"# {topic['title']} — {today_str}\n"
            f"**Sent to:** {', '.join(topic['recipients'])}\n\n"
            "## Fresh\n"
            "- No stories published in the last 24 hours.\n\n"
            "## Ongoing\n"
            "- No ongoing stories reported.\n\n"
            "## Coverage Gaps\n"
            f"- No {topic['title']} stories were published or aggregated "
            "in the last 24 hours.\n"
        )
        output_path.write_text(md_output)
        shutil.copy(output_path, digest_md_path)
        elapsed = time.time() - t0
        print(f"  [done] summary_md — empty digest, direct summary ({elapsed:.0f}s)")
        return

    fresh_json = json.dumps(fresh, indent=2)
    ongoing_json = json.dumps(ongoing, indent=2)

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
        "## Ongoing\n"
        "- [Story title](URL) — one-line summary (why still relevant)\n\n"
        "## Coverage Gaps\n"
        "- Any notable stories or angles that were missed today\n\n"
        "IMPORTANT: Every story MUST include its URL as a markdown link `[title](URL)`. "
        "This is used by the dedup system in future runs. Never omit the URL.\n\n"
        f"## Fresh Stories Data\n\n{fresh_json}\n\n"
        f"## Ongoing Stories Data\n\n{ongoing_json}"
    )

    try:
        raw = _call_llm_proxy(system, user, model=MODEL)
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
        lines.append("## Ongoing")
        for s in ongoing[:5]:
            lines.append(f"- [{s.get('title', '?')}]({s.get('url', '#')}) — {s.get('summary', '')[:100]}")
        output_path.write_text("\n".join(lines) + "\n")

    if output_path.exists():
        shutil.copy(output_path, digest_md_path)


# ═══════════════════════════════════════════════════════════════════════════
# Stories-in-flight management
# ═══════════════════════════════════════════════════════════════════════════

def _ensure_importance(s: dict) -> dict:
    """Add default 'importance' field to a story-in-flight entry if missing."""
    if "importance" not in s:
        s["importance"] = "medium"
    return s


def _prune_and_cool_stories(stories: list[dict], today: date | None = None) -> tuple[list[dict], int, int]:
    """Apply auto-cool and auto-prune to a list of story dicts.

    Returns (kept_stories, auto_cooled_count, auto_pruned_count).
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    kept: list[dict] = []
    auto_cooled = 0
    auto_pruned = 0

    for s in stories:
        _ensure_importance(s)

        if "first_seen" not in s:
            s["first_seen"] = s.get("last_updated", today.isoformat())

        last_str = s.get("last_updated", "")
        first_str = s.get("first_seen", last_str)
        try:
            last_date = datetime.strptime(last_str, "%Y-%m-%d").date()
            first_date = datetime.strptime(first_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            s["status"] = "cooled"
            auto_cooled += 1
            kept.append(s)
            continue

        total_age = (today - first_date).days
        inactive_age = (today - last_date).days
        status = s.get("status", "active")

        # Rule 1: Auto-prune stories older than PRUNE_AFTER_DAYS since first_seen
        if total_age >= PRUNE_AFTER_DAYS:
            auto_pruned += 1
            continue

        # Rule 2: Auto-cool stale active stories — inactivity only (no updates in
        # COOL_AFTER_DAYS), matching the documented rule (COOL_AFTER_DAYS comment
        # and load_and_prune_stories_in_flight docstring). Cooling on total_age
        # wrongly culls stories that are still being actively updated; the 7-day
        # auto-prune above already bounds how long any story stays in the tracker.
        if status == "active" and inactive_age >= COOL_AFTER_DAYS:
            s["status"] = "cooled"
            auto_cooled += 1
            kept.append(s)
            continue

        kept.append(s)

    return kept, auto_cooled, auto_pruned


def load_and_prune_stories_in_flight(digest_dir: Path) -> dict:
    """Load the cross-day story tracker and apply deterministic pruning.

    Two rules (Python-side, not LLM-dependent):
    1. AUTO-COOL: Any story with status "active" and last_updated older than
       COOL_AFTER_DAYS → set status to "cooled". Removes from Ongoing pool.
    2. AUTO-PRUNE: Any story whose first_seen is older than PRUNE_AFTER_DAYS
       → remove from the tracker entirely (regardless of status).

    Validated Phase 6 state proposals can still revive stories by updating
    last_updated and setting status back to "active" when evidence supports it.
    """
    path = digest_dir / "stories-in-flight.json"
    if not path.exists():
        return {"stories": []}

    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError):
        return {"stories": []}

    kept, auto_cooled, auto_pruned = _prune_and_cool_stories(data.get("stories", []))

    if auto_cooled > 0:
        print(f"  Auto-cooled {auto_cooled} stale stories (> {COOL_AFTER_DAYS}d no updates)")
    if auto_pruned > 0:
        print(f"  Auto-pruned {auto_pruned} expired stories (first_seen > {PRUNE_AFTER_DAYS}d old)")

    data["stories"] = kept
    return data


def cleanup_old_artifacts(digest_dir: Path, max_age_days: int = 14):
    """Remove run directories older than max_age_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    for child in digest_dir.iterdir():
        if child.is_dir() and child.name != "stories-in-flight":
            try:
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
    """Run the full digest pipeline for a topic category.

    When TEST_MODE is set (via --test CLI flag), output goes to
    ~/digests/test/<topic>/<label>/ and email is never sent. The
    stories-in-flight from prod are copied in so ongoing tracking works.
    """
    global MODEL_OVERRIDE
    if category not in TOPICS:
        print(f"Unknown topic: {category}")
        print(f"Available: {', '.join(TOPICS)}")
        sys.exit(1)

    topic = TOPICS[category]
    today_str = datetime.now().strftime("%Y-%m-%d")

    if TEST_MODE:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        label = (TEST_LABEL or "test") + "-" + ts
        digest_dir = DIGESTS_DIR / "test" / topic["category"]
        run_dir = digest_dir / label

        # Copy stories-in-flight from prod so ongoing tracking works
        prod_sif = DIGESTS_DIR / topic["category"] / "stories-in-flight.json"
        if prod_sif.exists():
            digest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(prod_sif, digest_dir / "stories-in-flight.json")
            print(f"  [test] Copied stories-in-flight from prod ({prod_sif})")
    else:
        digest_dir = DIGESTS_DIR / topic["category"]
        run_dir = digest_dir / today_str

    run_dir.mkdir(parents=True, exist_ok=True)

    model_note = f" [model: {MODEL_OVERRIDE}]" if MODEL_OVERRIDE else ""
    print(f"\n{'='*60}")
    print(f"  {topic['title']} — {today_str}{model_note}")
    print(f"  Run dir: {run_dir}")
    if TEST_MODE:
        print(f"  *** TEST MODE — output isolated, email enabled ***")
    print(f"{'='*60}\n")

    overall_start = time.time()
    phase_times: dict[str, float] = {}

    def _phase_start(name: str) -> float:
        t = time.time()
        print(f"\n── {name} ──")
        return t

    def _phase_done(name: str, start: float) -> None:
        elapsed = time.time() - start
        phase_times[name] = elapsed
        print(f"  [{elapsed:.0f}s] {name}")

    # Phase 0: Setup
    t0 = _phase_start("Phase 0: Setup")
    stories_in_flight = load_and_prune_stories_in_flight(digest_dir)
    active_stories = [s for s in stories_in_flight.get("stories", [])
                      if s.get("status") == "active"]
    print(f"  Stories in flight: {len(active_stories)} active")
    if not TEST_MODE:
        cleanup_old_artifacts(digest_dir)
    _phase_done("Phase 0: Setup", t0)

    try:
        # ── Cross-process retry backoff ──
        retry_state_path = digest_dir / ".retry-state.json"
        retry_count = 0
        if not TEST_MODE and retry_state_path.exists():
            try:
                state = json.loads(retry_state_path.read_text())
                retry_count = state.get("retry_count", 0)
                delay = min(10 * (2 ** retry_count), 600)
                if retry_count > 0:
                    print(f"  *** Cross-process backoff: attempt #{retry_count + 1}, "
                          f"waiting {delay}s")
                    time.sleep(delay)
            except (json.JSONDecodeError, ValueError):
                pass
        # ── Retry loop: try primary model, fall back on stub output ──
        for _stub_retry in range(2):
            if _stub_retry > 0:
                # Stub retry: skip if fallback was already tried via Phase 1 inner retry
                if MODEL_OVERRIDE:
                    print(f"  *** Both primary and fallback models already exhausted "
                          f"(inner retry used {MODEL_OVERRIDE}). No more retries.")
                    break
                MODEL_OVERRIDE = MODEL_FALLBACK
                print(f"  *** STUB RETRY: No stories produced. "
                      f"Retrying with fallback: {MODEL_OVERRIDE}")
                for p in run_dir.glob("0*-*.json"):
                    p.unlink()

            # Phase 1: Research
            t1 = _phase_start("Phase 1: Research")
            check_search_health("pre-phase1")
            phase_1_path = run_dir / "01-research-raw.json"
            if phase_1_path.exists():
                print(f"  [skip] Phase 1 output exists: {phase_1_path}")
                findings = json.loads(phase_1_path.read_text())
            else:
                findings = phase_1_research(topic, run_dir)
            health = check_search_health("post-phase1")
            if health.get("recommendation") == "halt":
                print("  *** HALT: Search engine health critical. Stopping digest. ***")
                print(f"  Working engines: {health.get('engines_working')}")
                print(f"  Suspended: {health.get('engines_suspended')}")
                sys.exit(2)
            if not findings:
                # Retry with fallback model when primary model produces empty results
                fallback = MODEL_FALLBACK
                if MODEL_OVERRIDE:
                    fallback = MODEL   # already on override, flip to original
                if not TEST_MODE:
                    import time as _time
                    retry_delay = min(10 * (2 ** (retry_count + 1)), 120)  # exponential backoff
                    print(f"  *** Backoff: waiting {retry_delay}s before fallback retry")
                    _time.sleep(retry_delay)
                    print(f"  *** RETRY: No findings. Retrying Phase 1 with fallback: {fallback}")
                    MODEL_OVERRIDE = fallback
                    if phase_1_path.exists():
                        phase_1_path.unlink()
                    for p in run_dir.glob("0*-*.json"):
                        p.unlink()
                    findings = phase_1_research(topic, run_dir)
                    if findings:
                        print(f"  *** RETRY succeeded with fallback model: {fallback}")
                if not findings:
                    h = check_search_health("post-fallback-retry")
                    print(f"  WARNING: No research findings after fallback retry. "
                          f"Search health: {h.get('results', '?')} results, "
                          f"{h.get('engines_working', '?')} working, "
                          f"{len(h.get('engines_suspended', []))} suspended. "
                          f"Digest will be empty.")
            _phase_done("Phase 1: Research", t1)

            # Phase 2: Judge Research
            t2 = _phase_start("Phase 2: Judge Research")
            phase_2_path = run_dir / "02-research-judged.json"
            if phase_2_path.exists():
                print(f"  [skip] Phase 2 output exists: {phase_2_path}")
                judged_raw = json.loads(phase_2_path.read_text())
                fresh_findings = judged_raw.get("fresh", [])
                ongoing_findings = judged_raw.get("ongoing", [])
            elif findings:
                fresh_findings, ongoing_findings = phase_2_judge_research(topic, findings, run_dir, stories_in_flight)
            else:
                fresh_findings, ongoing_findings = [], []
            _phase_done("Phase 2: Judge Research", t2)

            # Phase 3: Rank URLs
            t3 = _phase_start("Phase 3: Rank URLs")
            phase_3_path = run_dir / "03-urls-ranked.json"
            if phase_3_path.exists():
                print(f"  [skip] Phase 3 output exists: {phase_3_path}")
                ranked = json.loads(phase_3_path.read_text())
                phase_4_queue = ranked.get("phase_4_queue", [])
                sif_candidates = ranked.get("sif_candidates", [])
            elif fresh_findings or ongoing_findings or any(
                    s.get("status") == "active"
                    for s in stories_in_flight.get("stories", [])):
                # Run ranking even with zero findings so active stories-in-flight
                # still flow to Phase 6 as Ongoing candidates (SIF injection).
                phase_4_queue, sif_candidates = phase_3_rank(
                    topic, fresh_findings, ongoing_findings, stories_in_flight, run_dir)
            else:
                phase_4_queue, sif_candidates = [], []
            _phase_done("Phase 3: Rank URLs", t3)

            # Phase 4: Fetch + Summarize
            t4 = _phase_start("Phase 4: Fetch & Summarize")
            check_search_health("pre-phase4")
            phase_4_path = run_dir / "04-fetch-summaries.json"
            if phase_4_path.exists():
                print(f"  [skip] Phase 4 output exists: {phase_4_path}")
                summaries = json.loads(phase_4_path.read_text())
            elif phase_4_queue:
                summaries = phase_4_fetch(topic, phase_4_queue, run_dir)
            else:
                summaries = []
            _phase_done("Phase 4: Fetch & Summarize", t4)

            # Phase 5: Judge Summaries
            t5 = _phase_start("Phase 5: Judge Summaries")
            phase_5_path = run_dir / "05-summaries-judged.json"
            if phase_5_path.exists():
                print(f"  [skip] Phase 5 output exists: {phase_5_path}")
                judged = json.loads(phase_5_path.read_text())
            elif summaries:
                judged = phase_5_judge_summaries(topic, summaries, run_dir)
            else:
                judged = []
            _phase_done("Phase 5: Judge Summaries", t5)

            # Phase 6: Curate
            t6 = _phase_start("Phase 6: Curate")
            phase_6_path = run_dir / "06-curated.json"
            if phase_6_path.exists():
                print(f"  [skip] Phase 6 output exists: {phase_6_path}")
                curated = json.loads(phase_6_path.read_text())
                fresh = curated.get("fresh", [])
                ongoing = curated.get("ongoing", [])
                if "stories_in_flight" in curated:
                    stories_in_flight = curated["stories_in_flight"]
                    # Re-apply cooling/pruning to cached data (Phase 0 pruning was lost by cache)
                    sif_stories = stories_in_flight.get("stories", [])
                    if sif_stories:
                        kept, re_cooled, re_pruned = _prune_and_cool_stories(sif_stories)
                        if re_cooled > 0 or re_pruned > 0:
                            print(f"  [phase6-cache] Re-cooled {re_cooled} stale, "
                                  f"re-pruned {re_pruned} expired stories")
                            stories_in_flight["stories"] = kept
            elif judged or sif_candidates:
                # Curation runs on SIF candidates alone (judged empty) so ongoing
                # stories from previous days are reported instead of a blank digest.
                fresh, stories_in_flight, ongoing = phase_6_curate(
                    topic, judged, sif_candidates, stories_in_flight, run_dir)
            else:
                fresh, ongoing = [], []
            _phase_done("Phase 6: Curate", t6)

            # Re-apply cooling/pruning to prevent LLM from undoing auto-cooled stories
            sif_stories = stories_in_flight.get("stories", [])
            if sif_stories:
                kept, re_cooled, re_pruned = _prune_and_cool_stories(sif_stories)
                if re_cooled > 0 or re_pruned > 0:
                    print(f"  [post-6] Re-cooled {re_cooled} stale, re-pruned {re_pruned} expired stories")
                    stories_in_flight["stories"] = kept

            # Stub detection: if no fresh stories, retry with fallback model
            if _stub_retry == 0 and not fresh and not TEST_MODE:
                if not MODEL_OVERRIDE:
                    print(f"  *** Stub detected: 0 fresh stories after Phase 6 with primary model. "
                          f"Retrying with fallback.")
                    continue
                else:
                    print(f"  *** Stub detected but already on fallback model "
                          f"({MODEL_OVERRIDE}) — no more retries.")
            break

        # Phase 7: Write HTML
        t7 = _phase_start("Phase 7: Write HTML")
        notice = ""
        if fresh or ongoing:
            if _UPSTREAM_OUTAGE:
                notice = (
                    "NOTE: Today’s research stage was degraded—the research API "
                    "returned no fresh findings. The stories below are ongoing "
                    "coverage carried over from previous days."
                )
            html = phase_7_write(
                topic, fresh, ongoing, run_dir, notice=notice
            )
        else:
            if _UPSTREAM_OUTAGE:
                html = (
                    f'<html><body style="font-family:-apple-system,system-ui,sans-serif;padding:2em;">'
                    f'<h1 style="color:#1a1a2e;">{topic["title"]}</h1>'
                    f'<p style="color:#666;">{today_str}</p>'
                    f'<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:1em 1.2em;margin:1em 0;">'
                    f'<p style="margin:0;color:#856404;font-weight:600;">Digest unavailable — upstream API outage</p>'
                    f'<p style="margin:0.5em 0 0;color:#856404;font-size:14px;">'
                    f'The research API (opencode-go-proxy) was unreachable or returned no '
                    f'results during today\'s run. '
                    f'This is a transient infrastructure issue, not a news drought. '
                    f'Stories will resume when the API is available.</p></div></body></html>'
                )
            else:
                html = (
                    f'<html><body><h1>{topic["title"]}</h1>'
                    f'<p>{today_str}</p><p>No stories found today.</p></body></html>'
                )
            (run_dir / "digest.html").write_text(html)
        _phase_done("Phase 7: Write HTML", t7)

        # Phase 8: Send + Archive
        t8 = _phase_start("Phase 8: Send & Archive")
        if dry_run:
            print("  [skip] DRY RUN — skipping email send")
            archive_path = run_dir / "digest.html"
            (run_dir / "digest.html").write_text(html)
            print(f"  [done] archived HTML → {archive_path}")
            sif_path = digest_dir / "stories-in-flight.json"
            sif_path.write_text(json.dumps(stories_in_flight, indent=2))
            print(f"  [done] stories-in-flight updated")
            curated_src = run_dir / "06-curated.json"
            if curated_src.exists():
                shutil.copy(curated_src, run_dir / "curated_copy.json")
            else:
                print(f"  [WARN] 06-curated.json missing — curated_copy.json not written")
        else:
            phase_8_send_archive(topic, html, stories_in_flight, run_dir, digest_dir,
                                 fresh=fresh, ongoing=ongoing,
                                 send_on_empty=_UPSTREAM_OUTAGE)
        _phase_done("Phase 8: Send & Archive", t8)

        # Phase 9: Summary
        t9 = _phase_start("Phase 9: Summary")
        phase_9_summary(topic, fresh, ongoing, run_dir, digest_dir)
        _phase_done("Phase 9: Summary", t9)

    except Exception as e:
        # Save retry state for cross-process exponential backoff
        if not TEST_MODE:
            try:
                state = json.loads(retry_state_path.read_text()) \
                    if retry_state_path.exists() else {}
                state["retry_count"] = state.get("retry_count", 0) + 1
                state["last_failure"] = datetime.utcnow().isoformat()
                retry_state_path.write_text(json.dumps(state))
            except Exception:
                pass
        print(f"\n  FATAL: {e}")
        traceback.print_exc()
        sys.exit(1)

    overall_elapsed = time.time() - overall_start
    print(f"\n{'='*60}")
    print(f"  Digest complete in {overall_elapsed:.0f}s ({overall_elapsed/60:.1f} min)")
    print(f"{'='*60}\n")

    # ── .runs.log duration tracking ──────────────────────────────────────
    if not TEST_MODE:
        model = MODEL_OVERRIDE if MODEL_OVERRIDE else MODEL
        runs_log = digest_dir / ".runs.log"
        now_utc = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = f"{now_utc} {category} duration={overall_elapsed:.0f}s model={model}\n"
        with open(runs_log, "a") as f:
            f.write(entry)

    # ── Test report ──────────────────────────────────────────────────────
    if TEST_MODE:
        _write_test_report(run_dir, topic, category, phase_times,
                           overall_elapsed, len(findings) if findings else 0,
                           len(summaries) if 'summaries' in dir() else 0,
                           len(fresh) if 'fresh' in dir() else 0,
                           len(ongoing) if 'ongoing' in dir() else 0)

    # ── Clean up cross-process retry state ──
    if not TEST_MODE:
        try:
            retry_state_path = digest_dir / ".retry-state.json"
            if retry_state_path.exists():
                retry_state_path.unlink()
                print(f"  [done] Cleared retry state (successful run)")
        except Exception:
            pass


def _write_test_report(run_dir: Path, topic: dict, category: str,
                       phase_times: dict[str, float], total_time: float,
                       n_findings: int, n_summaries: int,
                       n_fresh: int, n_ongoing: int) -> None:
    """Write a test report summarizing timing and quality metrics."""
    report_path = run_dir / "test-report.md"
    model = MODEL_OVERRIDE or MODEL
    provider_info = _detect_model_provider(model)

    lines = [
        f"# Test Report: {topic['title']}",
        f"",
        f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"**Model:** `{model}`",
        f"**Provider:** `{provider_info['provider']}` ({provider_info['chat_url']})",
        f"**Label:** `{TEST_LABEL or 'N/A'}`",
        f"",
        f"## Timing",
        f"",
        f"| Phase | Time (s) | Time (min) |",
        f"|-------|----------|------------|",
    ]
    for name, secs in phase_times.items():
        lines.append(f"| {name} | {secs:.0f} | {secs/60:.1f} |")
    lines.append(f"| **Total** | **{total_time:.0f}** | **{total_time/60:.1f}** |")

    lines += [
        f"",
        f"## Throughput",
        f"",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Phase 1 findings | {n_findings} |",
        f"| Phase 4 summaries | {n_summaries} |",
        f"| Final fresh stories | {n_fresh} |",
        f"| Final ongoing stories | {n_ongoing} |",
        f"",
        f"## Artifacts",
        f"",
    ]
    for f in sorted(run_dir.iterdir()):
        if f.is_file():
            size_kb = f.stat().st_size / 1024
            lines.append(f"- `{f.name}` ({size_kb:.1f} KB)")

    report_path.write_text("\n".join(lines) + "\n")
    print(f"  [test] Report written → {report_path}")

# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deterministic multi-phase digest runner")
    parser.add_argument("topic", choices=list(TOPICS) + ["all"],
                        help="Topic to run (or 'all' for every topic)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run pipeline but skip email send (Phase 8)")
    parser.add_argument("--test", action="store_true",
                        help="Test mode: isolate output in ~/digests/test/, copy prod SIF, write report")
    parser.add_argument("--model", type=str, default=None,
                        help="Override the LLM model (e.g. openai-codex/gpt-5.6-luna:high)")
    parser.add_argument("--test-label", type=str, default=None,
                        help="Label for test run directory (default: model name or 'test')")
    args = parser.parse_args()

    # Set module-level globals for test mode
    if args.test:
        TEST_MODE = True
    if args.model:
        MODEL_OVERRIDE = args.model
    if args.test_label:
        TEST_LABEL = args.test_label
    elif args.model and args.test:
        TEST_LABEL = args.model

    if args.topic == "all":
        for cat in TOPICS:
            if cat == "agentic-platform":
                print(f"Skipping {cat} (has CC'd recipient — run manually)")
                continue
            run_digest(cat, dry_run=args.dry_run)
    else:
        run_digest(args.topic, dry_run=args.dry_run)
