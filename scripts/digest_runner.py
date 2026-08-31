#!/usr/bin/env python3
"""
Deterministic nine-phase Daily News curation runner.

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
    2. Judge Research  — batched LLM: date, relevance, source, significance
   2b. Observe Attention — GDELT coverage time series; deterministic scores
    3. Rank URLs       — Python: cross-topic dedup, product priority, and caps
    4. Fetch + Summarize — cached omp -p read (concurrency 2, ≤17 total)
    5. Judge Summaries — batched LLM: accuracy/fidelity check
    6. Curate          — proposal → Python validation → independent critic → state apply
    7. Write HTML      — newspaper standfirst + deterministic escaped archive rendering
    8. Archive         — local HTML + stable public publication artifact
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
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urljoin

try:
    import yaml
except ImportError:
    yaml = None
import requests
from news_attention import (
    EDITORIAL_POINTS,
    SCHEMA_VERSION as ATTENTION_SCHEMA_VERSION,
    canonicalize_publisher_url,
    normalize_editorial_significance,
    priority_sort_key,
    score_attention,
)

# ── Paths ──────────────────────────────────────────────────────────────────
DIGESTS_DIR = Path.home() / "digests"
TEMPLATE_PATH = DIGESTS_DIR / "template.html"
DIGEST_OMP_SANDBOX = Path.home() / "scripts" / "digest-omp-sandbox.ts"
DIGEST_OMP_CONFIG = Path.home() / ".omp/agent/daily-news-headless.yml"
ARTICLE_CACHE_DIR = DIGESTS_DIR / ".article-cache"
ATTENTION_CACHE_DIR = DIGESTS_DIR / ".attention-cache"
ATTENTION_ARCHIVE_DIR = DIGESTS_DIR / "news" / "attention"

RANKING_SCHEMA_VERSION = 3
STANDFIRST_PROMPT_VERSION = 2

# ── LLM Proxy ──────────────────────────────────────────────────────────────
LLM_PROXY_URL = "http://localhost:8081/v1/chat/completions"
MODEL = "deepseek-v4-flash"                     # API primary
MODEL_FALLBACK = "mimo-v2.5"                    # API fallback via opencode-go
MODEL_REVIEWER = "deepseek-v4-flash"            # separate critic pass
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
FETCH_PROMPT_VERSION = 3

# ── Search Health Monitoring ──────────────────────────────────────────────
SEARXNG_URL = "http://localhost:8080"
HEALTH_LOG_PATH = DIGESTS_DIR / ".search-health.log"
MAX_ENGINE_ERRORS_BEFORE_WARN = 100  # per-engine errors observed in 1h
MIN_WORKING_ENGINES = 2               # minimum engines returning results


def _configure_test_mode(test_root: Path | None = None) -> None:
    """Route every mutable shared cache and monitor artifact under the test root."""
    global TEST_MODE, ARTICLE_CACHE_DIR, ATTENTION_CACHE_DIR
    global ATTENTION_ARCHIVE_DIR, HEALTH_LOG_PATH

    TEST_MODE = True
    root = test_root or DIGESTS_DIR / "test"
    ARTICLE_CACHE_DIR = root / ".article-cache"
    ATTENTION_CACHE_DIR = root / ".attention-cache"
    ATTENTION_ARCHIVE_DIR = root / "news" / "attention"
    HEALTH_LOG_PATH = root / ".search-health.log"


def check_search_health(label: str = "") -> dict[str, Any]:
    """Check the fresh-news SearXNG fallback without gating the primary provider.

    Returns:
        {
            "ok": True/False,
            "results": count,
            "engines_working": [names],
            "engines_suspended": [(name, reason), ...],
            "recent_errors": count (1h),
            "recommendation": "ok" | "warn",
        }
    """
    status: dict[str, Any] = {
        "ok": False, "results": 0, "engines_working": [],
        "engines_suspended": [], "recent_errors": 0,
        "recommendation": "ok", "label": label,
        "query": "artificial intelligence", "time_range": "day",
        "categories": "news",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        # Test the same time-filtered news path digest discovery depends on.
        # A generic unfiltered query can look healthy while every fresh-news
        # query returns zero results.
        resp = requests.get(
            f"{SEARXNG_URL}/search",
            params={
                "q": status["query"],
                "format": "json",
                "language": "en",
                "time_range": status["time_range"],
                "categories": status["categories"],
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        status["results"] = len(results)

        engines_seen: set[str] = set()
        for result in results:
            engines_seen.update(result.get("engines", []))
            if result.get("engine"):
                engines_seen.add(result["engine"])
        status["engines_working"] = sorted(engines_seen)

        unresponsive = data.get("unresponsive_engines", [])
        status["engines_suspended"] = [
            {"engine": e[0], "reason": e[1]} for e in unresponsive
        ]

        # Check recent SearXNG errors. Docker writes container logs to stderr.
        try:
            result = subprocess.run(
                ["docker", "logs", "searxng", "--since", "1h"],
                capture_output=True, text=True, timeout=10,
            )
            log_output = result.stdout + result.stderr
            status["recent_errors"] = log_output.count("ERROR:searx.engines")
        except Exception:
            status["recent_errors"] = -1  # couldn't check

        # SearXNG is a fallback. Degradation is observable but must not block
        # successful research from the primary provider.
        working_count = len(status["engines_working"])
        suspended_count = len(status["engines_suspended"])

        if status["results"] == 0 or working_count == 0:
            status["recommendation"] = "warn"
            status["ok"] = False
        elif working_count < MIN_WORKING_ENGINES and suspended_count > 3:
            status["recommendation"] = "warn"
            status["ok"] = False
        elif suspended_count >= 3 or status.get("recent_errors", 0) > MAX_ENGINE_ERRORS_BEFORE_WARN:
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
    emoji = {"ok": "✓", "warn": "⚠"}.get(status["recommendation"], "?")
    print(f"  [health:{label}] {emoji} {status['results']} results from "
          f"{status['engines_working']} | "
          f"{len(status['engines_suspended'])} suspended | "
          f"{status.get('recent_errors', '?')} errors/1h | "
          f"rec: {status['recommendation']}")

    return status

# ── Batching ───────────────────────────────────────────────────────────────
BATCH_SIZE = 10  # findings/summaries per LLM call in phases 2 and 5

# ── Caps ───────────────────────────────────────────────────────────────────
FRESH_CAP = 12       # Pool A: max fresh findings passed to Phase 4
ONGOING_CAP = 5      # Pool B: max older articles passed to Phase 4
SIF_CAP = 3          # Pool C: max qualified developing stories passed to Phase 6
FOLLOWUP_STORY_CAP = 8  # high-significance tracker stories checked for developments

# ── Stories-in-flight constants ────────────────────────────────────────────
MIN_DEVELOPMENT_DAYS = 2  # evidence-backed developments on distinct UTC dates
DEVELOPMENT_HISTORY_CAP = 30
COOL_AFTER_DAYS = 5     # auto-cool after 5 days without evidence-backed movement
PRUNE_AFTER_DAYS = 7    # remove cooled stories after 7 days without movement
# A tracker story may be surfaced in the digest at most RESURFACE_CAP_DAYS
# consecutive days without an evidence-backed development; the next day it is
# cooled (and drops out of Developing and Ongoing). Matches the auto-cool
# window: five days of digest repetition without real movement is stale by the
# same measure the tracker uses (digest-quality audit 2026-08-22).
RESURFACE_CAP_DAYS = COOL_AFTER_DAYS - 1

# Block exact URLs already covered by this section during the rolling ongoing window.
# This is independent from the referenced-link same-event dedup below.
CROSS_DAY_DEDUP_DAYS = 5

# ── Cross-topic same-event dedup (referenced-source links) ────────────────
# A topic's selected story may be press coverage that links to the canonical
# source of an event (e.g. a TechCrunch writeup linking to the OpenAI
# announcement page). Those referenced URLs are recorded per run and block the
# same event from other topics (digest-quality audit 2026-08-26: the OpenAI
# Jalapeño announcement ran in both ai-tech and ai-hardware under different
# URLs, significance, and priority because dedup keyed only on normalized URL).
REFERENCED_URLS_SCHEMA_VERSION = 1
REFERENCED_URL_TIMEOUT = 25          # per-page bound for link collection
_HTML_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (homelab Daily News; link collector)",
}

# Conservative filters for the referenced-source collector. Recording every
# outbound link on a selected story's page would block genuinely distinct
# stories in later topics, so same-host navigation/related links, social and
# utility hosts, and obvious non-article paths never enter the dedup record.
_REFERENCED_URL_SKIP_HOSTS = {
    "twitter.com", "x.com", "facebook.com", "instagram.com", "linkedin.com",
    "reddit.com", "threads.net", "youtube.com", "youtu.be", "tiktok.com",
    "mstdn.social", "bsky.app",
}
_REFERENCED_URL_SKIP_SEGMENTS = {
    "about", "contact", "privacy", "terms", "terms-of-service", "terms-of-use",
    "login", "log-in", "signup", "sign-up", "subscribe", "newsletter",
    "feed", "rss", "sitemap", "search", "press", "advertise", "careers",
    "jobs", "team", "legal", "cookies", "cookie-policy", "help", "faq",
    "shop", "store", "account", "settings",
}


# ═══════════════════════════════════════════════════════════════════════════
# Editorial-significance rubric — separate from observed public attention
# ═══════════════════════════════════════════════════════════════════════════

EDITORIAL_SIGNIFICANCE_RUBRIC_SHARED = (
    "EDITORIAL SIGNIFICANCE RUBRIC (shared — applies to every topic):\n"
    "- high — major consequence, broad impact, or a significant change to the "
    "landscape; plausible front-page material on consequence alone.\n"
    "- medium — notable and meaningful to people who follow the space, but not "
    "a lead story on consequence alone.\n"
    "- low — incremental, niche, minor, or speculative.\n"
    "Judge consequence only. Never infer popularity, virality, coverage volume, "
    "or audience interest; those are measured separately from observable signals.\n"
)

DEVELOPING_STORY_RULES = (
    "DEVELOPING AND ONGOING CONTRACT:\n"
    "- Only stories with high editorial significance qualify.\n"
    "- A story must have material factual developments on at least two distinct "
    "UTC dates. Age, continued relevance, or an unresolved possibility is not a "
    "second development.\n"
    "- Material development means the underlying event changed: a new official "
    "action, decision, filing, vote, confirmed outcome, escalation, measurable "
    "impact, or comparably substantive fact.\n"
    "- Never qualify a single announcement, launch, release, patch, result, "
    "one-off article, opinion, analysis, recap, or new commentary that merely "
    "reframes the same facts. A different article about the same broad theme is "
    "not a development.\n"
)

EDITORIAL_SIGNIFICANCE_RUBRIC_SPECIFIC: dict[str, str] = {
    "ai-tech": (
        "PER-TOPIC EDITORIAL SIGNIFICANCE:\n"
        "- high: Major model release (GPT/Claude-tier), $100M+ funding, landmark regulation, "
        "significant breach.\n"
        "- medium: New tool/feature from known player, $10M+ round, research paper with "
        "practical impact, notable acquisition.\n"
        "- low: Minor version bumps, small rounds, speculative reports, "
        "\"X announced they will announce\".\n"
    ),
    "agentic-platform": (
        "PER-TOPIC EDITORIAL SIGNIFICANCE:\n"
        "- high: Breaking change to a major platform (Claude Code, Codex, Copilot), "
        "new agent architecture that meaningfully changes capabilities, critical vulnerability.\n"
        "- medium: New feature in a known platform, MCP/server tool releases, "
        "interesting benchmark result, SDK release.\n"
        "- low: Minor patch notes, small community projects, pre-announcements without substance.\n"
    ),
    "gaming": (
        "PER-TOPIC EDITORIAL SIGNIFICANCE:\n"
        "- high: AAA release or announcement, major studio news (closure, acquisition), "
        "platform-shifting event, esports championship result.\n"
        "- medium: Notable indie release, significant patch/expansion, industry trend piece, "
        "hardware news.\n"
        "- low: Minor updates, DLC announcements, rumors, small esports events.\n"
    ),
    "world": (
        "PER-TOPIC EDITORIAL SIGNIFICANCE:\n"
        "- high: Armed conflict escalation, major election result, natural disaster with "
        "casualties, significant policy change, international crisis.\n"
        "- medium: Diplomatic development, economic data release, legislative progress, "
        "notable protest or speech.\n"
        "- low: Process stories, incremental political maneuvering, local-interest pieces.\n"
    ),
    "ai-hardware": (
        "PER-TOPIC EDITORIAL SIGNIFICANCE:\n"
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
        "category": "ai-tech",
        "web_slug": "ai-tech",
        "web_title": "AI & Tech",
        "editorial_significance_rubric_specific": EDITORIAL_SIGNIFICANCE_RUBRIC_SPECIFIC["ai-tech"],
        "research_angles": [
            {
                "id": "models-releases",
                "prompt": (
                    "Search for AI model releases, major LLM announcements, and significant "
                    "model updates from the last 24 hours. Check sources like TechCrunch AI section "
                    "(https://techcrunch.com/category/artificial-intelligence/), The Verge AI "
                    "(https://www.theverge.com/ai-artificial-intelligence), Ars Technica AI "
                    "(https://arstechnica.com/ai/), and Hacker News (https://news.ycombinator.com/).\n\n"
                    "For each story found, record from the web_search results:\n"
                    "- Title\n"
                    "- URL (the exact URL returned by web_search — do not guess or construct)\n"
                    "- Source domain (e.g. techcrunch.com)\n"
                    "- Publication date (from the search evidence, ISO format if available)\n"
                    "- 1-2 sentence factual summary (no opinion, just what happened)\n"
                    "- Category: Model Releases, AI Infrastructure, or Research\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "If a search result lacks enough evidence, skip it. Prioritize stories from today. "
                    "Only include stories web_search actually returned. "
                    "Avoid low-quality aggregators (e.g. buildfastwithai.com) that repackage other outlets' content."
                ),
            },
            {
                "id": "platforms-tools",
                "prompt": (
                    "Search for agentic AI platform news, developer tools, open source AI projects, "
                    "and coding agent developments from the last 24 hours. Check TechCrunch, "
                    "The Verge, Ars Technica, Hacker News, and dev.to.\n\n"
                    "For each story found, record from the web_search results:\n"
                    "- Title\n"
                    "- URL (the exact URL returned by web_search — do not guess or construct)\n"
                    "- Source domain\n"
                    "- Publication date (from the search evidence, ISO format if available)\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Agentic/Agent Platforms, Open Source, or Tools & Developer\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "Prioritize stories from today. Only include stories web_search actually "
                    "returned. If a search result lacks enough evidence, skip it. "
                    "Avoid low-quality aggregators (e.g. buildfastwithai.com) that repackage other outlets' content."
                ),
            },
            {
                "id": "industry-community",
                "prompt": (
                    "Search for AI industry news, funding announcements, policy/regulation, major "
                    "company moves, and notable community discussions from the last 24 hours. "
                    "Check TechCrunch, The Verge, Ars Technica, Hacker News, and Reddit r/MachineLearning.\n\n"
                    "For each story found, record from the web_search results:\n"
                    "- Title\n"
                    "- URL (the exact URL returned by web_search — do not guess or construct)\n"
                    "- Source domain\n"
                    "- Publication date (from the search evidence, ISO format if available)\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Industry News, Policy, Funding, or Community\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "Prioritize stories from today. Only include stories web_search actually "
                    "returned. If a search result lacks enough evidence, skip it. "
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
            "5. EDITORIAL SIGNIFICANCE REVIEW: Review the consequence-only label from "
            "research. Adjust it if impact differs; never infer popularity or attention.\n\n"
            "CRITICAL: The date has ALREADY been checked by a pre-processor. You do NOT need "
            "to re-check dates. All findings you receive have been pre-filtered for freshness. "
            "Focus on source quality, relevance, duplicates, substance, and significance accuracy.\n\n"
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
        "category": "agentic-platform",
        "web_slug": "agents",
        "web_title": "Agents",
        "editorial_significance_rubric_specific": EDITORIAL_SIGNIFICANCE_RUBRIC_SPECIFIC["agentic-platform"],
        "research_angles": [
            {
                "id": "platforms-features",
                "prompt": (
                    "Search for agentic AI platform news: new features, launches, and major "
                    "updates from platforms like Claude Code, Codex, Cursor, omp, Pi, Aider, "
                    "OpenCode, Windsurf, Copilot, OpenClaw, Devin, Kiro, Jules, Replit Agent, "
                    "and other coding or general-purpose agent platforms. The examples are not "
                    "exhaustive. Use three complementary searches: one broad platform-launch "
                    "query, one major coding-agent/vendor query, and one open-source or "
                    "general-purpose agent query covering primary project blogs and Hacker News. "
                    "Do not spend every search on the named vendors. Focus on the last 24 hours.\n\n"
                    "For each story found, record from the web_search results:\n"
                    "- Title\n"
                    "- URL (exact URL returned by web_search)\n"
                    "- Source domain\n"
                    "- Publication date\n"
                    "- 1-2 sentence factual summary based only on the search evidence\n"
                    "- Category: Platform Updates, Releases, or Industry News\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "Only include stories that web_search actually returned; article verification happens later."
                ),
            },
            {
                "id": "ecosystem-tools",
                "prompt": (
                    "Search for agentic AI ecosystem news: MCP servers and tools, agent SDKs, "
                    "orchestration frameworks, workflow engines, evaluation benchmarks, "
                    "and notable community projects from the last 24 hours. "
                    "Check GitHub trending, Hacker News, dev.to, and AI newsletters.\n\n"
                    "For each story found, record from the web_search results:\n"
                    "- Title, exact URL returned by web_search, source domain, publication date\n"
                    "- 1-2 sentence factual summary based only on the search evidence\n"
                    "- Category: MCP/Ecosystem, SDKs & Frameworks, Benchmarks, or Community Projects\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "Only include stories that web_search actually returned; article verification happens later."
                ),
            },
            {
                "id": "techniques-research",
                "prompt": (
                    "Search for advances in agentic AI techniques: multi-agent patterns, "
                    "deterministic orchestration, agent evaluation methods, prompting strategies, "
                    "context management, and relevant research papers from the last 24 hours.\n\n"
                    "For each finding, record from the web_search results:\n"
                    "- Title, exact URL returned by web_search, source domain, publication date\n"
                    "- 1-2 sentence factual summary based only on the search evidence\n"
                    "- Category: Techniques & Patterns, Research, or Evaluation\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "Only include findings that web_search actually returned; article verification happens later."
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
            "5. EDITORIAL SIGNIFICANCE REVIEW: Review consequence only; never infer popularity.\n\n"
            "CRITICAL: The date has ALREADY been checked by a pre-processor. You do NOT need "
            "to re-check dates. Focus on source, relevance, duplicates, substance, and significance.\n\n"
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
        "category": "ai-hardware",
        "web_slug": "ai-hardware",
        "web_title": "AI Hardware",
        "editorial_significance_rubric_specific": EDITORIAL_SIGNIFICANCE_RUBRIC_SPECIFIC["ai-hardware"],
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
                    "For each story found, record from the web_search results:\n"
                    "- Title\n"
                    "- URL (the exact URL returned by web_search — do not guess or construct)\n"
                    "- Source domain (e.g. tomshardware.com)\n"
                    "- Publication date (from the search evidence, ISO format if available)\n"
                    "- 1-2 sentence factual summary (no opinion, just what happened)\n"
                    "- Category: Accelerators & Silicon or Custom/Startup Silicon\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "If a search result lacks enough evidence, skip it. Prioritize stories from today. "
                    "Only include stories web_search actually returned."
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
                    "For each story found, record from the web_search results:\n"
                    "- Title\n"
                    "- URL (the exact URL returned by web_search — do not guess or construct)\n"
                    "- Source domain\n"
                    "- Publication date (from the search evidence, ISO format if available)\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Memory & HBM, Networking & Interconnect, Datacenter & Power, "
                    "or Supply Chain & Fabs\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "If a search result lacks enough evidence, skip it. Prioritize stories from today. "
                    "Only include stories web_search actually returned."
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
                    "For each story found, record from the web_search results:\n"
                    "- Title\n"
                    "- URL (the exact URL returned by web_search — do not guess or construct)\n"
                    "- Source domain\n"
                    "- Publication date (from the search evidence, ISO format if available)\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Consumer & Edge\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "If a search result lacks enough evidence, skip it. Prioritize stories from today. "
                    "Only include stories web_search actually returned."
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
            "5. EDITORIAL SIGNIFICANCE REVIEW: Review the consequence-only label from "
            "research. Adjust it if impact differs; never infer popularity or attention.\n\n"
            "CRITICAL: The date has ALREADY been checked by a pre-processor. You do NOT need "
            "to re-check dates. All findings you receive have been pre-filtered for freshness. "
            "Focus on source quality, relevance, duplicates, substance, and significance accuracy.\n\n"
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
        "category": "gaming-digest",
        "web_slug": "gaming",
        "web_title": "Gaming",
        "editorial_significance_rubric_specific": EDITORIAL_SIGNIFICANCE_RUBRIC_SPECIFIC["gaming"],
        "research_angles": [
            {
                "id": "releases-announcements",
                "prompt": (
                    "Search for gaming news from the last 24 hours: game releases, major updates, "
                    "patches, DLC announcements, and platform news (Steam, Epic, console). "
                    "Check Kotaku, IGN, PC Gamer, Eurogamer, GameSpot, and gaming subreddits.\n\n"
                    "For each story, record from the web_search results:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Releases, Updates & Patches, DLC/Expansions, or Platform News\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "Only include stories web_search actually returned."
                ),
            },
            {
                "id": "industry-esports",
                "prompt": (
                    "Search for gaming industry news from the last 24 hours: studio news, "
                    "esports results, industry trends, hardware, and major community events. "
                    "Check gaming news sites and relevant subreddits.\n\n"
                    "For each story, record from the web_search results:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Industry, Esports, Hardware, or Community\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "Only include stories web_search actually returned."
                ),
            },
            {
                "id": "indie-highlights",
                "prompt": (
                    "Search for notable indie game news from the last 24 hours: new indie releases, "
                    "early access launches, Steam Next Fest highlights, and indie dev stories. "
                    "Check Steam new releases, indie game subreddits, and gaming news sites.\n\n"
                    "For each story, record from the web_search results:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Indie, Early Access, or Dev Stories\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "Only include stories web_search actually returned."
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
            "5. EDITORIAL SIGNIFICANCE REVIEW: Review consequence only; never infer popularity.\n\n"
            "CRITICAL: The date has ALREADY been checked by a pre-processor. You do NOT need "
            "to re-check dates. Focus on source, relevance, duplicates, substance, and significance.\n\n"
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
        "category": "world-digest",
        "web_slug": "world",
        "web_title": "World",
        "editorial_significance_rubric_specific": EDITORIAL_SIGNIFICANCE_RUBRIC_SPECIFIC["world"],
        "research_angles": [
            {
                "id": "us-news",
                "prompt": (
                    "Search for major U.S. news from the last 24 hours: politics, policy, "
                    "economy, Supreme Court, Congress, executive actions. Check AP News, "
                    "Reuters, NPR, BBC US section, and major newspaper sites.\n\n"
                    "For each story, record from the web_search results:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary (strictly factual, no editorializing)\n"
                    "- Category: Politics, Policy, Economy, Judiciary, or Executive\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "Only include stories web_search actually returned."
                ),
            },
            {
                "id": "world-affairs",
                "prompt": (
                    "Search for major international news from the last 24 hours: geopolitics, "
                    "conflicts, diplomacy, international organizations, global economy. "
                    "Check AP News, Reuters, BBC World, Al Jazeera, and major outlets.\n\n"
                    "For each story, record from the web_search results:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Geopolitics, Conflict, Diplomacy, Global Economy, or International\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "Only include stories web_search actually returned."
                ),
            },
            {
                "id": "science-culture",
                "prompt": (
                    "Search for notable science, technology, health, environment, and cultural "
                    "news from the last 24 hours. Check major outlets, science journals' news "
                    "sections, and reputable science news sites.\n\n"
                    "For each story, record from the web_search results:\n"
                    "- Title, URL, source domain, publication date\n"
                    "- 1-2 sentence factual summary\n"
                    "- Category: Science, Health, Environment, Technology, or Culture\n"
                    "- Editorial significance (consequence only, never popularity): high / medium / low\n\n"
                    "Only include stories web_search actually returned."
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
            "5. EDITORIAL SIGNIFICANCE REVIEW: Review consequence only; never infer popularity.\n\n"
            "CRITICAL: The date has ALREADY been checked by a pre-processor. You do NOT need "
            "to re-check dates. Focus on source, relevance, duplicates, substance, and significance.\n\n"
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
# Utility: editorial-significance rubric injection
# ═══════════════════════════════════════════════════════════════════════════

def _editorial_significance_rubric_text(topic: dict) -> str:
    """Build the consequence-only editorial rubric for a topic."""
    specific = topic.get("editorial_significance_rubric_specific", "")
    return (
        f"{EDITORIAL_SIGNIFICANCE_RUBRIC_SHARED}\n{specific}"
        if specific else EDITORIAL_SIGNIFICANCE_RUBRIC_SHARED
    )


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


# Transient proxy 5xx retry for transformation API calls (digest-quality audit
# 2026-08-24): a temporary 503 from the opencode-go proxy on 08-23 skipped the
# editorial critic entirely. Retry 502/503/504 with short backoff before the
# stage-level fallback engages.
PROXY_5XX_RETRIES = 2
PROXY_5XX_BACKOFF_SECONDS = 5


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
    resp = None
    for _attempt in range(PROXY_5XX_RETRIES + 1):
        resp = requests.post(provider_info["chat_url"], json=payload, timeout=timeout)
        try:
            resp.raise_for_status()
            break
        except requests.HTTPError:
            # A transient proxy 502/503/504 must not skip an entire editorial
            # stage — on 08-23 both Mimo calls 503'd and the ai-tech proposal
            # fell to raw fallback with no critic (digest-quality audit
            # 2026-08-24). Back off briefly and retry before the stage-level
            # fallback engages.
            if _attempt >= PROXY_5XX_RETRIES or resp.status_code not in (502, 503, 504):
                raise
            time.sleep(PROXY_5XX_BACKOFF_SECONDS * (_attempt + 1))
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
    """Call omp -p (headless) for steps that need web_search/read tools.

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

    # Pass the prompt by file so long research/fetch instructions avoid shell
    # quoting and stdin ambiguity.
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
            "--config", str(DIGEST_OMP_CONFIG),
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


def _is_listing_url(url: str) -> bool:
    """True when a URL points at a section/date archive listing, not an article.

    Search results sometimes surface Guardian daily archives with a real
    article's title, e.g. https://www.theguardian.com/technology/2026/aug/18/all
    — fetching that page returns the section listing ("Technology | The
    Guardian") and the article URL never exists. Those listings must never be
    selected into Fresh or Ongoing or enter stories-in-flight (digest-quality
    audit 2026-08-21: world-digest ongoing entries on 08-20 and 08-21 were
    the same two Guardian .../all pages, canonicalizing to /us/technology).
    """
    path = urlsplit((url or "").strip()).path.rstrip("/")
    return path.lower().endswith("/all")


# Publisher asset-CDN hosts that must never be used as article URLs. The
# research model invented assets.theregister.com article links — that host is
# The Register's static-image CDN, not an article host (HEAD returns 405 with
# no redirect), and the tracker echoed the mangled link in Ongoing email for
# five days before it was caught (digest-quality audit 2026-08-24).
_ASSET_CDN_URL_HOSTS = {
    "assets.theregister.com",
}


def _is_asset_cdn_url(url: str) -> bool:
    """True when a URL is hosted on a publisher's sibling asset CDN.

    Such hosts serve images/static assets, never articles, so a link to them
    is dead even though the host resolves. Candidate articles must resolve on
    the publisher's article host (e.g. www.theregister.com).
    """
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return False
    return host in _ASSET_CDN_URL_HOSTS or any(
        host.endswith(f".{cdn}") for cdn in _ASSET_CDN_URL_HOSTS
    )


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
        # Same-event dedup: later topics also block the canonical/related links
        # recorded from earlier topics' selected stories, so the same event
        # under a different URL is not curated twice (digest-quality audit
        # 2026-08-26). Only schema-version-matched records merge.
        referenced_path = (
            root / config["category"] / run_dir.name / "referenced-urls.json"
        )
        try:
            ref_data = json.loads(referenced_path.read_text())
            if ref_data.get("schema_version") == REFERENCED_URLS_SCHEMA_VERSION:
                for entry in ref_data.get("stories", []):
                    for url in entry.get("referenced_urls", []):
                        normalized = _normalize_url(url)
                        if normalized:
                            blocked.add(normalized)
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            pass
    return blocked


class _HrefCollector(HTMLParser):
    """Collect the hrefs of <a> tags from one article page (dedup record)."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for key, value in attrs:
            if key == "href" and value:
                self.hrefs.append(value)


def _collect_referenced_urls(page_url: str) -> list[str]:
    """Best-effort fetch of one article page; return normalized outbound links.

    Feeds the cross-topic same-event dedup record. Conservative filters keep
    only plausible article/canonical-source links: same-host navigation and
    related-story links, social/utility hosts, and obvious non-article paths
    never enter the record. Never raises — link collection is auxiliary to
    curation and must not fail a topic run.
    """
    try:
        resp = requests.get(
            page_url, headers=_HTML_FETCH_HEADERS, timeout=REFERENCED_URL_TIMEOUT
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []
    if len(resp.content) > 2_000_000 or not resp.text:
        return []
    parser = _HrefCollector()
    try:
        parser.feed(resp.text)
    except Exception:
        return []
    page_host = (urlsplit(page_url).hostname or "").lower()
    if page_host.startswith("www."):
        page_host = page_host[4:]
    seen: set[str] = set()
    out: list[str] = []
    for href in parser.hrefs:
        try:
            absolute = urljoin(page_url, href.strip())
        except ValueError:
            continue
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host or host == page_host:
            continue
        if host in _REFERENCED_URL_SKIP_HOSTS:
            continue
        if _is_listing_url(absolute) or _is_asset_cdn_url(absolute):
            continue
        segments = [s for s in parts.path.strip("/").split("/") if s]
        if not segments or not any(re.search(r"[a-zA-Z]", s) for s in segments):
            continue
        if any(s.lower() in _REFERENCED_URL_SKIP_SEGMENTS for s in segments):
            continue
        normalized = _normalize_url(absolute)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
        if len(out) >= 20:
            break
    return out


def _record_referenced_urls(
    topic: dict,
    fresh: list[dict],
    ongoing: list[dict],
    run_dir: Path,
) -> None:
    """Record canonical/related links from each selected story for later topics.

    Written to <run_dir>/referenced-urls.json (REFERENCED_URLS_SCHEMA_VERSION);
    _load_cross_topic_urls merges these into later topics' blocked set so the
    same event under a different URL is not curated twice (digest-quality audit
    2026-08-26: the OpenAI Jalapeño announcement ran in ai-tech via TechCrunch
    and in ai-hardware via the OpenAI page). Best-effort: a failed fetch simply
    contributes no links and never fails the run.
    """
    output_path = run_dir / "referenced-urls.json"
    stories = fresh + ongoing
    if not stories:
        try:
            output_path.unlink()
        except OSError:
            pass
        return
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(
            lambda s: {
                "url": s.get("url", ""),
                "referenced_urls": _collect_referenced_urls(s.get("url", "")),
            },
            stories,
        ))
    data = {
        "schema_version": REFERENCED_URLS_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stories": records,
    }
    output_path.write_text(json.dumps(data, indent=2))
    total = sum(len(r["referenced_urls"]) for r in records)
    print(f"  [dedup] recorded {total} referenced link(s) across {len(stories)} "
          "selected story(s) for cross-topic same-event blocking")


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


def _consecutive_surfaced_days(digest_dir: Path, url: str, today: date) -> int:
    """How many consecutive prior digest days (ending yesterday) surfaced `url`.

    Same two per-day sources as _load_recent_covered_urls: the run dir's
    06-curated.json (authoritative) and the archived <date>.md fallback.
    """
    normalized = _normalize_url(url)
    days = 0
    day = today - timedelta(days=1)
    while True:
        day_str = day.isoformat()
        appeared = False
        curated = digest_dir / day_str / "06-curated.json"
        if curated.exists():
            try:
                data = json.loads(curated.read_text())
                for story in data.get("fresh", []) + data.get("ongoing", []):
                    if _normalize_url(story.get("url", "")) == normalized:
                        appeared = True
                        break
            except (json.JSONDecodeError, ValueError):
                appeared = False
        else:
            md_file = digest_dir / f"{day_str}.md"
            if md_file.exists():
                appeared = any(
                    _normalize_url(m.group(1)) == normalized
                    for m in re.finditer(
                        r"\[[^\]]*\]\((https?://[^)\s]+)\)", md_file.read_text()
                    )
                )
        if not appeared:
            break
        days += 1
        day -= timedelta(days=1)
    return days


def _enforce_ongoing_resurface_cap(
    proposal: dict,
    stories_in_flight: dict,
    digest_dir: Path,
    today: date | None = None,
) -> tuple[list[str], list[dict]]:
    """Cool a qualified story after too many evidence-free resurfacings.

    Displaying a story never advances evidence-backed activity. The cap still
    bounds consecutive repetition before the five-day inactivity rule: after
    RESURFACE_CAP_DAYS appearances, the next selection is dropped and receives
    an administrative cooled status. Only a selected, source-linked material
    development resets the counter.
    """
    warnings: list[str] = []
    ops: list[dict] = []
    today = today or datetime.now(timezone.utc).date()
    story_by_url = {
        _normalize_url(story.get("url", "")): story
        for story in stories_in_flight.get("stories", [])
    }
    # Evidence-backed update ops (validation already requires same-story
    # evidence) count as genuine development and reset the cap.
    evidenced_urls = {
        _normalize_url(op.get("story_url", ""))
        for op in proposal.get("story_state_proposals", [])
        if op.get("operation") == "update" and op.get("evidence_candidate_ids")
    }
    kept: list[dict] = []
    for selection in proposal.get("selected_ongoing", []):
        url = _normalize_url(selection.get("story_url", ""))
        prior_days = _consecutive_surfaced_days(digest_dir, url, today)
        if prior_days >= RESURFACE_CAP_DAYS and url not in evidenced_urls:
            story = story_by_url.get(url)
            warnings.append(
                f"cooled recurring ongoing story surfaced {prior_days + 1} "
                "consecutive days without an evidence-backed development"
            )
            if story is not None:
                ops.append({
                    "operation": "update",
                    "story_url": story.get("url", ""),
                    "evidence_candidate_ids": [],
                    "latest_dev": story.get("latest_dev", ""),
                    "editorial_significance": story.get("editorial_significance", "medium"),
                    "status": "cooled",
                })
            continue
        kept.append(selection)
    if ops:
        proposal["selected_ongoing"] = kept
    return warnings, ops


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


def _candidate_fresh_date(candidate: dict, today: date | None = None) -> datetime | None:
    """Return the best publication date for a candidate.

    Prefers Phase 4's independently confirmed date; falls back to Phase 1's
    published date. A date_confirmed that is in the future (e.g. an event or
    conference date pulled from the article rather than its publication date)
    is not a valid publication date, so it falls back to date_published
    (digest-quality audit 2026-08-17: ai-hardware dropped the fresh 08-17 Hot
    Chips preview because date_confirmed was the 08-24 conference start date).
    None when neither parses (Phase 5 passed the story through for LLM judgment
    rather than dropping it).
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    confirmed = _parse_date((candidate.get("date_confirmed") or "").strip())
    if confirmed is not None and confirmed.date() <= today:
        return confirmed
    return _parse_date((candidate.get("date_published") or "").strip())


def _is_fresh_eligible(candidate: dict, yesterday: date, today: date | None = None) -> bool:
    """True when a candidate may legitimately appear under "Fresh — Last 24 Hours".

    A candidate with a parseable publication date is fresh-eligible only when
    that date is within the last 24h (>= yesterday) and not in the future
    (<= today). A candidate with no parseable date is kept, mirroring Phase 5's
    pass-through for undetermined dates. This deterministic gate is the
    backstop against the editorial model or critic placing ongoing-window
    stories under Fresh (digest-quality audit 2026-08-12) and against
    future-dated candidates shipping under Fresh (digest-quality audit
    2026-08-14: a 2026-10-15-dated story rendered under "Fresh — Last 24 Hours"
    in the 2026-08-12 ai-tech digest).
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    fresh_date = _candidate_fresh_date(candidate, today)
    if fresh_date is None:
        return True
    return yesterday <= fresh_date.date() <= today

def _story_development_dates(story: dict) -> set[str]:
    """Return distinct evidence-backed UTC dates for a tracked story.

    Legacy tracker entries have no evidence history. Treat only first_seen as
    evidence; last_updated may contain an old evidence-free display touch.
    """
    dates: set[str] = set()
    developments = story.get("developments", [])
    if isinstance(developments, list):
        for development in developments:
            if not isinstance(development, dict):
                continue
            parsed = _parse_date(development.get("date"))
            if parsed is not None:
                dates.add(parsed.date().isoformat())
    if dates:
        return dates
    initial = _parse_date(story.get("first_seen") or story.get("last_updated"))
    return {initial.date().isoformat()} if initial is not None else set()

def _has_validated_high_significance(story: dict) -> bool:
    """True only when a high label carries accepted structured evidence."""
    return (
        story.get("editorial_significance") == "high"
        and isinstance(story.get("significance_evidence"), dict)
        and story.get("significance_validation", {}).get("status") == "accepted"
    )


def _normalize_story_tracking(story: dict, today: date | None = None) -> dict:
    """Migrate one tracker entry to auditable evidence and significance fields."""
    if today is None:
        today = datetime.now(timezone.utc).date()
    normalize_editorial_significance(story)
    if story.get("url"):
        story["url"] = canonicalize_publisher_url(story["url"])
    if not _parse_date(story.get("first_seen")):
        fallback = _parse_date(story.get("last_updated"))
        story["first_seen"] = (
            fallback.date().isoformat() if fallback is not None else today.isoformat()
        )

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    developments = story.get("developments", [])
    if isinstance(developments, list):
        for development in developments:
            if not isinstance(development, dict):
                continue
            parsed = _parse_date(development.get("date"))
            url = str(development.get("url", "")).strip()
            if parsed is None:
                continue
            item = (parsed.date().isoformat(), url)
            if item in seen:
                continue
            seen.add(item)
            normalized.append({"date": item[0], "url": item[1]})

    if not normalized:
        normalized.append({
            "date": story["first_seen"],
            "url": str(story.get("url", "")).strip(),
        })
    normalized.sort(key=lambda item: (item["date"], item["url"]))
    story["developments"] = normalized[-DEVELOPMENT_HISTORY_CAP:]
    story["last_updated"] = max(item["date"] for item in normalized)
    return story


def _is_developing_story(story: dict) -> bool:
    """True only for validated-high stories with evidence on multiple days."""
    return (
        _has_validated_high_significance(story)
        and len(_story_development_dates(story)) >= MIN_DEVELOPMENT_DAYS
    )


def _build_developing_followup_angle(
    stories_in_flight: dict | None,
    today: date | None = None,
) -> dict | None:
    """Build one bounded research angle for material tracker follow-ups."""
    if not stories_in_flight:
        return None
    if today is None:
        today = datetime.now(timezone.utc).date()
    tracked = [
        story for story in stories_in_flight.get("stories", [])
        if story.get("status", "active") in ("active", "cooled")
        and _has_validated_high_significance(story)
        and story.get("first_seen") != today.isoformat()
        and not _is_listing_url(story.get("url", ""))
        and not _is_asset_cdn_url(story.get("url", ""))
    ]
    tracked = sorted(
        tracked, key=lambda story: story.get("last_updated", ""), reverse=True
    )[:FOLLOWUP_STORY_CAP]
    if not tracked:
        return None

    context = [{
        "title": story.get("title", ""),
        "story_url": story.get("url", ""),
        "latest_confirmed_development": story.get("latest_dev", ""),
        "first_seen": story.get("first_seen", ""),
        "last_evidence_date": story.get("last_updated", ""),
        "status": story.get("status", "active"),
    } for story in tracked]
    prompt = (
        "Search specifically for material developments from the last 24 hours in "
        "the high-significance tracked stories below. Search each story; zero results "
        "is a valid and preferable answer when nothing materially changed.\n\n"
        f"{DEVELOPING_STORY_RULES}\n"
        "Return only articles that report a new material fact after the supplied "
        "last_evidence_date. Exclude recaps, explainers, opinions, reactions without "
        "new action, and articles connected only by a broad theme. Each returned "
        "finding must include `develops_story_url`, copied exactly from the matching "
        "`story_url` below. Never invent a relationship or URL.\n\n"
        f"Tracked stories:\n{json.dumps(context, indent=2)}"
    )
    return {"id": "developing-followups", "prompt": prompt, "optional": True}


def _batch(items: list[Any], size: int = BATCH_SIZE) -> list[list[Any]]:
    """Split items into batches of at most `size`."""
    return [items[i:i + size] for i in range(0, len(items), size)]


# ═══════════════════════════════════════════════════════════════════════════
# Phase implementations
# ═══════════════════════════════════════════════════════════════════════════

def phase_1_research(
    topic: dict,
    run_dir: Path,
    stories_in_flight: dict | None = None,
) -> list[dict]:
    """Phase 1: Run broad discovery plus bounded tracked-story follow-ups.

    Each research angle gets its own omp call and uses web search. Returns the
    merged findings with their originating angle preserved.
    """
    global _UPSTREAM_OUTAGE, _RESEARCH_FAILURES, _RESEARCH_SUCCESSES
    _RESEARCH_FAILURES = []
    _RESEARCH_SUCCESSES = 0
    _UPSTREAM_OUTAGE = False

    output_path = run_dir / "01-research-raw.json"
    if output_path.exists():
        print(f"  [skip] Phase 1 output exists: {output_path}")
        return json.loads(output_path.read_text())

    rubric = _editorial_significance_rubric_text(topic)
    angles = list(topic["research_angles"])
    followup_angle = _build_developing_followup_angle(stories_in_flight)
    if followup_angle is not None:
        angles.append(followup_angle)

    system_prompt = (
        "You are a research assistant for a daily newspaper. Search the web for recent "
        "news events and report source-grounded findings in structured JSON. "
        "Write every finding, title, summary, and reason in English regardless of the "
        "source article's language. Translate non-English headlines into concise, idiomatic English.\n\n"
        "IMPORTANT: Do NOT use read to open articles during discovery. Only use "
        "web_search to find stories by their titles and URLs. The articles will be "
        "read later by a separate process. Your job is discovery, not deep reading.\n\n"
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
        '"category": "...", "editorial_significance": "high|medium|low", '
        '"significance_evidence": {"basis": "binding_policy_or_law|'
        'broad_public_consequence|major_conflict_or_disaster|major_financial_scale|'
        'major_product_or_platform_shift|security_or_safety_incident|'
        'widespread_mandatory_migration", "affected_scope": "broad|sector|niche", '
        '"impact": "source-grounded factual sentence"}, '
        '"event": "concise canonical statement of what happened", '
        '"event_terms": ["2-4 distinctive English names or phrases that must all identify '
        'this event"]}\n'
        "Event terms are for deterministic coverage measurement. Generate terms and aliases, "
        "but never estimate popularity, virality, audience interest, or an attention score. "
        "A tracked-story follow-up must also include `develops_story_url` exactly "
        "as supplied by that angle; otherwise omit that field.\n\n"
        "Never construct URLs — only use URLs that appeared in web_search results. "
        "Target 5-8 findings for a broad angle. For a tracked-story follow-up, zero "
        "is valid when nothing materially changed. Be quick — search, compile, output JSON.\n\n"
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

        if isinstance(findings, list):
            for finding in findings:
                if isinstance(finding, dict):
                    finding.setdefault("research_angle_id", angle["id"])

        elapsed = time.time() - t0
        if findings:
            print(f"  [done] {label} — {len(findings)} findings in {elapsed:.0f}s")
            _RESEARCH_SUCCESSES += 1
        elif angle.get("optional") and failure_msg is None:
            print(f"  [done] {label} — no material developments in {elapsed:.0f}s")
        else:
            if failure_msg is None:
                # HTTP 200 but empty broad discovery is degraded rather than a
                # trustworthy "nothing happened" result. The optional follow-up
                # angle above is the exception: no material movement is expected.
                failure_msg = "empty research results (LLM returned no findings)"
            print(f"  [FAIL] {label} — {failure_msg} ({elapsed:.0f}s)")
            check_search_health(f"fail-{angle['id']}")
            _RESEARCH_FAILURES.append(failure_msg)
        return findings

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_RESEARCH) as pool:
        per_angle = list(pool.map(_research_one, angles))
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

    1. Python parses date_published → tags each finding as fresh, older, or too_old.
       too_old findings are dropped without touching the LLM.
    2. Exact tracked-story links from the dedicated follow-up angle are validated.
       The judge admits only material new developments; unchanged recaps stay deduped.
    3. Findings are split into batches of BATCH_SIZE.
    4. Each batch gets one LLM call with topic rules and editorial-significance rubric.
    5. Python restores source metadata and enforces cross-batch/cross-day dedup.

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
    for finding in findings:
        normalize_editorial_significance(finding)
        if finding.get("url"):
            finding["url"] = canonicalize_publisher_url(finding["url"])

    pre_tagged: list[dict] = []
    too_old_count = 0
    tracker_by_url = {
        _normalize_url(story.get("url", "")): story
        for story in (stories_in_flight or {}).get("stories", [])
        if _normalize_url(story.get("url", ""))
    }
    invalid_followup_count = 0

    for f in findings:
        if f.get("research_angle_id") == "developing-followups":
            tracked_url = _normalize_url(f.get("develops_story_url", ""))
            tracked_story = tracker_by_url.get(tracked_url)
            if tracked_story is None or not _has_validated_high_significance(tracked_story):
                invalid_followup_count += 1
                continue
            f["develops_story_url"] = tracked_story.get("url", "")
        else:
            # Only the bounded follow-up angle may assert a cross-day story link.
            # This prevents a broad research result from inventing a relationship.
            f.pop("develops_story_url", None)

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
          f"{sum(1 for f in pre_tagged if f['date_tag'] == 'ongoing')} older, "
          f"{too_old_count} too_old, {invalid_followup_count} invalid follow-up (dropped)")

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

    # Build tracker context only from roots that still satisfy the full evidence
    # contract. Legacy label-only highs must not suppress normal fresh research.
    sif_context = ""
    eligible_tracker_by_url = {
        url: story
        for url, story in tracker_by_url.items()
        if _has_validated_high_significance(story)
    }
    if eligible_tracker_by_url:
        tracked_context = [{
            "title": story.get("title", ""),
            "story_url": story.get("url", ""),
            "latest_confirmed_development": story.get("latest_dev", ""),
            "editorial_significance": story.get("editorial_significance", "medium"),
            "last_evidence_date": story.get("last_updated", ""),
            "status": story.get("status", "active"),
        } for story in eligible_tracker_by_url.values()]
        sif_context = (
            "## Tracked stories\n"
            "A finding with `develops_story_url` came from the dedicated follow-up "
            "search. Approve it only when it reports a material new fact after the "
            "tracked story's last evidence date, and preserve `develops_story_url` "
            "exactly. Reject recaps, commentary, or broad-theme connections. A finding "
            "about a tracked topic without that exact field is not a vetted follow-up; "
            "reject it as `already_tracked_in_sif` rather than re-adding it.\n\n"
            f"{DEVELOPING_STORY_RULES}\n"
            + json.dumps(tracked_context, indent=2) + "\n\n"
        )

    # ── Step 2: Batch LLM calls ──
    rubric = _editorial_significance_rubric_text(topic)
    batches = _batch(pre_tagged, BATCH_SIZE)
    print(f"  Batched into {len(batches)} LLM call(s) ({BATCH_SIZE}/batch)")

    all_approved: list[dict] = []
    all_rejected: list[dict] = []

    system = (
        "You are a strict newspaper editor filtering research findings against quality "
        "rules. Be harsh — a false positive is worse than a false negative.\n\n"
        "You will receive a JSON array of research findings and a set of rules. "
        "For each finding, evaluate every rule. Preserve source fields, especially "
        "`research_angle_id`, `develops_story_url`, `date_tag`, `event`, `event_terms`, "
        "URL, and publication date. You may adjust `editorial_significance` based only "
        "on consequence. Every `high` finding must include structured "
        "`significance_evidence` with an allowed basis, broad/sector affected scope, and "
        "a factual impact sentence grounded in the supplied title/summary. Routine "
        "deprecations, patches, renames, or migration notices are not high without "
        "documented widespread disruption or affected scale. Never estimate popularity "
        "or attention.\n\n"
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
            f"## Editorial Significance Rubric\n\n{rubric}\n\n"
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
            for finding in batch_approved:
                normalize_editorial_significance(finding)
        except Exception as e:
            print(f"  [FAIL] judge_research batch {batch_idx + 1} — {e}, treating all as approved")
            all_approved.extend(batch)

    # ── Step 3: Restore source metadata, then enforce deterministic dedup ──
    seen_urls: set[str] = set()
    deduped_approved: list[dict] = []
    dedup_rejected: list[dict] = []
    original_by_url = {
        _normalize_url(f.get("url", "")): f for f in pre_tagged
        if _normalize_url(f.get("url", ""))
    }

    for f in all_approved:
        url = _normalize_url(f.get("url", ""))
        source = original_by_url.get(url)
        if source is not None:
            for field in (
                "date_tag", "research_angle_id", "develops_story_url",
                "event", "event_terms", "significance_evidence",
            ):
                if field in source:
                    f[field] = source[field]
                else:
                    f.pop(field, None)
        tracked_url = _normalize_url(f.get("develops_story_url", ""))
        if (
            f.get("research_angle_id") == "developing-followups"
            and (
                tracked_url not in tracker_by_url
                or not _has_validated_high_significance(tracker_by_url[tracked_url])
            )
        ):
            dedup_rejected.append({"finding": f, "reason": "invalid_followup_link"})
        elif url and url in seen_urls:
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

def _editorial_only_priority(item: dict) -> dict:
    normalize_editorial_significance(item)
    significance = item["editorial_significance"]
    item.setdefault("attention", {
        "schema_version": ATTENTION_SCHEMA_VERSION,
        "provider": "GDELT DOC 2.0",
        "status": "out_of_scope",
        "attention_now": 50.0,
        "digest_prominence": 50.0,
        "confidence": 0.0,
        "age_bucket": "over-24h",
        "normalized_signals": {},
        "evidence": {
            "channels_available": [],
            "channels_unavailable": ["news_coverage", "homepage_prominence", "social", "video"],
        },
    })
    item["priority_score"] = EDITORIAL_POINTS[significance]
    item["priority_explanation"] = (
        f"{significance.title()} editorial significance; attention scoring applies only "
        "to events first observed or materially updated in the last 24 hours."
    )
    return item


def phase_2b_attention(
    topic: dict,
    fresh: list[dict],
    ongoing: list[dict],
    run_dir: Path,
) -> tuple[list[dict], list[dict]]:
    """Measure observable news attention without asking an LLM for popularity."""
    output_path = run_dir / "02b-attention.json"
    if output_path.exists():
        try:
            cached = json.loads(output_path.read_text())
            if cached.get("schema_version") == ATTENTION_SCHEMA_VERSION:
                return cached.get("fresh", []), cached.get("ongoing", [])
        except (json.JSONDecodeError, OSError):
            pass

    print(f"  [run ] attention — {len(fresh)} fresh event(s)")
    started = time.time()
    scored_fresh, attention_artifact = score_attention(
        fresh,
        ATTENTION_CACHE_DIR,
    )
    scored_ongoing = [
        _editorial_only_priority(copy.deepcopy(item)) for item in ongoing
    ]
    output = {
        **attention_artifact,
        "fresh": scored_fresh,
        "ongoing": scored_ongoing,
    }
    output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")

    issue_date = (
        run_dir.name
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_dir.name)
        else datetime.now(timezone.utc).date().isoformat()
    )
    archive_path = ATTENTION_ARCHIVE_DIR / issue_date / f"{topic['web_slug']}.json"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(attention_artifact, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(archive_path)

    if ATTENTION_CACHE_DIR.exists():
        cutoff = time.time() - 48 * 3600
        for cache_path in ATTENTION_CACHE_DIR.glob("*.json"):
            try:
                if cache_path.stat().st_mtime < cutoff:
                    cache_path.unlink()
            except OSError:
                pass

    elapsed = time.time() - started
    print(
        f"  [done] attention — {attention_artifact['available']} observed, "
        f"{attention_artifact['unavailable']} unavailable, "
        f"{attention_artifact['cache_hits']} cache hit(s) ({elapsed:.0f}s)"
    )
    return scored_fresh, scored_ongoing


def phase_3_rank(
    topic: dict,
    fresh: list[dict],
    ongoing: list[dict],
    stories_in_flight: dict,
    run_dir: Path,
) -> tuple[list[dict], list[dict]]:
    """Phase 3: Deterministic priority ranking with caps.

    Pool A: Fresh findings
      - Sort by final product priority (editorial significance + observed attention)
      - Cap: FRESH_CAP (12)

    Pool B: Older articles (2-5 days old from Phase 2)
      - Sort by editorial-only priority, then publication recency
      - Cap: ONGOING_CAP (5)

    Pool C: qualified developing stories — does NOT enter Phase 4
      - Requires high editorial significance and evidence-backed movement on 2+ UTC dates
      - Sort by last_updated descending and cap at SIF_CAP (3)
      - Passed directly to Phase 6 with its evidence history and latest development

    Returns (phase_4_queue, sif_candidates).
    Phase 4 queue = Pool A + Pool B, with fresh first.
    """
    output_path = run_dir / "03-urls-ranked.json"
    if output_path.exists():
        try:
            data = json.loads(output_path.read_text())
            if data.get("ranking_schema_version") == RANKING_SCHEMA_VERSION:
                print(f"  [skip] Phase 3 output exists: {output_path}")
                return data.get("phase_4_queue", []), data.get("sif_candidates", [])
        except (json.JSONDecodeError, OSError):
            pass

    fresh = [normalize_editorial_significance(item) for item in fresh]
    ongoing = [normalize_editorial_significance(item) for item in ongoing]

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
    # URL-host validation: a candidate whose URL sits on a publisher asset CDN
    # (e.g. assets.theregister.com) is not an article and must never reach
    # fetch or curation (digest-quality audit 2026-08-24: research invented
    # assets.theregister.com article links that 405'd; the tracker echoed them
    # in the daily Ongoing email for five days).
    asset_cdn_rejected = [
        item for item in eligible_fresh + eligible_ongoing
        if _is_asset_cdn_url(item.get("url", ""))
    ]
    eligible_fresh = [
        item for item in eligible_fresh
        if not _is_asset_cdn_url(item.get("url", ""))
    ]
    eligible_ongoing = [
        item for item in eligible_ongoing
        if not _is_asset_cdn_url(item.get("url", ""))
    ]
    if asset_cdn_rejected:
        print(f"  [Phase 3 URL-host] rejected {len(asset_cdn_rejected)} "
              "asset-CDN URL(s) (not article hosts) before fetch")

    # Product priority combines editorial consequence with observed attention.
    pool_a = sorted(
        eligible_fresh,
        key=priority_sort_key,
        reverse=True,
    )[:FRESH_CAP]

    pool_b = sorted(
        eligible_ongoing,
        key=priority_sort_key,
        reverse=True,
    )[:ONGOING_CAP]

    # Pool C is the only source for the rendered Developing and Ongoing section.
    # Every candidate must already have high editorial significance and
    # evidence-backed movement on multiple dates.
    active_sif = [
        story for story in stories_in_flight.get("stories", [])
        if story.get("status") == "active"
        and _is_developing_story(story)
        and _normalize_url(story.get("url", "")) not in other_topic_urls
        and not _is_listing_url(story.get("url", ""))
        and not _is_asset_cdn_url(story.get("url", ""))
    ]
    pool_c = sorted(
        active_sif, key=lambda s: s.get("last_updated", ""), reverse=True
    )[:SIF_CAP]

    phase_4_queue = pool_a + pool_b
    for item in phase_4_queue:
        item["ranking_schema_version"] = RANKING_SCHEMA_VERSION

    output = {
        "ranking_schema_version": RANKING_SCHEMA_VERSION,
        "phase_4_queue": phase_4_queue,
        "sif_candidates": pool_c,
        "pool_a": pool_a,
        "pool_b": pool_b,
        "cross_topic_rejected": cross_topic_rejected,
    }
    output_path.write_text(json.dumps(output, indent=2))
    print(f"  Phase 3 done: Pool A={len(pool_a)} fresh, Pool B={len(pool_b)} older, "
          f"Pool C={len(pool_c)} developing SIF → {len(phase_4_queue)} total for fetch")
    return phase_4_queue, pool_c

def phase_4_fetch(topic: dict, findings: list[dict], run_dir: Path) -> list[dict]:
    """Fetch and summarize articles with a shared cache and two-worker bound."""
    output_path = run_dir / "04-fetch-summaries.json"
    if output_path.exists():
        try:
            cached = json.loads(output_path.read_text())
        except (json.JSONDecodeError, OSError):
            cached = []
        if cached and all(
            item.get("ranking_schema_version") == RANKING_SCHEMA_VERSION
            for item in cached
        ):
            print(f"  [skip] Phase 4 output exists: {output_path}")
            return cached
    pruned_cache_entries = _prune_article_cache()
    if pruned_cache_entries:
        print(f"  [cache] pruned {pruned_cache_entries} expired/invalid entry(s)")

    system_prompt = (
        "You are a research assistant. Read ONE article with the read tool and produce a "
        "topic-neutral, detailed factual summary. Do not search. Write the summary and "
        "key_details in English even when the article is in another language. Return "
        "`title` in English: keep an English headline verbatim, and faithfully translate "
        "a non-English headline without adding facts or commentary.\n\n"
        "Output one JSON object in ```json fences with these fields:\n"
        '  {"title": "English article title", "url": "the URL you read", '
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

        def _attempt(extra: str = "") -> dict:
            prompt = (
                f"Fetch this article: {url}\n\n"
                f"Title from research: {title}\n\n"
                "Use read to open the article. Then output your summary as JSON "
                "wrapped in ```json fences."
                f"{extra}"
            )
            raw = _call_omp_p(
                prompt, model=MODEL, timeout=FETCH_TIMEOUT,
                append_system=system_prompt,
            )
            result = _extract_json(raw, f"{label} output")
            if not isinstance(result, dict):
                raise ValueError(
                    f"fetch output is not a JSON object (got {type(result).__name__})")
            result["url"] = url
            return result

        try:
            result = _attempt()
        except Exception as first_error:
            # Retry once: model output sometimes truncates mid-JSON (no closing
            # fence) or comes back empty, which previously dropped the story
            # from the digest entirely. A fresh attempt with explicit brevity
            # instructions usually completes within the output limit.
            print(f"  [retry] {label} — attempt 1 failed: {first_error}; retrying once")
            try:
                result = _attempt(
                    "\n\nIMPORTANT: your previous response was truncated or invalid. "
                    "Output ONLY the complete JSON object in ```json fences, closed "
                    "properly. Keep the summary to 2-3 sentences and key_details to "
                    "at most 4 short bullets so the response is short enough to finish."
                )
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
        try:
            _save_article_cache(url, result, model=MODEL)
        except OSError as cache_error:
            print(f"  [cache warn] {label} — {cache_error}")
        elapsed = time.time() - started
        status = "✓" if result.get("fetch_success", True) else "✗"
        print(f"  [done] {label} — {status} ({elapsed:.0f}s)")
        return {**finding, **result, "url": url, "cache_hit": False}

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
        try:
            cached = json.loads(output_path.read_text())
        except (json.JSONDecodeError, OSError):
            cached = []
        if cached and all(
            item.get("ranking_schema_version") == RANKING_SCHEMA_VERSION
            for item in cached
        ):
            print(f"  [skip] Phase 5 output exists: {output_path}")
            return cached

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

    # Hygiene (digest-quality audit 2026-08-29): every surviving candidate must
    # carry a parseable date_confirmed. When neither Phase 4's fetch nor the
    # Phase 5 re-fetch confirms a publication date, fall back explicitly to
    # Phase 1's date_published instead of shipping null. ai-tech 08-29 shipped
    # Hunyuan Hy4 and GLM-5.3 with date_confirmed=null; priority_sort_key's
    # `date_confirmed or date_published` fallback kept ranking deterministic,
    # but the null field is a schema-hygiene gap.
    for s in validated:
        dc = (s.get("date_confirmed") or "").strip()
        if not dc or _parse_date(dc) is None:
            s["date_confirmed"] = (s.get("date_published") or "").strip()

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
            # A failed fetch is a hard drop, but record WHY so the drop is
            # auditable instead of silent (digest-quality audit 2026-08-19:
            # ai-tech shipped an empty Fresh section and 05-summaries-judged.json
            # showed fetch_success=false with empty judge_issues).
            verdict = "drop"
            issues = ["fetch_failed: article could not be fetched/summarized as confirmed"]
        else:
            issues = j.get("issues", [])
            if verdict == "fix" and j.get("fixed_summary"):
                s["summary"] = j["fixed_summary"]
        s["judge_verdict"] = verdict
        s["judge_issues"] = issues
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
    source = value if isinstance(value, str) and value.strip() else fallback
    text = " ".join(source.split()) if isinstance(source, str) else ""
    if len(text) <= limit:
        return text
    clipped = text[:limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{clipped}…"


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
        candidate = normalize_editorial_significance(copy.deepcopy(item))
        candidate["candidate_id"] = _editorial_candidate_id(candidate)
        eligible.append(candidate)

    eligible = sorted(
        eligible,
        key=priority_sort_key,
        reverse=True,
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
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
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
        if _is_listing_url(source.get("url", "")):
            # A section/date archive page is not an article; rejecting the
            # selection also keeps it out of stories-in-flight (digest-quality
            # audit 2026-08-21: Guardian /all listing URLs entered the world
            # tracker and resurfaced as ongoing on consecutive days).
            warnings.append(f"dropped listing URL fresh selection {candidate_id}")
            continue
        if _is_asset_cdn_url(source.get("url", "")):
            # A publisher asset-CDN host is not an article host; the link is
            # dead on arrival (digest-quality audit 2026-08-24:
            # assets.theregister.com research links 405'd and resurfaced in
            # the tracker). Rejecting the selection also keeps it out of
            # stories-in-flight.
            warnings.append(f"dropped asset-CDN fresh selection {candidate_id}")
            continue
        if candidate_id in selected_ids:
            warnings.append(f"ignored duplicate fresh selection {candidate_id}")
            continue
        if not _is_fresh_eligible(source, yesterday):
            # Deterministic freshness gate: an ongoing-window (2-5 day old)
            # candidate must never ship under "Fresh — Last 24 Hours", even
            # when the model selected it and the critic missed it (digest-quality
            # audit 2026-08-12: ai-hardware + agentic-platform shipped stale
            # stories under Fresh), and a future-dated candidate must not either
            # (digest-quality audit 2026-08-14: a 2026-10-15-dated story shipped
            # under Fresh). The candidate is treated as unselected, so it also
            # gets no tracker add/update.
            stale_date = _candidate_fresh_date(source)
            kind = "future-dated" if stale_date.date() > yesterday else "stale"
            warnings.append(
                f"dropped {kind} fresh selection {candidate_id} "
                f"(best date {stale_date.date().isoformat()} is outside the 24h window)"
            )
            continue
        selected_ids.add(candidate_id)
        declared_related = _normalize_url(source.get("develops_story_url", ""))
        requested_related = _normalize_url(item.get("related_story_url", ""))
        related = ""
        if declared_related:
            target = tracker_by_url.get(declared_related)
            if target is None or not _has_validated_high_significance(target):
                warnings.append(
                    f"removed invalid developing-story link from {candidate_id}"
                )
            else:
                related = declared_related
                if requested_related and requested_related != declared_related:
                    warnings.append(
                        f"replaced mismatched related story on {candidate_id}"
                    )
        elif requested_related:
            # Only Phase 1's dedicated follow-up search may establish a
            # cross-article story relationship.
            warnings.append(f"removed unverified related story from {candidate_id}")
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
        if _is_listing_url(item.get("story_url", item.get("url", ""))):
            warnings.append(f"ignored listing URL ongoing story {normalized!r}")
            continue
        if _is_asset_cdn_url(item.get("story_url", item.get("url", ""))):
            warnings.append(f"ignored asset-CDN ongoing story {normalized!r}")
            continue
        source = ongoing_by_url.get(normalized)
        if source is None:
            warnings.append(f"ignored unknown ongoing story {normalized!r}")
            continue
        if source.get("status", "active") != "active" or not _is_developing_story(source):
            warnings.append(
                f"ignored unqualified developing story {normalized!r}"
            )
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

    related_by_candidate_id = {
        item["candidate_id"]: _normalize_url(item.get("related_story_url", ""))
        for item in fresh
    }
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

        if operation == "add":
            candidate_id = item.get("candidate_id", "")
            if candidate_id not in selected_ids:
                warnings.append(
                    f"ignored tracker add for unselected candidate {candidate_id!r}")
                continue
            source = candidate_by_id[candidate_id]
            if related_by_candidate_id.get(candidate_id):
                warnings.append(
                    f"ignored tracker add for linked development {candidate_id}"
                )
                continue
            if not _has_validated_high_significance(source):
                warnings.append(
                    f"ignored unvalidated-high tracker add for {candidate_id}"
                )
                continue
            if _normalize_url(source.get("url", "")) in tracker_by_url:
                warnings.append(f"ignored tracker add for existing story {candidate_id}")
                continue
            if (
                _is_listing_url(source.get("url", ""))
                or _is_asset_cdn_url(source.get("url", ""))
            ):
                warnings.append(f"ignored invalid-URL tracker add for {candidate_id}")
                continue
            state_proposals.append({
                "operation": "add",
                "candidate_id": candidate_id,
                "evidence_candidate_ids": [candidate_id],
                "latest_dev": latest_dev or source.get("summary", ""),
                "editorial_significance": "high",
                "status": "active",
            })
        elif operation == "update":
            normalized = _normalize_url(item.get("story_url", ""))
            source = tracker_by_url.get(normalized)
            if (
                source is None
                or not _has_validated_high_significance(source)
                or not evidence
                or not latest_dev
                or _is_listing_url(item.get("story_url", ""))
                or _is_asset_cdn_url(item.get("story_url", ""))
            ):
                warnings.append(
                    f"ignored unsupported tracker update for {normalized!r}")
                continue
            # Cross-article evidence is safe only when the dedicated follow-up
            # research declared the exact tracker URL and the selected Fresh
            # item retained that validated relationship. This admits genuine
            # multi-day developments without reopening broad-theme overwrites.
            linked_evidence = [
                candidate_id for candidate_id in evidence
                if (
                    _normalize_url(candidate_by_id[candidate_id].get("url", ""))
                    == normalized
                    or related_by_candidate_id.get(candidate_id) == normalized
                )
            ]
            if not linked_evidence:
                warnings.append(
                    f"ignored unlinked tracker update for {normalized!r}")
                continue
            state_proposals.append({
                "operation": "update",
                "story_url": source.get("url", ""),
                "evidence_candidate_ids": linked_evidence,
                "latest_dev": latest_dev,
                "editorial_significance": "high",
                "status": "active",
            })
        else:
            warnings.append(f"ignored unknown state operation {operation!r}")

    # State continuity must not depend on model bookkeeping. Every selected
    # high-significance root story gets one initial evidence record; every vetted
    # follow-up gets an evidence-backed update. Follow-up articles never become
    # duplicate root tracker entries.
    added_candidate_ids = {
        op["candidate_id"] for op in state_proposals if op["operation"] == "add"
    }
    updated_story_urls = {
        _normalize_url(op.get("story_url", ""))
        for op in state_proposals if op["operation"] == "update"
    }
    for selection in fresh:
        candidate_id = selection["candidate_id"]
        source = candidate_by_id[candidate_id]
        related = related_by_candidate_id.get(candidate_id, "")
        if related and related not in updated_story_urls:
            tracked = tracker_by_url[related]
            state_proposals.append({
                "operation": "update",
                "story_url": tracked.get("url", ""),
                "evidence_candidate_ids": [candidate_id],
                "latest_dev": selection["editorial_summary"],
                "editorial_significance": "high",
                "status": "active",
            })
            updated_story_urls.add(related)
        elif (
            not related
            and source.get("editorial_significance") == "high"
            and candidate_id not in added_candidate_ids
            and _normalize_url(source.get("url", "")) not in tracker_by_url
        ):
            state_proposals.append({
                "operation": "add",
                "candidate_id": candidate_id,
                "evidence_candidate_ids": [candidate_id],
                "latest_dev": selection["editorial_summary"],
                "editorial_significance": "high",
                "status": "active",
            })
            added_candidate_ids.add(candidate_id)

    # The two-story send floor may use only stories that independently satisfy
    # the same Developing and Ongoing contract. Content volume never overrides
    # editorial significance or multi-day evidence; a thin section is not padded
    # with a one-off article.
    if len(fresh) + len(ongoing) < 2:
        selected_urls = {
            _normalize_url(candidate_by_id[item["candidate_id"]].get("url", ""))
            for item in fresh
        } | {
            _normalize_url(item["story_url"]) for item in ongoing
        }
        filler_pool = sorted(
            (story for story in sif_candidates
             if story.get("status", "active") == "active"
             and _is_developing_story(story)
             and not _is_listing_url(story.get("url", ""))
             and not _is_asset_cdn_url(story.get("url", ""))
             and _normalize_url(story.get("url", "")) not in selected_urls),
            key=lambda story: story.get("last_updated", ""), reverse=True,
        )
        while filler_pool and len(fresh) + len(ongoing) < 2 and len(ongoing) < 3:
            story = filler_pool.pop(0)
            story_url = _normalize_url(story.get("url", ""))
            ongoing.append({
                "story_url": story.get("url", ""),
                "rank": len(ongoing) + 1,
                "summary": story.get("latest_dev", story.get("title", "")),
                "why_still_relevant": (
                    "High-impact story with material developments confirmed on "
                    "multiple days; the latest verified development remains active."
                ),
            })
            selected_ongoing.add(story_url)


    domains: dict[str, int] = {}
    for item in fresh:
        source = candidate_by_id[item["candidate_id"]]
        domain = source.get("source_domain") or urlsplit(source.get("url", "")).hostname or ""
        domains[domain] = domains.get(domain, 0) + 1
    concentrated = sorted(domain for domain, count in domains.items() if domain and count > 2)
    if concentrated:
        warnings.append(f"source concentration above 2: {', '.join(concentrated)}")
        # Enforce the cap: keep the two highest-ranked candidates per over-limit
        # domain and drop the lower-ranked same-source selections, so a
        # single-source Fresh section can no longer ship (digest-quality audit
        # 2026-08-14: ai-tech shipped 5 TechCrunch stories, ai-hardware 4 Data
        # Center Dynamics stories). `fresh` is already in rank order.
        capped: list[dict] = []
        per_domain: dict[str, int] = {}
        for item in fresh:
            source = candidate_by_id[item["candidate_id"]]
            domain = source.get("source_domain") or urlsplit(source.get("url", "")).hostname or ""
            if domain in concentrated:
                if per_domain.get(domain, 0) >= 2:
                    warnings.append(
                        f"dropped fresh selection {item['candidate_id']} "
                        f"(source concentration cap: max 2 per domain)"
                    )
                    continue
                per_domain[domain] = per_domain.get(domain, 0) + 1
            capped.append(item)
        fresh = capped
    # A source-concentration drop also removes that candidate's tracker
    # evidence. Persistent state must describe only stories that actually ship.
    final_selected_ids = {item["candidate_id"] for item in fresh}
    filtered_state: list[dict] = []
    for operation in state_proposals:
        if operation["operation"] == "add":
            if operation["candidate_id"] in final_selected_ids:
                filtered_state.append(operation)
            continue
        evidence = [
            candidate_id for candidate_id in operation.get("evidence_candidate_ids", [])
            if candidate_id in final_selected_ids
        ]
        if not evidence:
            continue
        filtered_state.append({**operation, "evidence_candidate_ids": evidence})
    state_proposals = filtered_state
    if (
        any(_is_fresh_eligible(candidate, yesterday) for candidate in candidates)
        and not fresh
    ):
        warnings.append("proposal selected no valid fresh stories")

    selected_sources = [
        candidate_by_id[item["candidate_id"]]
        for item in fresh
    ] + [
        ongoing_by_url[_normalize_url(item["story_url"])]
        for item in ongoing
    ]
    selected_domains = sorted({
        source.get("source_domain")
        or urlsplit(source.get("url", "")).hostname
        or "unknown"
        for source in selected_sources
    })
    selected_categories = sorted({
        source.get("category", "Uncategorized")
        for source in selected_sources
    })
    if selected_sources:
        balance_summary = (
            f"Validated selection: {len(fresh)} fresh, "
            f"{len(ongoing)} developing/ongoing; "
            f"{len(selected_domains)} source domain(s); categories: "
            f"{', '.join(selected_categories)}."
        )
    else:
        balance_summary = (
            "Validated selection: no publishable fresh or developing stories."
        )

    return {
        "selected_fresh": fresh,
        "selected_ongoing": ongoing,
        "story_state_proposals": state_proposals,
        "rejected": proposal.get("rejected", []),
        "gaps": _clean_editorial_text(proposal.get("gaps"), limit=800),
        "balance_summary": balance_summary,
    }, warnings


def _raw_editorial_proposal(
    candidates: list[dict],
    sif_candidates: list[dict],
) -> dict:
    """Build a source-only last-resort proposal after both curation models fail."""
    selected_fresh = [
        {
            "candidate_id": candidate["candidate_id"],
            "rank": index,
            "editorial_summary": candidate.get("summary", ""),
            "selection_reason": "deterministic fallback",
            "related_story_url": None,
        }
        for index, candidate in enumerate(candidates[:7], 1)
    ]
    return {
        "selected_fresh": selected_fresh,
        "selected_ongoing": [
            {
                "story_url": story.get("url", ""),
                "rank": index,
                "summary": story.get("latest_dev", ""),
                "why_still_relevant": story.get("latest_dev", ""),
            }
            for index, story in enumerate(sif_candidates[:3], 1)
        ],
        # Keep only high-significance root stories as follow-up candidates. A
        # selected follow-up article updates its linked root during validation
        # and must never become a second root entry.
        "story_state_proposals": [
            {
                "operation": "add",
                "candidate_id": selection["candidate_id"],
                "evidence_candidate_ids": [selection["candidate_id"]],
                "latest_dev": candidate.get("summary", ""),
                "editorial_significance": "high",
                "status": "active",
            }
            for selection, candidate in zip(selected_fresh, candidates[:7])
            if candidate.get("editorial_significance") == "high"
            and not candidate.get("develops_story_url")
        ],
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
    """Apply validated operations while preserving auditable evidence history."""
    updated = copy.deepcopy(stories_in_flight)
    stories = updated.setdefault("stories", [])
    parsed_today = _parse_date(today_str)
    today = (
        parsed_today.date() if parsed_today is not None
        else datetime.now(timezone.utc).date()
    )
    for story in stories:
        _normalize_story_tracking(story, today)
    candidate_by_id = {
        candidate["candidate_id"]: candidate
        for candidate in candidates if candidate.get("candidate_id")
    }
    story_by_url = {
        _normalize_url(story.get("url", "")): story
        for story in stories
        if _normalize_url(story.get("url", ""))
    }
    for operation in proposal.get("story_state_proposals", []):
        if operation["operation"] == "add":
            source = candidate_by_id.get(operation["candidate_id"])
            if source is None or not _has_validated_high_significance(source):
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
                "editorial_significance": "high",
                "significance_evidence": copy.deepcopy(
                    source.get("significance_evidence", {})
                ),
                "significance_validation": copy.deepcopy(
                    source.get("significance_validation", {})
                ),
                "first_seen": today_str,
                "developments": [{
                    "date": today_str,
                    "url": source.get("url", ""),
                }],
            }
            stories.append(story)
            story_by_url[normalized] = story
            continue

        if operation["operation"] != "update":
            continue
        story = story_by_url.get(_normalize_url(operation.get("story_url", "")))
        if story is None:
            continue
        evidence_sources = [
            candidate_by_id[candidate_id]
            for candidate_id in operation.get("evidence_candidate_ids", [])
            if candidate_id in candidate_by_id
        ]
        if not evidence_sources:
            # Evidence-free updates are administrative only (for example,
            # deterministic cooling). They never fabricate a development date
            # or extend the evidence-backed activity window.
            story["status"] = operation.get("status", story.get("status", "active"))
            continue
        story["developments"].extend({
            "date": today_str,
            "url": source.get("url", ""),
        } for source in evidence_sources)
        story["latest_dev"] = operation["latest_dev"]
        story["editorial_significance"] = "high"
        story["status"] = operation.get("status", "active")
        _normalize_story_tracking(story, today)
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
        try:
            data = json.loads(output_path.read_text())
            if data.get("ranking_schema_version") == RANKING_SCHEMA_VERSION:
                print(f"  [skip] Phase 6 output exists: {output_path}")
                return (
                    data["fresh"],
                    data.get("stories_in_flight", stories_in_flight),
                    data["ongoing"],
                )
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    started = time.time()
    today_str = datetime.now().strftime("%Y-%m-%d")
    blocked_urls = _load_cross_topic_urls(topic, run_dir)
    candidates, dropped = _prepare_editorial_candidates(summaries, blocked_urls)
    # Deterministic freshness gate: only candidates within the last 24h (or
    # with undetermined dates) may populate the Fresh section. When none are
    # eligible, an empty Fresh section is the honest outcome and must not be
    # treated as a model/critic failure (digest-quality audit 2026-08-12).
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    fresh_eligible = [
        candidate for candidate in candidates
        if _is_fresh_eligible(candidate, yesterday)
    ]
    sif_candidates = [
        story for story in sif_candidates
        if _normalize_url(story.get("url", "")) not in blocked_urls
        and story.get("status", "active") == "active"
        and _is_developing_story(story)
        and not _is_listing_url(story.get("url", ""))
        and not _is_asset_cdn_url(story.get("url", ""))
    ]
    print(f"  [6a prep] {len(candidates)} candidates, {len(sif_candidates)} SIF, "
          f"{len(blocked_urls)} cross-topic URL(s) blocked")

    system = (
        "You are the lead editor of a daily newspaper section. Make one coherent "
        "proposal from vetted candidates and qualified developing stories. Selection, "
        "source/topic balance, story connections, and state proposals are interdependent. "
        "Treat the deterministic `priority_score` as the primary ranking signal; never "
        "alter or invent attention, confidence, or priority values. Do not write a "
        "section standfirst and do not replace the tracker.\n\n"
        f"{DEVELOPING_STORY_RULES}\n"
        "Select 5-7 fresh stories when enough good candidates exist. Select zero to "
        "three Developing and Ongoing stories only from the supplied qualified SIF "
        "candidates; zero is correct when none adds value. Never fill an ongoing quota. "
        "Every selection must use an exact candidate_id or story_url supplied. For a "
        "Fresh candidate carrying `develops_story_url`, copy that exact URL into "
        "`related_story_url` and propose an evidence-backed update of that tracked "
        "story. No other cross-article connection is allowed. Add only selected, "
        "high-significance root candidates to the tracker. `why_still_relevant` must "
        "name the latest material development, never merely say the item is still "
        "relevant, recent, unresolved, or the latest commentary. Write concise newspaper "
        "copy that leads with facts; never refer to a digest, edition, candidate list, "
        "ranking process, or the reader. Write all prose in English regardless of source "
        "language; keep the supplied English story titles unchanged.\n\n"
        "Output one JSON object in ```json fences:\n"
        '{"selected_fresh":[{"candidate_id":"...","rank":1,'
        '"editorial_summary":"2-3 factual sentences","selection_reason":"...",'
        '"related_story_url":null}],'
        '"selected_ongoing":[{"story_url":"...","rank":1,'
        '"summary":"what the story is","why_still_relevant":"what changed"}],'
        '"story_state_proposals":[{"operation":"add|update",'
        '"candidate_id":"for add","story_url":"for update",'
        '"evidence_candidate_ids":["..."],"latest_dev":"...",'
        '"editorial_significance":"high|medium|low","status":"active|cooled"}],'
        '"rejected":[{"candidate_id":"...","reason":"..."}],'
        '"gaps":"...","balance_summary":"..."}'
    )
    user = (
        f"## Date\n{today_str}\n\n"
        f"## Vetted candidates\n{json.dumps(candidates, indent=2)}\n\n"
        f"## Qualified Developing and Ongoing candidates\n"
        f"{json.dumps(sif_candidates, indent=2)}\n\n"
        f"## Full tracker for connections and proposed updates\n"
        f"{json.dumps(stories_in_flight, indent=2)}\n\n"
        f"## Editorial significance rubric\n{_editorial_significance_rubric_text(topic)}\n\n"
        f"## Developing-story contract\n{DEVELOPING_STORY_RULES}\n"
        f"## Dropped summaries; never select\n"
        f"{json.dumps([{'title': item.get('title'), 'url': item.get('url'), 'reason': item.get('judge_issues', [])} for item in dropped], indent=2)}"
    )

    proposal: dict | None = None
    proposal_model = ""
    proposal_warnings: list[str] = []
    proposal_errors: list[str] = []
    freshness_hint = ""
    for requested_model, effective_model in _model_attempts(MODEL, MODEL_FALLBACK):
        # Retry the primary once before falling back to a weaker model: a single
        # truncated/malformed response or transient transport error used to
        # degrade the whole editorial stage to the fallback model (digest-quality
        # audit 2026-08-11: world proposal fell back after one extraction error).
        attempts = 2 if effective_model == _effective_model(MODEL) else 1
        attempt = 0
        while attempt < attempts:
            attempt += 1
            try:
                raw = _call_llm_proxy(
                    system, user + freshness_hint, model=requested_model,
                    timeout=EDITORIAL_TIMEOUT,
                )
                parsed = _extract_json(raw, f"editorial proposal ({effective_model})")
                validated, warnings = _validate_editorial_proposal(
                    parsed, candidates, sif_candidates, stories_in_flight, blocked_urls
                )
                if fresh_eligible and not validated["selected_fresh"]:
                    if not freshness_hint:
                        # All fresh picks fell outside the last-24h window (or
                        # none were selected) while fresh-eligible candidates
                        # exist. Retry this model once with the freshness window
                        # reinforced instead of failing straight through to raw
                        # fallback (digest-quality audit 2026-08-14:
                        # agentic-platform shipped deterministic raw fallback
                        # with no critic review).
                        freshness_hint = (
                            "\n\n## Freshness window reminder\n"
                            "Your previous proposal was rejected because it "
                            "selected no valid fresh stories. \"Fresh — Last 24 "
                            "Hours\" may only contain stories whose best "
                            "publication date (date_confirmed, else "
                            "date_published) is yesterday or today (UTC) — "
                            "never older or future-dated. Fresh-eligible "
                            "candidates exist in the supplied list; re-select "
                            "5-7 fresh stories from them."
                        )
                        attempt -= 1
                        raise ValueError(
                            "model selected no valid fresh stories; retrying "
                            "with reinforced freshness hint"
                        )
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
        if proposal is not None:
            break

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
            "You are the independent critic for a daily newspaper section. Review the "
            "selection, source/topic balance, developing-story links, and persistent "
            "state proposals. Return bounded changes only; never rewrite the whole proposal. "
            "The deterministic `priority_score` owns ranking: move a story only to correct "
            "a clear ordering violation and never estimate or alter attention. Enforce the "
            "Developing and Ongoing contract strictly: remove anything merely old, still "
            "relevant, one-off, or unsupported by material developments on multiple dates. "
            "Check for a missed higher-priority candidate, unsupported connections, source "
            "concentration, stale material, and state changes without selected evidence. "
            "Write all notes and reasoning in English.\n\n"
            f"{DEVELOPING_STORY_RULES}\n"
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
            f"## Deterministic warnings\n{json.dumps(proposal_warnings, indent=2)}\n\n"
            f"## Developing-story contract\n{DEVELOPING_STORY_RULES}"
        )
        critic_models = (MODEL_REVIEWER, MODEL_FALLBACK)
        critic_rejected = False
        for requested_model, effective_model in _model_attempts(*critic_models):
            # Retry the primary critic once before falling back to a weaker model
            # (a transient proxy 500 degraded review on 2026-08-11); an
            # authoritative reject is not retried.
            attempts = 2 if effective_model == _effective_model(MODEL_REVIEWER) else 1
            for _ in range(attempts):
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
                    if fresh_eligible and not validated["selected_fresh"]:
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
                    if critic_rejected:
                        break  # authoritative reject: try the next model
            if review_status == "reviewed":
                break
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

    # The resurface cap bounds repetition even before evidence inactivity reaches
    # the five-day auto-cool threshold. Its evidence-free update is administrative:
    # state application changes status only and never advances last_updated.
    cap_warnings, cap_ops = _enforce_ongoing_resurface_cap(
        final_proposal, stories_in_flight, run_dir.parent
    )
    if cap_warnings:
        for warning in cap_warnings:
            print(f"  [6c resurface] {warning}")
        review_warnings.extend(cap_warnings)
    if cap_ops:
        final_proposal["story_state_proposals"] = (
            final_proposal.get("story_state_proposals", []) + cap_ops
        )

    updated_sif = _apply_story_state_proposals(
        stories_in_flight, final_proposal, candidates, today_str
    )
    fresh, ongoing = _materialize_editorial_selection(
        final_proposal, candidates, updated_sif
    )
    # Hygiene assertion (digest-quality audit 2026-08-29): every curated fresh
    # story must carry date_confirmed; Phase 5 backfills it from date_published
    # when the fetch could not confirm one, so a miss here is a regression.
    # Backfill defensively and persist the warning in 06c's validation warnings
    # for auditability. Tracker-sourced ongoing stories carry evidence dates
    # (first_seen/developments), not publication dates, so they are exempt.
    hygiene_warnings: list[str] = []
    for story in fresh:
        if not (story.get("date_confirmed") or "").strip():
            story["date_confirmed"] = (story.get("date_published") or "").strip()
            hygiene_warnings.append(
                "date_confirmed missing on curated fresh story; backfilled from "
                f"date_published ({story['date_confirmed'] or 'none'}): "
                f"{story.get('url', '?')}"
            )
    if hygiene_warnings:
        review_warnings.extend(hygiene_warnings)
    fresh.sort(key=priority_sort_key, reverse=True)
    for rank, item in enumerate(fresh, 1):
        item["rank"] = rank
    output = {
        "ranking_schema_version": RANKING_SCHEMA_VERSION,
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
                # Compare against the primary model, not _effective_model(MODEL):
                # a whole-run fallback sets MODEL_OVERRIDE, which made the
                # effective comparison self-consistent and hid the degradation
                # (digest-quality audit 2026-08-13).
                or proposal_model != MODEL
                or review_status != "reviewed"
            ),
        },
    }
    (run_dir / "06c-editorial-final.json").write_text(json.dumps({
        "proposal": final_proposal,
        "output": output,
        # Final validation warnings (proposal + critic review) so the shipped
        # selection's drops/caps are auditable from the final artifact
        # (digest-quality audit 2026-08-14: 06c omitted the source-concentration
        # warning entirely).
        "validation_warnings": proposal_warnings + review_warnings,
    }, indent=2))
    output_path.write_text(json.dumps(output, indent=2))
    elapsed = time.time() - started
    print(f"  [done] curate — {len(fresh)} fresh, {len(ongoing)} ongoing, "
          f"review={review_status} ({elapsed:.0f}s)")
    # Record canonical/related links from the selected stories so later topics
    # block the same event under a different URL (digest-quality audit 2026-08-26).
    _record_referenced_urls(topic, fresh, ongoing, run_dir)
    return fresh, updated_sif, ongoing


def _validate_standfirst(standfirst: str, stories: list[dict]) -> tuple[bool, str]:
    text = " ".join(standfirst.split()) if isinstance(standfirst, str) else ""
    if len(text) < 40:
        return False, "standfirst is too short"
    if len(text) > 900:
        return False, "standfirst exceeds 900 characters"
    if not re.search(r"""[.!?…]["'’”)]*$""", text):
        return False, "standfirst ends mid-sentence"
    if re.search(r"https?://|www\.", text, re.IGNORECASE):
        return False, "standfirst contains a URL"
    if "<" in text or ">" in text:
        return False, "standfirst contains HTML"
    if re.search(
        r"\b(?:today[’']s|this)\s+(?:digest|edition|briefing)\b|"
        r"\b(?:digest|edition)\s+(?:leads|focuses|covers|includes)\b|"
        r"\bread on\b|\balso in focus\b",
        text,
        re.IGNORECASE,
    ):
        return False, "standfirst uses digest-style meta language"
    source_text = " ".join(
        f"{story.get('title', '')} {story.get('summary', '')} "
        f"{story.get('why_still_relevant', '')}"
        for story in stories
    )
    source_numbers = set(re.findall(r"\b\d[\d,.]*%?\b", source_text))
    standfirst_numbers = set(re.findall(r"\b\d[\d,.]*%?\b", text))
    unsupported = sorted(standfirst_numbers - source_numbers)
    if unsupported:
        return False, f"standfirst introduced unsupported numbers: {', '.join(unsupported)}"
    entity_pattern = r"\b[A-Z][A-Za-z0-9’'-]{1,}\b"
    source_entities = {
        re.sub(r"[’']s$", "", entity.casefold())
        for entity in re.findall(entity_pattern, source_text)
    }
    standfirst_entities = {
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
    unsupported_entities = sorted(standfirst_entities - source_entities)
    if unsupported_entities:
        return False, (
            "standfirst introduced unsupported names: "
            + ", ".join(unsupported_entities)
        )
    return True, ""


def _first_complete_sentence(value: Any) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    if not text:
        return ""
    match = re.match(r"""^.*?[.!?…]["'’”)]*(?:\s|$)""", text)
    if match and len(match.group(0).strip()) <= 850:
        return match.group(0).strip()
    if len(text) <= 850:
        return text if re.search(r"""[.!?…]["'’”)]*$""", text) else f"{text}."
    return ""


def _fallback_standfirst(fresh: list[dict], ongoing: list[dict]) -> str:
    stories = fresh or ongoing
    if not stories:
        return "No publishable stories were selected for this section."
    sentences = [
        _first_complete_sentence(story.get("summary", ""))
        for story in stories[:3]
    ]
    sentences = [sentence for sentence in sentences if sentence]
    if not sentences:
        title = " ".join(str(stories[0].get("title", "Lead story")).split())
        return title if re.search(r"[.!?…]$", title) else f"{title}."
    standfirst = sentences[0]
    if len(sentences) > 1 and len(f"{standfirst} {sentences[1]}") <= 850:
        standfirst = f"{standfirst} {sentences[1]}"
    return standfirst

def _standfirst_story_fingerprint(stories: list[dict]) -> str:
    payload = [
        {
            "url": story.get("url", ""),
            "summary": story.get("summary", ""),
            "priority_score": story.get("priority_score"),
        }
        for story in stories
    ]
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _generate_section_standfirst(
    topic: dict,
    fresh: list[dict],
    ongoing: list[dict],
    run_dir: Path,
) -> str:
    """Generate newspaper copy only after selection and priority ranking."""
    artifact_path = run_dir / "07-standfirst.json"
    stories = fresh + ongoing
    story_fingerprint = _standfirst_story_fingerprint(stories)
    if artifact_path.exists():
        try:
            data = json.loads(artifact_path.read_text())
            cached = data.get("standfirst", "")
            valid, _ = _validate_standfirst(cached, stories)
            if (
                data.get("prompt_version") == STANDFIRST_PROMPT_VERSION
                and data.get("story_fingerprint") == story_fingerprint
                and valid
            ):
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    system = (
        "Write a concise two- or three-sentence newspaper standfirst for one section. "
        "Lead directly with the most consequential verified fact, then connect one or "
        "two supporting developments in natural newspaper prose. Use only facts, names, "
        "and numbers in the approved story data. Do not add URLs, HTML, new reporting, "
        "or claims about rejected stories. Never mention a digest, edition, briefing, "
        "candidate list, ranking, the writing process, or the reader. Do not use phrases "
        "such as 'today's digest leads with', 'also in focus', or 'read on'. Finish every "
        "sentence completely. Write in English and keep any supplied English story title "
        'unchanged. Output one JSON object: {"standfirst":"..."}'
    )
    user = (
        f"Newspaper section: {topic['web_title']}\n\n"
        f"Approved stories in priority order:\n{json.dumps(stories, indent=2)}"
    )
    errors: list[str] = []
    standfirst = ""
    model_used = ""
    status = "model"
    for requested_model, effective_model in _model_attempts(MODEL, MODEL_FALLBACK):
        try:
            raw = _call_llm_proxy(
                system, user, model=requested_model, timeout=INTRO_TIMEOUT
            )
            result = _extract_json(raw, f"section standfirst ({effective_model})")
            if not isinstance(result, dict):
                raise ValueError("standfirst output must be a JSON object")
            candidate = " ".join(str(result.get("standfirst", "")).split())
            valid, reason = _validate_standfirst(candidate, stories)
            if not valid:
                raise ValueError(reason)
            standfirst = candidate
            model_used = effective_model
            break
        except Exception as error:
            error_summary = _summarize_model_error(error)
            errors.append(f"{effective_model}: {error_summary}")
            print(
                f"  [7 retry] standfirst failed with {effective_model}: "
                f"{error_summary}"
            )
    if not standfirst:
        standfirst = _fallback_standfirst(fresh, ongoing)
        status = "deterministic_fallback"
    artifact_path.write_text(json.dumps({
        "prompt_version": STANDFIRST_PROMPT_VERSION,
        "story_fingerprint": story_fingerprint,
        "standfirst": standfirst,
        "status": status,
        "model": model_used,
        "errors": errors,
    }, indent=2, ensure_ascii=False) + "\n")
    return standfirst


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
    standfirst: str,
    *,
    notice: str = "",
) -> str:
    template = TEMPLATE_PATH.read_text()
    template = re.sub(
        r"\n<!--\nSTORY BLOCK TEMPLATE[\s\S]*?-->\s*$", "\n", template
    )
    standfirst_text = f"{notice} {standfirst}".strip()
    fresh_html = "\n".join(
        _render_story_block(story) for story in fresh
    ) or _empty_section_block("No fresh stories selected today.")
    ongoing_html = "\n".join(
        _render_story_block(story, ongoing=True) for story in ongoing
    ) or _empty_section_block("No developing or ongoing stories selected today.")
    replacements = {
        "{{DIGEST_TITLE}}": html.escape(str(topic["title"])),
        "{{DATE}}": html.escape(datetime.now().strftime("%B %d, %Y")),
        "{{INTRO}}": html.escape(standfirst_text),
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
    """Generate the approved standfirst, then render archival HTML deterministically."""
    output_path = run_dir / "digest.html"
    standfirst_path = run_dir / "07-standfirst.json"
    story_fingerprint = _standfirst_story_fingerprint(fresh + ongoing)
    if output_path.exists() and standfirst_path.exists():
        try:
            cached = json.loads(standfirst_path.read_text())
            valid, _ = _validate_standfirst(
                cached.get("standfirst", ""), fresh + ongoing
            )
            if (
                cached.get("prompt_version") == STANDFIRST_PROMPT_VERSION
                and cached.get("story_fingerprint") == story_fingerprint
                and valid
            ):
                print(f"  [skip] Phase 7 output exists: {output_path}")
                return output_path.read_text()
        except (json.JSONDecodeError, OSError):
            pass

    print(f"  [run ] write_html — {len(fresh)} fresh, {len(ongoing)} ongoing")
    started = time.time()
    standfirst = _generate_section_standfirst(topic, fresh, ongoing, run_dir)
    rendered = _render_digest_html(
        topic, fresh, ongoing, standfirst, notice=notice
    )
    output_path.write_text(rendered)
    elapsed = time.time() - started
    print(f"  [done] write_html — deterministic render, {len(rendered)} chars "
          f"({elapsed:.0f}s)")
    return rendered

def _public_story(story: dict, *, ongoing: bool = False) -> dict:
    """Return only source-backed fields safe to publish on the news site."""
    fields = (
        "title", "url", "source_domain", "date_published", "date_confirmed",
        "summary", "category", "editorial_significance", "significance_evidence",
        "significance_validation", "author", "event", "priority_score",
        "priority_explanation",
    )
    normalized = normalize_editorial_significance(copy.deepcopy(story))
    public = {
        key: normalized.get(key)
        for key in fields
        if normalized.get(key) is not None
    }
    attention = normalized.get("attention")
    if isinstance(attention, dict):
        public["attention"] = {
            key: attention.get(key)
            for key in (
                "schema_version", "provider", "status", "attention_now",
                "digest_prominence", "confidence", "age_bucket",
                "normalized_signals", "evidence",
            )
            if attention.get(key) is not None
        }
    if ongoing and normalized.get("why_still_relevant"):
        public["why_still_relevant"] = normalized["why_still_relevant"]
    return public


def phase_8_archive(
    topic: dict,
    rendered_html: str,
    stories_in_flight: dict,
    run_dir: Path,
    digest_dir: Path,
    fresh: list[dict] | None = None,
    ongoing: list[dict] | None = None,
    *,
    notice: str = "",
    archive_daily: bool = True,
) -> Path:
    """Archive the topic and write its stable, public publication artifact.

    No email is sent here. The all-topic publisher consumes publication.json
    after every category finishes, updates the site, and sends one summary email.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    fresh = fresh or []
    ongoing = ongoing or []

    curated_src = run_dir / "06-curated.json"
    if curated_src.exists():
        shutil.copy(curated_src, run_dir / "curated_copy.json")
    else:
        print("  [WARN] 06-curated.json missing — curated_copy.json not written")

    archive_path = (
        digest_dir / f"{today_str}.html"
        if archive_daily and not TEST_MODE
        else run_dir / "digest.html"
    )
    if archive_daily and not TEST_MODE:
        latest_html = digest_dir / ".daily_digest.html"
        latest_html.write_text(rendered_html)
        shutil.copy(latest_html, archive_path)
    else:
        archive_path.write_text(rendered_html)
    print(f"  [done] archived HTML → {archive_path}")

    standfirst_path = run_dir / "07-standfirst.json"
    standfirst = ""
    if standfirst_path.exists():
        try:
            standfirst = json.loads(standfirst_path.read_text()).get("standfirst", "")
        except (json.JSONDecodeError, OSError):
            standfirst = ""
    standfirst = standfirst or _fallback_standfirst(fresh, ongoing)
    publication = {
        "schema_version": 2,
        "ranking_schema_version": RANKING_SCHEMA_VERSION,
        "date": today_str,
        "slug": topic["web_slug"],
        "title": topic["web_title"],
        "source_category": topic["category"],
        "status": "degraded" if notice else ("published" if fresh or ongoing else "empty"),
        "notice": notice,
        "standfirst": standfirst,
        "fresh": [_public_story(story) for story in fresh],
        "ongoing": [_public_story(story, ongoing=True) for story in ongoing],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    publication_path = run_dir / "publication.json"
    publication_tmp = run_dir / ".publication.json.tmp"
    publication_tmp.write_text(json.dumps(publication, indent=2, ensure_ascii=False) + "\n")
    publication_tmp.replace(publication_path)
    print(f"  [done] publication artifact → {publication_path}")

    sif_path = digest_dir / "stories-in-flight.json"
    sif_path.write_text(json.dumps(stories_in_flight, indent=2))
    print("  [done] stories-in-flight updated")
    return publication_path



def phase_9_summary(topic: dict, fresh: list[dict], ongoing: list[dict],
                    run_dir: Path, digest_dir: Path) -> None:
    """Phase 9: Write the .md summary for future dedup.

    One LLM call, lightweight. Output is retained in the run and topic archives.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    output_path = run_dir / "summary.md"
    digest_md_path = digest_dir / f"{today_str}.md"
    publication_url = f"https://news.carter2099.com/{today_str}/{topic['web_slug']}/"
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
            f"**Published at:** {publication_url}\n\n"
            "## Fresh\n"
            "- No stories published in the last 24 hours.\n\n"
            "## Developing and Ongoing\n"
            "- No developing or ongoing stories reported.\n\n"
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
        "You are writing a concise markdown summary of today's published digest for "
        "archival and future deduplication. Write the entire summary in English and keep "
        "the supplied English story titles unchanged. Output ONLY the markdown, no "
        "explanations."
    )

    user = (
        f"Write a markdown summary of today's {topic['title']} in this exact format:\n\n"
        f"# {topic['title']} — {today_str}\n"
        f"**Published at:** {publication_url}\n\n"
        "## Fresh\n"
        "- [Story title](URL) — one-line summary\n"
        "- [Story title](URL) — one-line summary\n\n"
        "## Developing and Ongoing\n"
        "- [Story title](URL) — one-line summary (latest material development)\n\n"
        "## Coverage Gaps\n"
        "- Any notable stories or angles that were missed today\n\n"
        "IMPORTANT: Every story MUST include its URL as a markdown link `[title](URL)`. "
        "This is used by the dedup system in future runs. Never omit the URL.\n\n"
        f"## Fresh Stories Data\n\n{fresh_json}\n\n"
        f"## Developing and Ongoing Stories Data\n\n{ongoing_json}"
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
            f"**Published at:** {publication_url}",
            "",
            "## Fresh",
        ]
        for s in fresh[:10]:
            lines.append(f"- [{s.get('title', '?')}]({s.get('url', '#')}) — {s.get('summary', '')[:100]}")
        lines.append("")
        lines.append("## Developing and Ongoing")
        for s in ongoing[:5]:
            lines.append(f"- [{s.get('title', '?')}]({s.get('url', '#')}) — {s.get('summary', '')[:100]}")
        output_path.write_text("\n".join(lines) + "\n")

    if output_path.exists():
        shutil.copy(output_path, digest_md_path)


# ═══════════════════════════════════════════════════════════════════════════
# Stories-in-flight management
# ═══════════════════════════════════════════════════════════════════════════



def _prune_and_cool_stories(
    stories: list[dict],
    today: date | None = None,
) -> tuple[list[dict], int, int]:
    """Cool on evidence inactivity; prune only cooled, inactive stories."""
    if today is None:
        today = datetime.now(timezone.utc).date()
    kept: list[dict] = []
    auto_cooled = 0
    auto_pruned = 0

    for story in stories:
        _normalize_story_tracking(story, today)
        last_date = datetime.strptime(story["last_updated"], "%Y-%m-%d").date()
        inactive_age = (today - last_date).days
        status = story.get("status", "active")

        if status == "active" and inactive_age >= COOL_AFTER_DAYS:
            story["status"] = "cooled"
            status = "cooled"
            auto_cooled += 1

        # Active stories may run longer than seven days when real developments
        # continue. Only cooled stories with seven evidence-free days expire.
        if status == "cooled" and inactive_age >= PRUNE_AFTER_DAYS:
            auto_pruned += 1
            continue

        kept.append(story)

    return kept, auto_cooled, auto_pruned


def load_and_prune_stories_in_flight(digest_dir: Path) -> dict:
    """Load, migrate, cool, and prune the cross-day story tracker.

    Two deterministic rules:
    1. AUTO-COOL: active story with no evidence-backed development for
       COOL_AFTER_DAYS becomes cooled and leaves Developing and Ongoing.
    2. AUTO-PRUNE: cooled story with no evidence-backed development for
       PRUNE_AFTER_DAYS is removed. An actively developing story is not removed
       merely because its first report is old.

    A selected, source-linked fresh development can revive a cooled story.
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
        print(f"  Auto-cooled {auto_cooled} stale stories "
              f"(>= {COOL_AFTER_DAYS}d without evidence)")
    if auto_pruned > 0:
        print(f"  Auto-pruned {auto_pruned} cooled stories "
              f"(>= {PRUNE_AFTER_DAYS}d without evidence)")

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


def _archive_stub_attempt(run_dir: Path) -> None:
    """Preserve the failed attempt's phase artifacts instead of deleting them.

    Stub/fallback retries used to unlink every 0*-*.json in the run dir, so a
    fallback rerun left no JSON trail of the original failure. Move the
    attempt's artifacts into a timestamped stub-attempt-* subdirectory instead
    (digest-quality audit 2026-08-13).
    """
    archive = run_dir / f"stub-attempt-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
    archive.mkdir(exist_ok=True)
    for p in run_dir.glob("0*-*.json"):
        shutil.move(str(p), str(archive / p.name))
    print(f"  [retry] Preserved failed attempt artifacts → {archive.name}")


def _cleanup_stub_attempts(run_dir: Path) -> None:
    """Remove archived stub-attempt subdirectories after a fully successful run.

    The archived partial attempt is only useful while the run may fail; once
    the final run completes, keeping it makes the run dir look like a partial
    run and double-counts artifacts in audits (digest-quality audit 2026-08-24:
    both audit-window ai-tech days carried stub-attempt-* debris).
    """
    for child in run_dir.iterdir():
        if child.is_dir() and child.name.startswith("stub-attempt-"):
            shutil.rmtree(child)
            print(f"  [cleanup] Removed archived stub attempt: {child.name}")


def validate_runtime_contract() -> None:
    """Fail before research when a load-bearing pipeline symbol is missing."""
    errors: list[str] = []
    for name, minimum in (
        ("CROSS_DAY_DEDUP_DAYS", 1),
        ("REFERENCED_URLS_SCHEMA_VERSION", 1),
        ("RANKING_SCHEMA_VERSION", 1),
        ("ATTENTION_SCHEMA_VERSION", 1),
    ):
        value = globals().get(name)
        if not isinstance(value, int) or value < minimum:
            errors.append(f"{name} must be an integer >= {minimum} (got {value!r})")
    if len(TOPICS) != 5:
        errors.append(f"TOPICS must contain five sections (got {len(TOPICS)})")
    for key, config in TOPICS.items():
        missing = {
            field for field in ("category", "web_slug", "web_title", "research_angles")
            if field not in config
        }
        if missing:
            errors.append(f"{key} missing fields: {', '.join(sorted(missing))}")
    for name in (
        "_load_recent_covered_urls",
        "_load_cross_topic_urls",
        "phase_2_judge_research",
        "phase_2b_attention",
    ):
        if not callable(globals().get(name)):
            errors.append(f"{name} is missing or not callable")
    for name, path in (
        ("TEMPLATE_PATH", TEMPLATE_PATH),
        ("DIGEST_OMP_SANDBOX", DIGEST_OMP_SANDBOX),
        ("DIGEST_OMP_CONFIG", DIGEST_OMP_CONFIG),
    ):
        if not path.is_file():
            errors.append(f"{name} is missing or not a file: {path}")
    if DIGEST_OMP_CONFIG.is_file():
        if yaml is None:
            errors.append("PyYAML is required to validate DIGEST_OMP_CONFIG")
        else:
            try:
                digest_config = yaml.safe_load(DIGEST_OMP_CONFIG.read_text()) or {}
                provider_order = (
                    digest_config.get("providers", {}).get("webSearchOrder", [])
                )
                if provider_order[:2] != ["codex", "searxng"]:
                    errors.append(
                        "DIGEST_OMP_CONFIG providers.webSearchOrder must start "
                        "with ['codex', 'searxng']"
                    )
                searxng = digest_config.get("searxng", {})
                if searxng.get("endpoint") != SEARXNG_URL:
                    errors.append(
                        f"DIGEST_OMP_CONFIG searxng.endpoint must be {SEARXNG_URL}"
                    )
                categories = {
                    item.strip() for item in str(searxng.get("categories", "")).split(",")
                }
                if not {"general", "news"}.issubset(categories):
                    errors.append(
                        "DIGEST_OMP_CONFIG searxng.categories must include general and news"
                    )
                if searxng.get("language") not in (None, ""):
                    errors.append(
                        "DIGEST_OMP_CONFIG searxng.language must remain unset"
                    )
            except Exception as error:
                errors.append(f"DIGEST_OMP_CONFIG is invalid YAML: {error}")
    if errors:
        raise RuntimeError("Daily News preflight failed: " + "; ".join(errors))


# ═══════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ═══════════════════════════════════════════════════════════════════════════

def run_digest(category: str, dry_run: bool = False) -> None:
    """Run the full digest pipeline for a topic category.

    When TEST_MODE is set (via --test CLI flag), output goes to
    ~/digests/test/<topic>/<label>/. Production and test topic runs never send
    email; the all-topic publisher owns the single daily summary message.
    """
    global MODEL_OVERRIDE
    validate_runtime_contract()
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
        print(f"  *** TEST MODE — output isolated, no email ***")
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
                _archive_stub_attempt(run_dir)

            # Phase 1: Research
            t1 = _phase_start("Phase 1: Research")
            check_search_health("pre-phase1")
            phase_1_path = run_dir / "01-research-raw.json"
            if phase_1_path.exists():
                print(f"  [skip] Phase 1 output exists: {phase_1_path}")
                findings = json.loads(phase_1_path.read_text())
            else:
                findings = phase_1_research(topic, run_dir, stories_in_flight)
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
                    _archive_stub_attempt(run_dir)
                    findings = phase_1_research(
                        topic, run_dir, stories_in_flight
                    )
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

            # Phase 2b: Observable Attention
            t2b = _phase_start("Phase 2b: Observe Attention")
            if fresh_findings or ongoing_findings:
                fresh_findings, ongoing_findings = phase_2b_attention(
                    topic, fresh_findings, ongoing_findings, run_dir
                )
            _phase_done("Phase 2b: Observe Attention", t2b)

            # Phase 3: Rank URLs
            t3 = _phase_start("Phase 3: Rank URLs")
            if fresh_findings or ongoing_findings or any(
                    s.get("status") == "active" and _is_developing_story(s)
                    for s in stories_in_flight.get("stories", [])):
                # Run ranking without findings only when a qualified Developing
                # and Ongoing story can still flow into Phase 6.
                phase_4_queue, sif_candidates = phase_3_rank(
                    topic, fresh_findings, ongoing_findings, stories_in_flight, run_dir
                )
            else:
                phase_4_queue, sif_candidates = [], []
            _phase_done("Phase 3: Rank URLs", t3)

            # Phase 4: Fetch + Summarize
            t4 = _phase_start("Phase 4: Fetch & Summarize")
            summaries = (
                phase_4_fetch(topic, phase_4_queue, run_dir)
                if phase_4_queue else []
            )
            _phase_done("Phase 4: Fetch & Summarize", t4)

            # Phase 5: Judge Summaries
            t5 = _phase_start("Phase 5: Judge Summaries")
            judged = (
                phase_5_judge_summaries(topic, summaries, run_dir)
                if summaries else []
            )
            _phase_done("Phase 5: Judge Summaries", t5)

            # Phase 6: Curate
            t6 = _phase_start("Phase 6: Curate")
            if judged or sif_candidates:
                # Curation runs on SIF candidates alone (judged empty) so ongoing
                # stories from previous days are reported instead of a blank section.
                fresh, stories_in_flight, ongoing = phase_6_curate(
                    topic, judged, sif_candidates, stories_in_flight, run_dir
                )
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

        # Phase 7: Write archive HTML
        t7 = _phase_start("Phase 7: Write Archive HTML")
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
        _phase_done("Phase 7: Write Archive HTML", t7)

        # Phase 8: Archive + stable public artifact. Email is deliberately owned
        # by news_publish.py after every topic has completed.
        t8 = _phase_start("Phase 8: Archive & Publish Artifact")
        phase_8_archive(
            topic,
            html,
            stories_in_flight,
            run_dir,
            digest_dir,
            fresh=fresh,
            ongoing=ongoing,
            notice=notice,
            archive_daily=not dry_run,
        )
        _phase_done("Phase 8: Archive & Publish Artifact", t8)

        # Phase 9: Summary
        t9 = _phase_start("Phase 9: Summary")
        phase_9_summary(topic, fresh, ongoing, run_dir, digest_dir)
        _phase_done("Phase 9: Summary", t9)
        # The run completed successfully: drop the archived stub-attempt debris
        # from the failed first attempt so the run dir stays clean and audits
        # don't double-count partial runs (digest-quality audit 2026-08-24).
        _cleanup_stub_attempts(run_dir)

    except Exception as e:
        # Save retry state for cross-process exponential backoff
        if not TEST_MODE:
            try:
                state = json.loads(retry_state_path.read_text()) \
                    if retry_state_path.exists() else {}
                state["retry_count"] = state.get("retry_count", 0) + 1
                state["last_failure"] = datetime.now(timezone.utc).isoformat()
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
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    parser.add_argument("topic", nargs="?", choices=list(TOPICS) + ["all"],
                        help="Topic to run (or 'all' for every topic)")
    parser.add_argument("--preflight", action="store_true",
                        help="Validate load-bearing runtime contracts and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run without updating the top-level daily HTML archive")
    parser.add_argument("--test", action="store_true",
                        help="Test mode: isolate output in ~/digests/test/, copy prod SIF, write report")
    parser.add_argument("--model", type=str, default=None,
                        help="Override the LLM model (e.g. deepseek-v4-flash)")
    parser.add_argument("--test-label", type=str, default=None,
                        help="Label for test run directory (default: model name or 'test')")
    args = parser.parse_args()

    if args.preflight:
        validate_runtime_contract()
        print("Daily News preflight passed")
        raise SystemExit(0)
    if args.topic is None:
        parser.error("topic is required unless --preflight is used")

    # Set module-level globals for test mode
    if args.test:
        _configure_test_mode()
    if args.model:
        MODEL_OVERRIDE = args.model
    if args.test_label:
        TEST_LABEL = args.test_label
    elif args.model and args.test:
        TEST_LABEL = args.model

    if args.topic == "all":
        for cat in TOPICS:
            run_digest(cat, dry_run=args.dry_run)
    else:
        run_digest(args.topic, dry_run=args.dry_run)
