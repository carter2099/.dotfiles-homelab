#!/usr/bin/env python3
"""
Deterministic 9-phase email digest runner.

Each phase is a narrow, self-contained LLM call or Python step so the
local Qwen model can handle it reliably. Bounding caps prevent any one
topic from starving the others within the systemd timeout.

Architecture, stories-in-flight mechanics, and debugging:
  ~/notes/homelab/email-digests.md

Usage:
    python3 ~/scripts/digest_runner.py ai-tech
    python3 ~/scripts/digest_runner.py ai-tech --dry-run
    python3 ~/scripts/digest_runner.py all

Phases:
    1. Research        — pi -p web_search (3 angles, sequential)
    2. Judge Research  — batched LLM: Python date pre-tag + LLM quality filter
    3. Rank URLs       — Python: Pool A (fresh, capped 12) + Pool B (ongoing, capped 5)
                          + Pool C (stories-in-flight, capped 3, bypasses Phase 4)
    4. Fetch + Summarize — pi -p web_fetch (fresh first, then ongoing, ≤17 total)
    5. Judge Summaries — batched LLM: accuracy/fidelity check
    6. Curate          — 6a Python prep → 6b LLM editorial → 6c Python validate
    7. Write HTML      — one LLM call filling template
    8. Send + Archive  — pure Python
    9. Summary         — one lightweight LLM call
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

# ── Batching ───────────────────────────────────────────────────────────────
BATCH_SIZE = 10  # findings/summaries per LLM call in phases 2 and 5

# ── Caps ───────────────────────────────────────────────────────────────────
FRESH_CAP = 12      # Pool A: max fresh findings passed to Phase 4
ONGOING_CAP = 5     # Pool B: max ongoing articles passed to Phase 4
SIF_CAP = 3         # Pool C: max stories-in-flight passed directly to Phase 6

# ── Stories-in-flight constants ────────────────────────────────────────────
COOL_AFTER_DAYS = 5     # auto-set status to "cooled" if no updates in 5 days
PRUNE_AFTER_DAYS = 10   # remove cooled stories entirely after 10 days total


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
            "For each finding, evaluate against these rules and assign a verdict.\n\n"
            "1. SOURCE CHECK: Is this from a known reputable outlet? TechCrunch, The Verge, "
            "Ars Technica, Wired, ZDNet, VentureBeat, Hacker News, official company blogs, "
            "GitHub repos with significant activity, academic papers on arxiv. Personal blogs "
            "are OK if they have substance. Content farms, SEO spam, and low-quality "
            "aggregators should be dropped with reason 'unreliable_source'.\n"
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
    model: str = MODEL_REASONING,
    temperature: float = 0.3,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Call the local Qwen model via llm-proxy. Returns response text."""
    date_prefix = _date_context()
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": f"{date_prefix}\n\n{system}"},
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
    cmd = ["pi", "-p", "--provider", "local-llm", "--model", model,
           "--session-dir", str(Path.home() / ".pi/agent/sessions-automated")]
    date_prefix = _date_context()
    full_system = f"{date_prefix}\n\n{append_system}" if append_system else date_prefix
    cmd.extend(["--append-system-prompt", full_system])

    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "HOME": str(Path.home())},
    )
    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(f"pi -p failed (rc={result.returncode}): {result.stderr[:500]}")
    return result.stdout


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

    raise ValueError(f"Could not extract JSON from {label}. Raw text (first 500 chars):\n{text[:500]}")


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
    """Phase 1: Parallel research agents via pi -p.

    Each research angle gets its own pi -p call. They use web_search and
    web_fetch to find stories. Returns merged list of findings.
    """
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


def phase_2_judge_research(topic: dict, findings: list[dict], run_dir: Path) -> tuple[list[dict], list[dict]]:
    """Phase 2: Python date pre-tagging + batched LLM judge.

    1. Python parses date_published → tags each finding as fresh, ongoing, or too_old.
       too_old findings are dropped without touching the LLM.
    2. Findings are split into batches of BATCH_SIZE.
    3. Each batch gets one LLM call with the topic's judgment rules + importance rubric.
    4. Python merges batch results, handling cross-batch duplicates.

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
    # Use calendar-date comparison (not exact hours) because most article
    # dates are date-only strings with no time component. A story dated
    # "yesterday" could have been published at 23:59 — treating it as fresh
    # is more conservative than assuming 00:00 and calling it ongoing.
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
            f"## Rules\n\n{topic['judgment_rules']}\n\n"
            f"## Importance Rubric\n\n{rubric}\n\n"
            f"## Findings to evaluate (batch {batch_idx + 1}/{len(batches)})\n\n"
            f"{batch_json}\n\n"
            "Evaluate each finding against every rule. Output the approved and "
            "rejected arrays in ```json fences. Include a clear reason for each rejection."
        )

        try:
            raw = _call_llm_proxy(system, user, model=MODEL_REASONING)
            result = _extract_json(raw, f"judge_research batch {batch_idx + 1}")
            batch_approved = result.get("approved", [])
            batch_rejected = result.get("rejected", [])
            all_approved.extend(batch_approved)
            all_rejected.extend(batch_rejected)
            print(f"  Batch {batch_idx + 1}: {len(batch_approved)} approved, {len(batch_rejected)} rejected")
        except Exception as e:
            print(f"  [FAIL] judge_research batch {batch_idx + 1} — {e}, treating all as approved")
            all_approved.extend(batch)

    # ── Step 3: Python merge — cross-batch duplicate detection ──
    seen_urls: set[str] = set()
    deduped_approved: list[dict] = []
    dedup_rejected: list[dict] = []

    for f in all_approved:
        url = f.get("url", "").strip().rstrip("/").lower()
        if url and url in seen_urls:
            dedup_rejected.append({"finding": f, "reason": "cross_batch_duplicate"})
        else:
            if url:
                seen_urls.add(url)
            deduped_approved.append(f)

    if dedup_rejected:
        print(f"  Cross-batch dedup: removed {len(dedup_rejected)} duplicates")

    # Split by date_tag
    fresh = [f for f in deduped_approved if f.get("date_tag") == "fresh"]
    ongoing = [f for f in deduped_approved if f.get("date_tag") == "ongoing"]

    elapsed = time.time() - t0
    print(f"  [done] judge_research — {len(fresh)} fresh, {len(ongoing)} ongoing, "
          f"{len(all_rejected) + len(dedup_rejected)} rejected ({elapsed:.0f}s)")
    for r in all_rejected[:5]:
        finding = r.get("finding", {})
        reason = r.get("reason", "unspecified")
        print(f"    ✗ {finding.get('title', '?')[:60]}: {reason}")
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

    # ── Pool A: Fresh findings ──
    # Sort strategy: stable two-pass. Primary: importance (high→med→low).
    # Tiebreaker: date_published recency (newer first).
    pool_a = sorted(fresh, key=lambda f: f.get("date_published", ""), reverse=True)
    pool_a = sorted(pool_a, key=lambda f: importance_order.get(f.get("importance", "low"), 2))
    pool_a = pool_a[:FRESH_CAP]

    # ── Pool B: Ongoing articles ──
    # Sort strategy: stable two-pass. Primary: date_published recency (newer first).
    # Tiebreaker: importance (high→med→low).
    pool_b = sorted(ongoing, key=lambda f: importance_order.get(f.get("importance", "low"), 2))
    pool_b = sorted(pool_b, key=lambda f: f.get("date_published", ""), reverse=True)
    pool_b = pool_b[:ONGOING_CAP]

    # ── Pool C: Stories-in-flight (bypasses Phase 4) ──
    active_sif = [s for s in stories_in_flight.get("stories", [])
                  if s.get("status") == "active"]
    # Sort by last_updated descending
    pool_c = sorted(active_sif, key=lambda s: s.get("last_updated", ""), reverse=True)[:SIF_CAP]

    # Phase 4 fetch queue: Pool A (fresh first) + Pool B
    phase_4_queue = pool_a + pool_b

    output = {
        "phase_4_queue": phase_4_queue,
        "sif_candidates": pool_c,
        "pool_a": pool_a,
        "pool_b": pool_b,
    }
    output_path.write_text(json.dumps(output, indent=2))
    print(f"  Phase 3 done: Pool A={len(pool_a)} fresh, Pool B={len(pool_b)} ongoing, "
          f"Pool C={len(pool_c)} SIF (bypass Phase 4) → {len(phase_4_queue)} total for fetch")
    return phase_4_queue, pool_c


def phase_4_fetch(topic: dict, findings: list[dict], run_dir: Path) -> list[dict]:
    """Phase 4: Fetch each article and write detailed summaries.

    Fresh stories fetch first (already ordered by Phase 3), then ongoing articles.
    Total bounded by Phase 3 caps: ≤17 articles (12 fresh + 5 ongoing).
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
        source = finding.get("source_verdict", "?")
        print(f"  [run ] [{source}] {label}")
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
    # Findings are already ordered fresh-first by Phase 3
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_FETCH) as pool:
        futures = {pool.submit(_fetch_one, f): f for f in findings}
        for future in as_completed(futures):
            results.append(future.result())

    # Preserve original order (fresh first, then ongoing)
    url_order = {f.get("url"): i for i, f in enumerate(findings)}
    results.sort(key=lambda r: url_order.get(r.get("url"), 999))

    output_path.write_text(json.dumps(results, indent=2))
    successful = sum(1 for r in results if r.get("fetch_success", True))
    print(f"  Phase 4 done: {successful}/{len(results)} fetches successful")
    return results


def phase_5_judge_summaries(topic: dict, summaries: list[dict], run_dir: Path) -> list[dict]:
    """Phase 5: Batched LLM judge of summary accuracy and faithfulness.

    Splits summaries into batches of BATCH_SIZE (8-10) per LLM call.
    Python merges results.
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

    batches = _batch(to_judge, BATCH_SIZE)
    print(f"  Batched into {len(batches)} LLM call(s) ({BATCH_SIZE}/batch)")

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
            raw = _call_llm_proxy(system, user, model=MODEL_REASONING)
            judgments = _extract_json(raw, f"judge_summaries batch {batch_idx + 1}")
            if not isinstance(judgments, list):
                judgments = [judgments]
            all_judgments.extend(judgments)
            print(f"  Batch {batch_idx + 1}: {len(judgments)} judgments received")
        except Exception as e:
            print(f"  [FAIL] judge_summaries batch {batch_idx + 1} — {e}, keeping all in batch")
            for s in batch:
                all_judgments.append({"url": s.get("url", ""), "verdict": "keep", "issues": [], "fixed_summary": ""})

    # Apply judgments
    judged_map = {j.get("url", ""): j for j in all_judgments}
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

    output_path.write_text(json.dumps(results, indent=2))
    return results


def phase_6_curate(topic: dict, summaries: list[dict], sif_candidates: list[dict],
                   stories_in_flight: dict, run_dir: Path) -> tuple[list[dict], dict, list[dict]]:
    """Phase 6: Curate — split into 6a (Python prep), 6b (LLM editorial), 6c (Python validate).

    Returns (fresh_stories, updated_stories_in_flight, ongoing_stories).
    """
    output_path = run_dir / "06-curated.json"
    if output_path.exists():
        print(f"  [skip] Phase 6 output exists: {output_path}")
        data = json.loads(output_path.read_text())
        return data["fresh"], data.get("stories_in_flight", stories_in_flight), data["ongoing"]

    kept = [s for s in summaries if s.get("judge_verdict") in ("keep", "fix")]
    dropped = [s for s in summaries if s.get("judge_verdict") == "drop"]

    # ── 6a: Python prep ──
    fresh_candidates = [s for s in kept if s.get("source_verdict") == "fresh"]
    ongoing_candidates = [s for s in kept if s.get("source_verdict") == "ongoing"]

    # Deduplicate by URL within each pool
    def _dedup_by_url(items: list[dict]) -> list[dict]:
        seen: set[str] = set()
        result: list[dict] = []
        for item in items:
            url = item.get("url", "").strip().rstrip("/").lower()
            if url and url not in seen:
                seen.add(url)
                result.append(item)
        return result

    fresh_candidates = _dedup_by_url(fresh_candidates)
    ongoing_candidates = _dedup_by_url(ongoing_candidates)

    # Pre-rank by importance + recency, cap to top 15 for the LLM
    importance_order = {"high": 0, "medium": 1, "low": 2}
    ranked_candidates = sorted(
        fresh_candidates + ongoing_candidates,
        key=lambda s: (
            importance_order.get(s.get("importance", "low"), 2),
            s.get("date_published", "9999-99-99"),
        )
    )[:15]

    print(f"  [6a prep] {len(fresh_candidates)} fresh, {len(ongoing_candidates)} ongoing, "
          f"{len(sif_candidates)} SIF → {len(ranked_candidates)} capped for LLM")

    # ── 6b: LLM editorial ──
    t0 = time.time()
    rubric = _importance_rubric_text(topic)

    candidates_json = json.dumps(ranked_candidates, indent=2)
    sif_json = json.dumps(sif_candidates, indent=2)
    tracker_json = json.dumps(stories_in_flight, indent=2)

    system = (
        "You are the lead editor of a daily news digest. You receive vetted article "
        "summaries and a 'stories-in-flight' tracker of evolving stories from previous "
        "days. Your job is to curate the final story selection.\n\n"
        "Tasks:\n"
        "1. CROSS-REFERENCE: Identify connections between fresh articles and stories "
        "already in the tracker. If today's findings add meaningful new developments "
        "to a tracker story, update its 'latest_dev' and set 'last_updated' to today's "
        "date (YYYY-MM-DD) — this resets the auto-cool clock.\n"
        "2. UPDATE TRACKER: Adjust 'importance' on tracker entries as stories evolve "
        "(e.g. diplomatic spat → military confrontation → escalate to high; resolved "
        "story → cool to low). You may set status to 'cooled' if a story has definitively "
        "resolved (e.g. bill signed, trial verdict, product shipped). Otherwise leave "
        "status as-is — the system auto-cools stories with no updates after 5 days and "
        "auto-prunes cooled stories after 10 days total.\n"
        "3. ADD NEW TRACKER ENTRIES: Major announcements, unfolding events, controversies, "
        "multi-day stories should be added to the tracker. Each needs: title, url, first_seen "
        "(today), last_updated (today), latest_dev (1-sentence summary of what's new), "
        "status: 'active', importance: high/medium/low (default medium), category.\n"
        "4. FLAG GAPS: What important story might be missing? Add a 'gaps' note.\n"
        "5. WRITE INTRO HOOK: 2-3 sentence editorial intro setting the tone.\n"
        "6. SELECT FINAL LINEUP: 5-7 fresh stories + 2-3 ongoing from the stories-in-flight "
        "tracker (NOT from the candidates — those are separate). The ongoing stories should "
        "be DIFFERENT from the fresh stories — they are ongoing narratives, not today's headlines.\n"
        "CRITICAL: Candidates have a 'source_verdict' field ('fresh' = today/yesterday, "
        "'ongoing' = 2-5 days old). Prefer 'source_verdict: fresh' candidates for the "
        "Fresh section. Only use 'source_verdict: ongoing' candidates if there aren't "
        "enough fresh ones — and even then, drop the oldest ones first.\n\n"
        "Output a JSON object wrapped in ```json fences with this structure:\n"
        '  {\n'
        '    "fresh": [\n'
        '      {"rank": 1, "title": "...", "url": "...", "category": "...",\n'
        '       "summary": "2-3 sentence editorial summary", "related_to": null|1,\n'
        '       "related_urls": ["..."]},\n'
        '      ...\n'
        '    ],\n'
        '    "ongoing": [\n'
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
        "IMPORTANT: Ongoing stories must use URLs from the stories-in-flight tracker, "
        "NOT from today's candidates. Ongoing = narratives from earlier days. "
        "Fresh = today's news. Do not put the same story in both sections."
    )

    dropped_json = json.dumps(
        [{"title": d.get("title"), "url": d.get("url"),
          "reason": d.get("judge_issues", [])} for d in dropped],
        indent=2)

    user = (
        f"## Fresh/Ongoing Candidates (for the Fresh section)\n\n{candidates_json}\n\n"
        f"## Stories In Flight — Active Candidates (for Ongoing section)\n\n{sif_json}\n\n"
        f"## Full Stories In Flight Tracker (update this)\n\n{tracker_json}\n\n"
        f"## Importance Rubric\n\n{rubric}\n\n"
        f"## Dropped Summaries (for reference, do not include)\n\n"
        f"{dropped_json}\n\n"
        "Curate the final selection. Fresh candidates go in the Fresh section. "
        "Stories-in-flight go in the Ongoing section. "
        "Update the tracker with new developments and new entries. "
        "Output the JSON object in ```json fences."
    )

    try:
        raw = _call_llm_proxy(system, user, model=MODEL_REASONING)
        result = _extract_json(raw, "curate output")

        # ── 6c: Python validate ──
        fresh = result.get("fresh", kept[:7])
        ongoing = result.get("ongoing", [])
        updated_sif = result.get("stories_in_flight", stories_in_flight)
        gaps = result.get("gaps", "")
        intro = result.get("intro_hook", "")

        # Validate URLs against source data — build the set of known URLs
        known_urls: set[str] = set()
        for c in ranked_candidates:
            u = c.get("url", "").strip().rstrip("/").lower()
            if u:
                known_urls.add(u)
        for s in sif_candidates:
            u = s.get("url", "").strip().rstrip("/").lower()
            if u:
                known_urls.add(u)

        # Check fresh stories' URLs
        validated_fresh = []
        for f in fresh:
            url = f.get("url", "").strip().rstrip("/").lower()
            if url in known_urls or not url:
                validated_fresh.append(f)
            else:
                print(f"  [6c validate] Dropped hallucinated URL from fresh: {f.get('title', '?')[:60]}")

        # Check ongoing stories' URLs
        validated_ongoing = []
        for o in ongoing:
            url = o.get("url", "").strip().rstrip("/").lower()
            if url in known_urls or not url:
                validated_ongoing.append(o)
            else:
                print(f"  [6c validate] Dropped hallucinated URL from ongoing: {o.get('title', '?')[:60]}")

        fresh = validated_fresh or fresh  # fall back to unvalidated if all were dropped
        ongoing = validated_ongoing or ongoing

        elapsed = time.time() - t0
        print(f"  [done] curate — {len(fresh)} fresh, {len(ongoing)} ongoing ({elapsed:.0f}s)")
        if gaps:
            print(f"    Gaps: {gaps[:200]}")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"  [FAIL] curate — {e} ({elapsed:.0f}s), using raw summaries")
        fresh = kept[:7]
        ongoing = []
        updated_sif = stories_in_flight
        intro = ""
        gaps = ""

    output = {
        "fresh": fresh,
        "ongoing": ongoing,
        "stories_in_flight": updated_sif,
        "intro_hook": intro,
        "gaps": gaps,
    }
    output_path.write_text(json.dumps(output, indent=2))
    return fresh, updated_sif, ongoing


def phase_7_write(topic: dict, fresh: list[dict], ongoing: list[dict],
                  intro_hook: str, run_dir: Path) -> str:
    """Phase 7: Write the HTML email.

    One LLM call fills the HTML template with curated stories.
    """
    output_path = run_dir / "digest.html"
    if output_path.exists():
        print(f"  [skip] Phase 7 output exists: {output_path}")
        return output_path.read_text()

    print(f"  [run ] write_html — {len(fresh)} fresh, {len(ongoing)} ongoing")
    t0 = time.time()

    template = TEMPLATE_PATH.read_text()
    today_str = datetime.now().strftime("%B %d, %Y")

    # Pre-fill the template header
    html = template.replace("{{DIGEST_TITLE}}", topic["title"])
    html = html.replace("{{DATE}}", today_str)

    curated_json = json.dumps({"fresh": fresh, "ongoing": ongoing}, indent=2)

    system = (
        "You are an HTML email writer for a daily digest. You receive curated story "
        "data and an HTML template with placeholders. Fill in the placeholders and "
        "output the complete HTML.\n\n"
        "CRITICAL RULES:\n"
        "- Every story link must use the exact URL provided — do not alter, guess, or construct URLs.\n"
        "- Use the EXACT story block HTML from the template comments for each story.\n"
        "- For Ongoing stories, include the WHY line variant with the purple italic text.\n"
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
        f"## HTML Template (fill {{INTRO}}, {{FRESH_STORIES}}, {{ONGOING_STORIES}})\n\n{html}\n\n"
        "Fill the placeholders with the curated stories. Output the complete HTML document."
    )

    try:
        raw = _call_llm_proxy(system, user, model=MODEL_REASONING)
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

    # Idempotent resume: if today's archive exists, this phase already ran —
    # don't re-send the email (every other phase skips on existing output).
    archive_path = digest_dir / f"{today_str}.html"
    if archive_path.exists():
        print(f"  [skip] Phase 8 output exists: {archive_path}")
        return

    temp_html = digest_dir / ".daily_digest.html"
    temp_html.write_text(html)

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

    shutil.copy(temp_html, archive_path)
    print(f"  [done] archived HTML → {archive_path}")

    sif_path = digest_dir / "stories-in-flight.json"
    sif_path.write_text(json.dumps(stories_in_flight, indent=2))
    print(f"  [done] stories-in-flight updated")

    curated_src = run_dir / "06-curated.json"
    if curated_src.exists():
        shutil.copy(curated_src, run_dir / "curated_copy.json")


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
        raw = _call_llm_proxy(system, user, model=MODEL_REASONING)
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


def load_and_prune_stories_in_flight(digest_dir: Path) -> dict:
    """Load the cross-day story tracker and apply deterministic pruning.

    Two rules (Python-side, not LLM-dependent):
    1. AUTO-COOL: Any story with status "active" and last_updated older than
       COOL_AFTER_DAYS → set status to "cooled". Removes from Ongoing pool.
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
        # Ensure importance field exists (schema migration)
        _ensure_importance(s)

        last_str = s.get("last_updated", "")
        try:
            last_date = datetime.strptime(last_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
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

        # Phase 2: Judge Research (batched + date pre-tag)
        print("\n── Phase 2: Judge Research ──")
        if findings:
            fresh_findings, ongoing_findings = phase_2_judge_research(topic, findings, run_dir)
        else:
            fresh_findings, ongoing_findings = [], []

        # Phase 3: Rank URLs (Pools A/B/C with caps)
        print("\n── Phase 3: Rank URLs ──")
        if fresh_findings or ongoing_findings:
            phase_4_queue, sif_candidates = phase_3_rank(
                topic, fresh_findings, ongoing_findings, stories_in_flight, run_dir)
        else:
            phase_4_queue, sif_candidates = [], []

        # Phase 4: Fetch + Summarize (fresh first, then ongoing, ≤17 total)
        print("\n── Phase 4: Fetch & Summarize ──")
        if phase_4_queue:
            summaries = phase_4_fetch(topic, phase_4_queue, run_dir)
        else:
            summaries = []

        # Phase 5: Judge Summaries (batched)
        print("\n── Phase 5: Judge Summaries ──")
        if summaries:
            judged = phase_5_judge_summaries(topic, summaries, run_dir)
        else:
            judged = []

        # Phase 6: Curate (6a prep → 6b LLM → 6c validate)
        print("\n── Phase 6: Curate ──")
        if judged:
            fresh, stories_in_flight, ongoing = phase_6_curate(
                topic, judged, sif_candidates, stories_in_flight, run_dir)
        else:
            fresh, ongoing = [], []

        # Phase 7: Write
        print("\n── Phase 7: Write HTML ──")
        curated_data = json.loads((run_dir / "06-curated.json").read_text()) \
            if (run_dir / "06-curated.json").exists() else {}
        intro_hook = curated_data.get("intro_hook", "")
        if fresh:
            html = phase_7_write(topic, fresh, ongoing, intro_hook, run_dir)
        else:
            html = (
                f'<html><body><h1>{topic["title"]}</h1>'
                f'<p>{today_str}</p><p>No stories found today.</p></body></html>'
            )
            (run_dir / "digest.html").write_text(html)

        # Phase 8: Send + Archive
        print("\n── Phase 8: Send & Archive ──")
        if dry_run:
            print("  [skip] DRY RUN — skipping email send")
            today_str = datetime.now().strftime("%Y-%m-%d")
            archive_path = digest_dir / f"{today_str}.html"
            shutil.copy(run_dir / "digest.html", archive_path)
            print(f"  [done] archived HTML → {archive_path}")
            sif_path = digest_dir / "stories-in-flight.json"
            sif_path.write_text(json.dumps(stories_in_flight, indent=2))
            print(f"  [done] stories-in-flight updated")
        else:
            phase_8_send_archive(topic, html, stories_in_flight, run_dir, digest_dir)

        # Phase 9: Summary
        print("\n── Phase 9: Summary ──")
        phase_9_summary(topic, fresh, ongoing, run_dir, digest_dir)

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
