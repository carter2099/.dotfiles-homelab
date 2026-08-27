#!/usr/bin/env python3
"""Focused behavioral fixtures for digest dedup, cache, editorial, and rendering."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import digest_runner as digest  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_url_normalization() -> None:
    normalized = digest._normalize_url(
        "HTTPS://www.Example.com/Case-Sensitive/?utm_source=x&b=2&a=1#fragment"
    )
    check(normalized == "example.com/Case-Sensitive?a=1&b=2", normalized)


def test_article_cache_contract() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        cache_dir = Path(temporary)
        now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
        result = {
            "title": "Cached title",
            "url": "https://example.com/story",
            "summary": "Cached factual summary.",
            "fetch_success": True,
        }
        digest._save_article_cache(
            "https://example.com/story?utm_source=test",
            result,
            model=digest.MODEL,
            cache_dir=cache_dir,
            now=now,
        )
        hit = digest._load_article_cache(
            "https://www.example.com/story",
            model=digest.MODEL,
            cache_dir=cache_dir,
            now=now + timedelta(hours=1),
        )
        check(hit == result, f"cache hit={hit!r}")
        wrong_model = digest._load_article_cache(
            "https://example.com/story",
            model=digest.MODEL_FALLBACK,
            cache_dir=cache_dir,
            now=now + timedelta(hours=1),
        )
        check(wrong_model is None, "cache crossed model contract")
        stale = digest._load_article_cache(
            "https://example.com/story",
            model=digest.MODEL,
            cache_dir=cache_dir,
            now=now + timedelta(hours=25),
        )
        check(stale is None, "stale cache entry was reused")
        removed = digest._prune_article_cache(
            cache_dir=cache_dir, now=now + timedelta(hours=25)
        )
        check(removed == 1, f"expired cache entries removed={removed}")
        check(not list(cache_dir.glob("*.json")), "expired cache file remained")


def test_cross_topic_dedup_precedes_fetch_queue() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_dir = root / "ai-tech" / "2026-08-10"
        run_dir.mkdir(parents=True)
        other_category = digest.TOPICS["gaming"]["category"]
        other_dir = root / other_category / "2026-08-10"
        other_dir.mkdir(parents=True)
        duplicate = "https://example.com/shared?utm_source=gaming"
        (other_dir / "06-curated.json").write_text(json.dumps({
            "fresh": [{"url": duplicate}],
            "ongoing": [],
        }))
        fresh = [
            {
                "title": "Duplicate",
                "url": "https://www.example.com/shared",
                "editorial_significance": "high",
                "date_published": "2026-08-10",
            },
            {
                "title": "Unique",
                "url": "https://example.com/unique",
                "editorial_significance": "medium",
                "date_published": "2026-08-10",
            },
        ]
        with patch.object(digest, "DIGESTS_DIR", root):
            queue, _ = digest.phase_3_rank(
                digest.TOPICS["ai-tech"], fresh, [], {"stories": []}, run_dir
            )
        check([item["title"] for item in queue] == ["Unique"], f"queue={queue!r}")
        artifact = json.loads((run_dir / "03-urls-ranked.json").read_text())
        check(len(artifact["cross_topic_rejected"]) == 1, "skip was not audited")


def test_cross_topic_same_event_referenced_url_dedup() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_dir = root / "ai-tech" / "2026-08-26"
        run_dir.mkdir(parents=True)
        other = root / digest.TOPICS["gaming"]["category"] / "2026-08-26"
        other.mkdir(parents=True)
        (other / "06-curated.json").write_text(json.dumps({
            "fresh": [{
                "url": "https://techcrunch.com/2026/08/25/openai-jalapeno-chip",
            }],
            "ongoing": [],
        }))
        (other / "referenced-urls.json").write_text(json.dumps({
            "schema_version": digest.REFERENCED_URLS_SCHEMA_VERSION,
            "generated_at": "2026-08-27T00:00:00+00:00",
            "stories": [{
                "url": "https://techcrunch.com/2026/08/25/openai-jalapeno-chip",
                "referenced_urls": [
                    "https://openai.com/index/jalapeno-inference-chip/",
                ],
            }],
        }))
        fresh = [
            {
                "title": "Same event via source page",
                "url": "https://openai.com/index/jalapeno-inference-chip/",
                "editorial_significance": "high",
                "date_published": "2026-08-25",
            },
            {
                "title": "Unique story",
                "url": "https://example.com/unique",
                "editorial_significance": "medium",
                "date_published": "2026-08-25",
            },
        ]
        with patch.object(digest, "DIGESTS_DIR", root):
            queue, _ = digest.phase_3_rank(
                digest.TOPICS["ai-tech"], fresh, [], {"stories": []}, run_dir
            )
        check(
            [item["title"] for item in queue] == ["Unique story"],
            f"queue={queue!r}",
        )
        artifact = json.loads((run_dir / "03-urls-ranked.json").read_text())
        check(
            len(artifact["cross_topic_rejected"]) == 1,
            "same-event source-page URL not blocked",
        )


def test_phase_two_cross_day_dedup_window_contract() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        today = datetime.now(timezone.utc).date().isoformat()
        run_dir = root / "ai-tech" / today
        run_dir.mkdir(parents=True)
        finding = {
            "title": "Fresh verified event",
            "url": "https://example.com/fresh-event",
            "source_domain": "example.com",
            "date_published": today,
            "summary": "A source-grounded event occurred today.",
            "category": "Research",
            "editorial_significance": "medium",
            "event": "Fresh verified event occurs",
            "event_terms": ["Fresh verified", "event occurs"],
        }
        judged = {
            "approved": [finding],
            "rejected": [],
        }
        with patch(
            "digest_runner._call_llm_proxy",
            return_value=json.dumps(judged),
        ):
            fresh, ongoing = digest.phase_2_judge_research(
                digest.TOPICS["ai-tech"],
                [finding],
                run_dir,
                {"stories": []},
            )
        check(digest.CROSS_DAY_DEDUP_DAYS == 5, digest.CROSS_DAY_DEDUP_DAYS)
        check(len(fresh) == 1 and not ongoing, (fresh, ongoing))


def test_runtime_preflight_fails_closed_on_missing_symbol() -> None:
    digest.validate_runtime_contract()
    with patch.object(digest, "CROSS_DAY_DEDUP_DAYS", None):
        raised = False
        try:
            digest.validate_runtime_contract()
        except RuntimeError as error:
            raised = "CROSS_DAY_DEDUP_DAYS" in str(error)
        check(raised, "preflight accepted a missing cross-day dedup contract")


def test_attention_phase_persists_durable_observations() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_dir = root / "run" / "2026-08-25"
        run_dir.mkdir(parents=True)
        fresh = [{
            "title": "Observed event",
            "url": "https://example.com/observed",
            "editorial_significance": "medium",
        }]
        ongoing = [{
            "title": "Older event",
            "url": "https://example.com/older",
            "editorial_significance": "high",
        }]
        scored = [{
            **fresh[0],
            "priority_score": 82.0,
            "priority_explanation": "Observed coverage breakout.",
            "attention": {"status": "ok", "confidence": 0.8},
        }]
        artifact = {
            "schema_version": 1,
            "provider": "GDELT DOC 2.0",
            "observed_at": "2026-08-25T12:00:00+00:00",
            "requests": 1,
            "cache_hits": 0,
            "available": 1,
            "unavailable": 0,
            "observations": [],
        }
        with (
            patch.object(digest, "ATTENTION_CACHE_DIR", root / "cache"),
            patch.object(digest, "ATTENTION_ARCHIVE_DIR", root / "attention"),
            patch("digest_runner.score_attention", return_value=(scored, artifact)),
        ):
            scored_fresh, scored_ongoing = digest.phase_2b_attention(
                digest.TOPICS["ai-tech"], fresh, ongoing, run_dir
            )
        check(scored_fresh[0]["priority_score"] == 82.0, scored_fresh)
        check(scored_ongoing[0]["priority_score"] == 100.0, scored_ongoing)
        check((run_dir / "02b-attention.json").exists(), "run attention artifact missing")
        durable = root / "attention" / "2026-08-25" / "ai-tech.json"
        check(durable.exists(), "durable attention observation missing")


def test_phase_three_uses_product_priority() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_dir = root / "ai-tech" / "2026-08-25"
        run_dir.mkdir(parents=True)
        fresh = [
            {
                "title": "High consequence, quieter coverage",
                "url": "https://example.com/consequence",
                "editorial_significance": "high",
                "priority_score": 76.0,
                "date_published": "2026-08-25",
            },
            {
                "title": "Medium consequence, attention breakout",
                "url": "https://example.com/breakout",
                "editorial_significance": "medium",
                "priority_score": 89.0,
                "date_published": "2026-08-25",
            },
        ]
        with patch.object(digest, "DIGESTS_DIR", root):
            queue, _ = digest.phase_3_rank(
                digest.TOPICS["ai-tech"], fresh, [], {"stories": []}, run_dir
            )
        check(
            [item["title"] for item in queue]
            == [
                "Medium consequence, attention breakout",
                "High consequence, quieter coverage",
            ],
            queue,
        )
        artifact = json.loads((run_dir / "03-urls-ranked.json").read_text())
        check(
            artifact["ranking_schema_version"] == digest.RANKING_SCHEMA_VERSION,
            artifact,
        )


def test_phase_four_concurrency_and_shared_cache() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        cache_dir = root / "cache"
        (root / "run-one").mkdir()
        (root / "run-two").mkdir()
        findings = [
            {
                "title": f"Story {index}",
                "url": f"https://example.com/{index}",
                "source_verdict": "fresh",
            }
            for index in range(3)
        ]
        active = 0
        maximum = 0
        lock = threading.Lock()

        def fake_omp(prompt: str, **_: object) -> str:
            nonlocal active, maximum
            url = prompt.split("Fetch this article: ", 1)[1].splitlines()[0]
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return json.dumps({
                "title": f"Fetched {url.rsplit('/', 1)[-1]}",
                "url": url,
                "date_confirmed": "2026-08-10",
                "author": "",
                "summary": "A detailed factual summary.",
                "key_details": ["detail"],
                "fetch_success": True,
            })

        with patch.object(digest, "ARTICLE_CACHE_DIR", cache_dir), patch.object(
            digest, "_call_omp_p", side_effect=fake_omp
        ) as mocked:
            first = digest.phase_4_fetch(
                digest.TOPICS["ai-tech"], findings, root / "run-one"
            )
            second = digest.phase_4_fetch(
                digest.TOPICS["gaming"], findings, root / "run-two"
            )
        check(maximum == 2, f"expected concurrency 2, saw {maximum}")
        check(mocked.call_count == 3, f"cache did not suppress calls: {mocked.call_count}")
        check([item["url"] for item in first] == [item["url"] for item in findings],
              "concurrency changed output order")
        check(all(item["cache_hit"] for item in second), "second topic missed shared cache")


def editorial_fixture() -> tuple[list[dict], list[dict], dict]:
    # Publication dates are yesterday-relative: the Phase 6c freshness gate
    # (digest-quality audit 2026-08-12) drops fresh selections outside the
    # last-24h window, so fixture candidates must stay fresh-eligible on any
    # run day.
    fresh_day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    candidates, _ = digest._prepare_editorial_candidates([
        {
            "title": "Primary story",
            "url": "https://example.com/primary",
            "source_domain": "example.com",
            "summary": "Primary verified summary.",
            "category": "Research",
            "editorial_significance": "high",
            "date_published": fresh_day,
            "source_verdict": "fresh",
            "judge_verdict": "keep",
        },
        {
            "title": "Secondary story",
            "url": "https://second.example/story",
            "source_domain": "second.example",
            "summary": "Secondary verified summary.",
            "category": "Policy",
            "editorial_significance": "medium",
            "date_published": fresh_day,
            "source_verdict": "fresh",
            "judge_verdict": "keep",
        },
    ], set())
    tracker = {"stories": [{
        "title": "Existing narrative",
        "url": "https://example.com/existing",
        "category": "Research",
        "latest_dev": "Previous development.",
        "status": "active",
        "editorial_significance": "high",
        "first_seen": "2026-08-08",
        "last_updated": "2026-08-09",
        "developments": [
            {"date": "2026-08-08", "url": "https://example.com/existing"},
            {"date": "2026-08-09", "url": "https://example.com/existing-update"},
        ],
    }]}
    return candidates, tracker["stories"], tracker


def test_editorial_validation_and_state_application() -> None:
    candidates, sif_candidates, tracker = editorial_fixture()
    first_id = candidates[0]["candidate_id"]
    # A candidate carrying the tracked story's own URL is legitimate update
    # evidence; a different-story candidate is not (digest-quality audit
    # 2026-08-22: ai-hardware's memory-prices story was overwritten with the
    # related KOSPI story's development).
    same_story = {
        **candidates[0],
        "title": "Existing narrative update",
        "url": "https://example.com/existing",
        "candidate_id": "candidate-same-story",
    }
    candidates.append(same_story)
    same_story_id = same_story["candidate_id"]
    proposal = {
        "selected_fresh": [
            {"candidate_id": same_story_id, "editorial_summary": "Approved summary."},
            {"candidate_id": "candidate-unknown", "editorial_summary": "Bad."},
            {"candidate_id": same_story_id, "editorial_summary": "Duplicate."},
            {"candidate_id": first_id, "editorial_summary": "Cross-story source."},
        ],
        "selected_ongoing": [],
        "story_state_proposals": [
            {
                "operation": "update",
                "story_url": "https://example.com/existing",
                "evidence_candidate_ids": [same_story_id],
                "latest_dev": "New verified development.",
                "editorial_significance": "high",
                "status": "active",
            },
            {
                "operation": "update",
                "story_url": "https://example.com/existing",
                "evidence_candidate_ids": ["candidate-unknown"],
                "latest_dev": "Unsupported.",
            },
            {
                "operation": "update",
                "story_url": "https://example.com/existing",
                "evidence_candidate_ids": [first_id],
                "latest_dev": "Related story development.",
            },
        ],
    }
    validated, warnings = digest._validate_editorial_proposal(
        proposal, candidates, sif_candidates, tracker
    )
    check(len(validated["selected_fresh"]) == 2, validated)
    check(len(validated["story_state_proposals"]) == 2, validated)
    update_ops = [
        op for op in validated["story_state_proposals"]
        if op["operation"] == "update"
    ]
    check(len(update_ops) == 1, validated["story_state_proposals"])
    check(
        validated["balance_summary"]
        == "Validated selection: 2 fresh, 0 developing/ongoing; "
           "1 source domain(s); categories: Research.",
        validated["balance_summary"],
    )
    check(any("unknown candidate_id" in warning for warning in warnings), warnings)
    check(
        any("unlinked tracker update" in warning for warning in warnings),
        warnings,
    )
    original = json.loads(json.dumps(tracker))
    updated = digest._apply_story_state_proposals(
        tracker, validated, candidates, "2026-08-10"
    )
    check(tracker == original, "state application mutated its input")
    check(updated["stories"][0]["latest_dev"] == "New verified development.", updated)
    check(updated["stories"][0]["last_updated"] == "2026-08-10", updated)
    check(
        digest._story_development_dates(updated["stories"][0])
        == {"2026-08-08", "2026-08-09", "2026-08-10"},
        updated["stories"][0],
    )


def test_editorial_critic_patch_contract() -> None:
    candidates, _, tracker = editorial_fixture()
    proposal = {
        "selected_fresh": [
            {"candidate_id": candidates[0]["candidate_id"]},
            {"candidate_id": candidates[1]["candidate_id"]},
        ],
        "selected_ongoing": [],
        "story_state_proposals": [],
    }
    patched, applied, warnings = digest._apply_editorial_patches(proposal, {
        "changes": [{
            "operation": "move_fresh",
            "candidate_id": candidates[1]["candidate_id"],
            "position": 1,
        }],
    })
    check(patched["selected_fresh"][0]["candidate_id"] == candidates[1]["candidate_id"],
          patched)
    check(len(applied) == 1 and not warnings, (applied, warnings))
    check(tracker["stories"], "fixture tracker unexpectedly empty")


def test_editorial_drops_stale_fresh_selection() -> None:
    """A stale Fresh pick cannot enter either output or tracker evidence."""
    candidates, sif_candidates, tracker = editorial_fixture()
    stale_day = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    stale = copy.deepcopy(candidates[0])
    stale["date_published"] = stale_day
    stale["date_confirmed"] = stale_day
    stale["date_tag"] = "ongoing"
    stale["source_verdict"] = "ongoing"
    candidates = [stale, copy.deepcopy(candidates[1])]
    proposal = {
        "selected_fresh": [
            {"candidate_id": candidate["candidate_id"]} for candidate in candidates
        ],
        "selected_ongoing": [],
        "story_state_proposals": [{
            "operation": "add",
            "candidate_id": stale["candidate_id"],
            "evidence_candidate_ids": [stale["candidate_id"]],
            "latest_dev": "Prices still climbing.",
            "editorial_significance": "high",
            "status": "active",
        }],
    }
    validated, warnings = digest._validate_editorial_proposal(
        proposal, candidates, sif_candidates, tracker
    )
    check(len(validated["selected_fresh"]) == 1, validated["selected_fresh"])
    check(
        validated["selected_fresh"][0]["candidate_id"] == candidates[1]["candidate_id"],
        validated["selected_fresh"],
    )
    check(any("stale fresh selection" in warning for warning in warnings), warnings)
    # The qualified developing story may fill the thin digest, but display is
    # not evidence and therefore creates no tracker update.
    check(validated["story_state_proposals"] == [],
          validated["story_state_proposals"])
    updated = digest._apply_story_state_proposals(
        tracker, validated, candidates, "2026-08-10"
    )
    check(
        not any(story.get("url") == stale["url"] for story in updated["stories"]),
        "stale-dropped candidate entered the tracker",
    )
    check(
        updated["stories"][0]["last_updated"] == "2026-08-09",
        "displaying a story fabricated an evidence date",
    )


def test_freshness_gate_rejects_future_dates() -> None:
    """The last-24h freshness window has an upper bound: a future-dated
    candidate must never pass _is_fresh_eligible (digest-quality audit
    2026-08-14: a 2026-10-15-dated story rendered under "Fresh — Last 24 Hours"
    in the 2026-08-12 ai-tech digest)."""
    yesterday = datetime(2026, 8, 13, tzinfo=timezone.utc).date()
    today = datetime(2026, 8, 14, tzinfo=timezone.utc).date()
    future = {"date_confirmed": "2026-10-15", "date_published": "2026-08-12"}
    check(not digest._is_fresh_eligible(future, yesterday, today),
          "future-dated candidate passed the freshness gate")
    fresh = {"date_confirmed": "2026-08-13"}
    check(digest._is_fresh_eligible(fresh, yesterday, today),
          "yesterday-dated candidate must stay fresh-eligible")
    same_day = {"date_confirmed": "2026-08-14"}
    check(digest._is_fresh_eligible(same_day, yesterday, today),
          "today-dated candidate must stay fresh-eligible")
    stale = {"date_confirmed": "2026-08-10"}
    check(not digest._is_fresh_eligible(stale, yesterday, today),
          "stale candidate passed the freshness gate")
    undated = {"date_confirmed": "", "date_published": ""}
    check(digest._is_fresh_eligible(undated, yesterday, today),
          "undated candidate must pass through")


def test_freshness_gate_ignores_future_event_date_confirmed() -> None:
    """A date_confirmed in the future (an event/conference date pulled from the
    article) must not override a fresh date_published. The Hot Chips 08-17
    preview shipped its conference start date (08-24) as date_confirmed, which
    the previous preference logic treated as the best date and dropped as
    future-dated even though it was published within the 24h window
    (digest-quality audit 2026-08-17: ai-hardware shipped zero fresh stories)."""
    yesterday = datetime(2026, 8, 16, tzinfo=timezone.utc).date()
    today = datetime(2026, 8, 17, tzinfo=timezone.utc).date()
    fresh_event = {"date_published": "2026-08-17", "date_confirmed": "2026-08-24"}
    check(digest._is_fresh_eligible(fresh_event, yesterday, today),
          "future event date_confirmed dropped a fresh-eligible candidate")
    # Regression guard: keep the genuine future-dated (publication) rejection.
    genuine_future = {"date_published": "2026-10-15", "date_confirmed": ""}
    check(not digest._is_fresh_eligible(genuine_future, yesterday, today),
          "genuine future-dated publication must still be rejected")


def test_editorial_caps_source_concentration() -> None:
    """Fresh selection is capped at 2 stories per source domain: lower-ranked
    same-source candidates are dropped with a warning instead of shipping a
    single-source Fresh section (digest-quality audit 2026-08-14: ai-tech
    shipped 5 TechCrunch stories, ai-hardware 4 Data Center Dynamics stories)."""
    fresh_day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    candidates, _ = digest._prepare_editorial_candidates([
        {
            "title": f"TechCrunch story {index}",
            "url": f"https://techcrunch.com/{index}",
            "source_domain": "techcrunch.com",
            "summary": f"Verified summary {index}.",
            "category": "Research",
            "editorial_significance": "high" if index == 0 else "medium",
            "date_published": fresh_day,
            "source_verdict": "fresh",
            "judge_verdict": "keep",
        }
        for index in range(3)
    ] + [
        {
            "title": "Other story",
            "url": "https://other.example/story",
            "source_domain": "other.example",
            "summary": "Verified other summary.",
            "category": "Policy",
            "editorial_significance": "medium",
            "date_published": fresh_day,
            "source_verdict": "fresh",
            "judge_verdict": "keep",
        },
    ], set())
    proposal = {
        "selected_fresh": [
            {"candidate_id": candidate["candidate_id"]}
            for candidate in candidates
        ],
        "selected_ongoing": [],
        "story_state_proposals": [],
        "gaps": "",
        "balance_summary": "",
    }
    validated, warnings = digest._validate_editorial_proposal(
        proposal, candidates, [], {"stories": []}
    )
    selected = validated["selected_fresh"]
    domains = {
        candidate["candidate_id"]: candidate["source_domain"]
        for candidate in candidates
    }
    techcrunch_count = sum(
        1 for item in selected if domains[item["candidate_id"]] == "techcrunch.com"
    )
    check(len(selected) == 3, f"expected 3 fresh after cap, got {len(selected)}")
    check(techcrunch_count == 2, f"techcrunch count after cap: {techcrunch_count}")
    check(any("source concentration above 2" in warning for warning in warnings),
          warnings)
    check(any("source concentration cap" in warning for warning in warnings),
          warnings)


def test_editorial_proposal_retries_with_freshness_hint() -> None:
    """A model proposal whose fresh picks were all dropped by the freshness gate
    is retried once with the window reinforced instead of dropping straight to
    raw fallback (digest-quality audit 2026-08-14: agentic-platform shipped
    deterministic raw fallback with no critic review)."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_dir = root / "agentic-platform" / "2026-08-14"
        run_dir.mkdir(parents=True)
        fresh_day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        stale_day = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")

        def build(title: str, url: str, day: str, significance: str) -> dict:
            return {
                "title": title,
                "url": url,
                "source_domain": "example.com",
                "summary": f"{title} verified summary.",
                "category": "Research",
                "editorial_significance": significance,
                "date_published": day,
                "date_confirmed": day,
                "date_tag": "fresh" if day == fresh_day else "ongoing",
                "source_verdict": "fresh" if day == fresh_day else "ongoing",
                "judge_verdict": "keep",
            }

        stale_a = build("Stale story A", "https://example.com/stale-a", stale_day, "high")
        stale_b = build("Stale story B", "https://example.com/stale-b", stale_day, "medium")
        fresh_c = build("Fresh story C", "https://example.com/fresh-c", fresh_day, "medium")
        summaries = [stale_a, stale_b, fresh_c]
        stale_a_id = digest._editorial_candidate_id(stale_a)
        stale_b_id = digest._editorial_candidate_id(stale_b)
        fresh_c_id = digest._editorial_candidate_id(fresh_c)

        stale_only = {
            "selected_fresh": [
                {"candidate_id": stale_a_id},
                {"candidate_id": stale_b_id},
            ],
            "selected_ongoing": [],
            "story_state_proposals": [],
            "gaps": "",
            "balance_summary": "",
        }
        fresh_proposal = {
            "selected_fresh": [{
                "candidate_id": fresh_c_id,
                "rank": 1,
                "editorial_summary": "Reviewed factual summary.",
                "selection_reason": "Only fresh-eligible candidate.",
                "related_story_url": None,
            }],
            "selected_ongoing": [],
            "story_state_proposals": [{
                "operation": "add",
                "candidate_id": fresh_c_id,
                "evidence_candidate_ids": [fresh_c_id],
                "latest_dev": "Reviewed factual summary.",
                "editorial_significance": "high",
                "status": "active",
            }],
            "rejected": [],
            "gaps": "",
            "balance_summary": "One fresh story.",
        }
        responses: list[object] = [
            json.dumps(stale_only),
            json.dumps(fresh_proposal),
            json.dumps({"verdict": "approve", "changes": [], "notes": "Sound."}),
        ]

        def fake_call(*_: object, **__: object) -> str:
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return str(value)

        with patch.object(digest, "DIGESTS_DIR", root), patch.object(
            digest, "_call_llm_proxy", side_effect=fake_call
        ):
            fresh, _, ongoing = digest.phase_6_curate(
                digest.TOPICS["agentic-platform"], summaries, [], {"stories": []}, run_dir
            )
        check(len(fresh) == 1, f"expected 1 fresh after hint retry, got {fresh}")
        check(fresh[0]["url"] == "https://example.com/fresh-c", fresh)
        check(not ongoing, ongoing)
        check(not responses, f"unused model responses: {responses!r}")
        proposal_artifact = json.loads(
            (run_dir / "06a-editorial-proposal.json").read_text()
        )
        check(proposal_artifact["status"] == "model", proposal_artifact["status"])
        check(len(proposal_artifact["errors"]) == 1, proposal_artifact["errors"])
        check(
            "reinforced freshness hint" in proposal_artifact["errors"][0],
            proposal_artifact["errors"],
        )
        artifact = json.loads((run_dir / "06c-editorial-final.json").read_text())
        check(
            artifact["output"]["editorial"]["review_status"] == "reviewed",
            artifact,
        )
        check(
            artifact["output"]["editorial"]["proposal_model"] == digest.MODEL,
            artifact,
        )
        check(
            "validation_warnings" in artifact,
            "06c must persist validation warnings for auditability",
        )


def test_critic_fresh_removal_honored_when_all_candidates_stale() -> None:
    """A critic that removes the last stale fresh story must be honored, not
    converted to review=unavailable with the invalid placement retained
    (digest-quality audit 2026-08-12: ai-hardware shipped a 2d-old RTX story
    under Fresh because the 'removed every valid fresh story' guard fired)."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_dir = root / "ai-hardware" / "2026-08-12"
        run_dir.mkdir(parents=True)
        stale_day = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        summary = {
            "title": "RTX 50-series price spike",
            "url": "https://example.com/rtx-prices",
            "source_domain": "example.com",
            "summary": "Prices up as much as 39%.",
            "category": "GPUs",
            "editorial_significance": "high",
            "date_published": stale_day,
            "date_confirmed": stale_day,
            "date_tag": "ongoing",
            "source_verdict": "ongoing",
            "judge_verdict": "keep",
        }
        candidate_id = digest._editorial_candidate_id(summary)
        proposal = {
            "selected_fresh": [{
                "candidate_id": candidate_id,
                "rank": 1,
                "editorial_summary": "Prices spiked 39%.",
                "selection_reason": "Consumer impact.",
                "related_story_url": None,
            }],
            "selected_ongoing": [],
            "story_state_proposals": [{
                "operation": "add",
                "candidate_id": candidate_id,
                "evidence_candidate_ids": [candidate_id],
                "latest_dev": "Prices spiked 39%.",
                "editorial_significance": "high",
                "status": "active",
            }],
            "rejected": [],
            "gaps": "",
            "balance_summary": "One lead story.",
        }
        responses: list[object] = [
            RuntimeError("primary unavailable"),
            RuntimeError("primary unavailable (retry)"),
            json.dumps(proposal),
            json.dumps({"verdict": "approve_with_changes", "changes": [{
                "operation": "remove_fresh",
                "candidate_id": candidate_id,
            }], "notes": "Sole candidate is outside the 24h freshness window."}),
        ]

        def fake_call(*_: object, **__: object) -> str:
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return str(value)

        with patch.object(digest, "DIGESTS_DIR", root), patch.object(
            digest, "_call_llm_proxy", side_effect=fake_call
        ):
            fresh, updated, _ = digest.phase_6_curate(
                digest.TOPICS["ai-hardware"], [summary], [], {}, run_dir
            )
        check(fresh == [], f"stale story shipped under Fresh: {fresh}")
        artifact = json.loads((run_dir / "06-curated.json").read_text())
        check(
            artifact["editorial"]["review_status"] == "reviewed",
            artifact["editorial"],
        )
        review_artifact = json.loads(
            (run_dir / "06b-editorial-review.json").read_text()
        )
        check(not review_artifact["errors"], review_artifact["errors"])
        # The stale story was not selected for anything, so it is not tracked.
        check(
            not any(
                story.get("url") == summary["url"]
                for story in updated.get("stories", [])
            ),
            updated,
        )
        check(not responses, f"unused model responses: {responses!r}")


def test_critic_emptying_valid_fresh_still_fails_closed() -> None:
    """The 'removed every valid fresh story' guard must still fire when
    genuinely fresh candidates exist, so a broken critic cannot empty the
    digest; the validated proposal is retained."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_dir = root / "ai-tech" / "2026-08-12"
        run_dir.mkdir(parents=True)
        candidates, _, tracker = editorial_fixture()
        summaries = [
            {key: value for key, value in candidate.items() if key != "candidate_id"}
            for candidate in candidates
        ]
        proposal = {
            "selected_fresh": [
                {"candidate_id": candidates[0]["candidate_id"], "rank": 1,
                 "editorial_summary": "Fresh story one.", "selection_reason": "Top.",
                 "related_story_url": None},
                {"candidate_id": candidates[1]["candidate_id"], "rank": 2,
                 "editorial_summary": "Fresh story two.", "selection_reason": "Second.",
                 "related_story_url": None},
            ],
            "selected_ongoing": [],
            "story_state_proposals": [],
            "rejected": [],
            "gaps": "",
            "balance_summary": "Two fresh stories.",
        }
        responses: list[object] = [
            json.dumps(proposal),
            json.dumps({"verdict": "approve_with_changes", "changes": [
                {"operation": "remove_fresh",
                 "candidate_id": candidates[0]["candidate_id"]},
                {"operation": "remove_fresh",
                 "candidate_id": candidates[1]["candidate_id"]},
            ], "notes": "Removing all fresh."}),
        ]

        def fake_call(*_: object, **__: object) -> str:
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return str(value)

        with patch.object(digest, "DIGESTS_DIR", root), patch.object(
            digest, "_call_llm_proxy", side_effect=fake_call
        ):
            fresh, _, _ = digest.phase_6_curate(
                digest.TOPICS["ai-tech"], summaries, [], tracker, run_dir
            )
        artifact = json.loads((run_dir / "06-curated.json").read_text())
        check(artifact["editorial"]["review_status"] == "unavailable", artifact)
        check(len(fresh) == 2, f"valid fresh stories were lost: {fresh}")


def test_phase_six_fallback_and_review_chain() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_dir = root / "ai-tech" / "2026-08-10"
        run_dir.mkdir(parents=True)
        candidates, _, tracker = editorial_fixture()
        summaries = [
            {key: value for key, value in candidate.items() if key != "candidate_id"}
            for candidate in candidates
        ]
        selected_id = candidates[0]["candidate_id"]
        proposal = {
            "selected_fresh": [{
                "candidate_id": selected_id,
                "rank": 1,
                "editorial_summary": "Reviewed factual summary.",
                "selection_reason": "Highest product priority.",
                "related_story_url": None,
            }],
            "selected_ongoing": [],
            "story_state_proposals": [{
                "operation": "add",
                "candidate_id": selected_id,
                "evidence_candidate_ids": [selected_id],
                "latest_dev": "Reviewed factual summary.",
                "editorial_significance": "high",
                "status": "active",
            }],
            "rejected": [],
            "gaps": "",
            "balance_summary": "One lead story.",
        }
        responses: list[object] = [
            RuntimeError("primary unavailable"),
            RuntimeError("primary unavailable (retry)"),
            json.dumps(proposal),
            json.dumps({"verdict": "approve", "changes": [], "notes": "Sound."}),
        ]

        def fake_call(*_: object, **__: object) -> str:
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return str(value)

        with patch.object(digest, "DIGESTS_DIR", root), patch.object(
            digest, "_call_llm_proxy", side_effect=fake_call
        ):
            fresh, updated, ongoing = digest.phase_6_curate(
                digest.TOPICS["ai-tech"], summaries, [], tracker, run_dir
            )
        check(len(fresh) == 1 and not ongoing, (fresh, ongoing))
        check(len(updated["stories"]) == 2, updated)
        artifact = json.loads((run_dir / "06-curated.json").read_text())
        check(artifact["editorial"]["proposal_model"] == digest.MODEL_FALLBACK, artifact)
        check(artifact["editorial"]["review_status"] == "reviewed", artifact)
        check(
            artifact["editorial"]["degraded"] is True,
            "fallback-model proposal must be flagged degraded (digest-quality audit)",
        )
        check(not responses, f"unused model responses: {responses!r}")


def test_editorial_proposal_retries_primary_before_fallback() -> None:
    """A single primary proposal failure must be retried, not degrade to fallback."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_dir = root / "ai-tech" / "2026-08-10"
        run_dir.mkdir(parents=True)
        candidates, _, tracker = editorial_fixture()
        summaries = [
            {key: value for key, value in candidate.items() if key != "candidate_id"}
            for candidate in candidates
        ]
        selected_id = candidates[0]["candidate_id"]
        proposal = {
            "selected_fresh": [{
                "candidate_id": selected_id,
                "rank": 1,
                "editorial_summary": "Retried primary summary.",
                "selection_reason": "Highest product priority.",
                "related_story_url": None,
            }],
            "selected_ongoing": [],
            "story_state_proposals": [{
                "operation": "add",
                "candidate_id": selected_id,
                "evidence_candidate_ids": [selected_id],
                "latest_dev": "Retried primary summary.",
                "editorial_significance": "high",
                "status": "active",
            }],
            "rejected": [],
            "gaps": "",
            "balance_summary": "One lead story.",
        }
        responses: list[object] = [
            RuntimeError("Could not extract JSON from editorial proposal (primary). Raw text: ```json {"),
            json.dumps(proposal),
            json.dumps({"verdict": "approve", "changes": [], "notes": "Sound."}),
        ]

        def fake_call(*_: object, **__: object) -> str:
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return str(value)

        with patch.object(digest, "DIGESTS_DIR", root), patch.object(
            digest, "_call_llm_proxy", side_effect=fake_call
        ):
            fresh, _, ongoing = digest.phase_6_curate(
                digest.TOPICS["ai-tech"], summaries, [], tracker, run_dir
            )
        check(len(fresh) == 1 and not ongoing, (fresh, ongoing))
        artifact = json.loads((run_dir / "06-curated.json").read_text())
        check(
            artifact["editorial"]["proposal_model"] == digest.MODEL,
            artifact,
        )
        check(
            artifact["editorial"]["degraded"] is False,
            artifact["editorial"],
        )
        check(
            len(artifact["editorial"]["proposal_model"]) > 0,
            "proposal model missing",
        )
        check(not responses, f"unused model responses: {responses!r}")
        proposal_artifact = json.loads(
            (run_dir / "06a-editorial-proposal.json").read_text()
        )
        check(len(proposal_artifact["errors"]) == 1, proposal_artifact["errors"])


def test_editorial_critic_retries_primary_after_transient_error() -> None:
    """A transient primary critic error (proxy 500) must be retried once."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_dir = root / "ai-tech" / "2026-08-10"
        run_dir.mkdir(parents=True)
        candidates, _, tracker = editorial_fixture()
        summaries = [
            {key: value for key, value in candidate.items() if key != "candidate_id"}
            for candidate in candidates
        ]
        selected_id = candidates[0]["candidate_id"]
        proposal = {
            "selected_fresh": [{
                "candidate_id": selected_id,
                "rank": 1,
                "editorial_summary": "Reviewed factual summary.",
                "selection_reason": "Highest product priority.",
                "related_story_url": None,
            }],
            "selected_ongoing": [],
            "story_state_proposals": [{
                "operation": "add",
                "candidate_id": selected_id,
                "evidence_candidate_ids": [selected_id],
                "latest_dev": "Reviewed factual summary.",
                "editorial_significance": "high",
                "status": "active",
            }],
            "rejected": [],
            "gaps": "",
            "balance_summary": "One lead story.",
        }
        responses: list[object] = [
            json.dumps(proposal),
            RuntimeError("deepseek-v4-flash: 500 Server Error: Internal Server Error for url: http://localhost:8082/v1/chat/completions"),
            json.dumps({"verdict": "approve", "changes": [], "notes": "Sound on retry."}),
        ]

        def fake_call(*_: object, **__: object) -> str:
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return str(value)

        with patch.object(digest, "DIGESTS_DIR", root), patch.object(
            digest, "_call_llm_proxy", side_effect=fake_call
        ):
            fresh, _, _ = digest.phase_6_curate(
                digest.TOPICS["ai-tech"], summaries, [], tracker, run_dir
            )
        check(len(fresh) == 1, (fresh,))
        artifact = json.loads((run_dir / "06-curated.json").read_text())
        check(
            artifact["editorial"]["review_model"] == digest.MODEL_REVIEWER,
            artifact,
        )
        check(artifact["editorial"]["review_status"] == "reviewed", artifact)
        check(not responses, f"unused model responses: {responses!r}")
        review_artifact = json.loads(
            (run_dir / "06b-editorial-review.json").read_text()
        )
        check(len(review_artifact["errors"]) == 1, review_artifact["errors"])


def test_critic_rejection_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_dir = root / "ai-tech" / "2026-08-10"
        run_dir.mkdir(parents=True)
        candidates, _, tracker = editorial_fixture()
        summaries = [
            {key: value for key, value in candidate.items() if key != "candidate_id"}
            for candidate in candidates
        ]
        selected_id = candidates[0]["candidate_id"]
        proposal = {
            "selected_fresh": [{
                "candidate_id": selected_id,
                "editorial_summary": "Proposed summary.",
            }],
            "selected_ongoing": [],
            "story_state_proposals": [{
                "operation": "add",
                "candidate_id": selected_id,
                "evidence_candidate_ids": [selected_id],
                "latest_dev": "Proposed summary.",
                "editorial_significance": "high",
                "status": "active",
            }],
        }
        responses = [
            json.dumps(proposal),
            json.dumps({"verdict": "reject", "changes": []}),
            json.dumps({"verdict": "reject", "changes": []}),
        ]
        with patch.object(digest, "DIGESTS_DIR", root), patch.object(
            digest, "_call_llm_proxy", side_effect=responses
        ):
            fresh, updated, _ = digest.phase_6_curate(
                digest.TOPICS["ai-tech"], summaries, [], tracker, run_dir
            )
        artifact = json.loads((run_dir / "06-curated.json").read_text())
        check(len(fresh) == 2, "critic rejection did not use source-ranked fallback")
        # Rejected state cannot apply. The deterministic fallback records only
        # selected high-significance roots; medium one-offs are not follow-up
        # candidates for Developing and Ongoing.
        check(
            not any(
                story.get("latest_dev") == "Proposed summary."
                for story in updated["stories"]
            ),
            "rejected state proposal was applied",
        )
        today = datetime.now().strftime("%Y-%m-%d")
        added = {
            story.get("url"): story
            for story in updated["stories"]
            if story.get("first_seen") == today
        }
        check(
            set(added) == {"https://example.com/primary"},
            f"fallback tracked non-high fresh stories: {added}",
        )
        check(
            all(
                story.get("last_updated") == today
                and story.get("editorial_significance") == "high"
                and len(story.get("developments", [])) == 1
                for story in added.values()
            ),
            added,
        )
        check(
            artifact["editorial"]["review_status"] == "rejected_fallback",
            artifact,
        )


def test_standfirst_boundary_and_deterministic_render() -> None:
    stories = [{"title": "Safe <Title>", "summary": "Verified 12% result."}]
    valid, _ = digest._validate_standfirst(
        "Verified results reached 12%. The source-backed change reshapes the market.",
        stories,
    )
    check(valid, "source-backed newspaper standfirst was rejected")
    valid, reason = digest._validate_standfirst(
        "Verified results reached 99%. The change reshapes the market.", stories
    )
    check(not valid and "99" in reason, reason)
    valid, reason = digest._validate_standfirst(
        "Today’s digest leads with the verified 12% result. Read on for details.",
        stories,
    )
    check(not valid and "meta language" in reason, reason)
    valid, reason = digest._validate_standfirst(
        "Verified results reached 12% while the market", stories
    )
    check(not valid and "mid-sentence" in reason, reason)
    fallback = digest._fallback_standfirst(
        [{"summary": "A verified change occurred. Additional detail follows."}], []
    )
    check(fallback == "A verified change occurred.", fallback)
    clipped = digest._clean_editorial_text("word " * 300, limit=80)
    check(clipped.endswith("word…") and len(clipped) <= 81, clipped)

    fresh = [{
        "title": "Safe <Title>",
        "url": "https://example.com/story?a=1&b=2",
        "category": "Research",
        "summary": "Verified & reviewed.",
    }]
    ongoing = [{
        "title": "Ongoing",
        "url": "https://example.com/ongoing",
        "category": "Policy",
        "summary": "Existing summary.",
        "why_still_relevant": "New evidence.",
    }]
    rendered = digest._render_digest_html(
        {"title": "Test Section"}, fresh, ongoing, "Verified source-backed standfirst."
    )
    check("Safe &lt;Title&gt;" in rendered, "title was not escaped")
    check('href="https://example.com/story?a=1&amp;b=2"' in rendered,
          "URL was not safely rendered")
    check("↳ New evidence." in rendered, "ongoing rationale missing")
    check("{{FRESH_STORIES}}" not in rendered, "template placeholder remained")
    check("STORY BLOCK TEMPLATE" not in rendered, "template instructions leaked")
    check(
        "Developing and Ongoing" in rendered,
        "rendered section label did not match the editorial contract",
    )


def test_tracker_updates_require_material_evidence() -> None:
    """Rendering is not evidence; an exact source-linked follow-up is."""
    candidates, sif_candidates, tracker = editorial_fixture()
    proposal = {
        "selected_fresh": [{"candidate_id": candidates[1]["candidate_id"]}],
        "selected_ongoing": [{
            "story_url": "https://example.com/existing",
            "summary": "Established multi-day development.",
            "why_still_relevant": "Latest verified action remains in effect.",
        }],
        "story_state_proposals": [],
    }
    validated, _ = digest._validate_editorial_proposal(
        proposal, candidates, sif_candidates, tracker
    )
    check(validated["story_state_proposals"] == [],
          validated["story_state_proposals"])
    original = json.loads(json.dumps(tracker))
    displayed = digest._apply_story_state_proposals(
        tracker, validated, candidates, "2026-08-10"
    )
    check(tracker == original, "state application mutated its input")
    check(displayed["stories"][0]["last_updated"] == "2026-08-09",
          displayed["stories"][0])

    # A new article may update a tracked root only when the dedicated research
    # path declared the exact relationship. The candidate URL may differ.
    followup = {
        **candidates[0],
        "title": "Existing narrative materially advances",
        "url": "https://news.example.com/existing-action",
        "candidate_id": "candidate-linked-followup",
        "develops_story_url": "https://example.com/existing",
    }
    candidates.append(followup)
    with_update = {
        "selected_fresh": [{
            "candidate_id": followup["candidate_id"],
            "editorial_summary": "Officials took a new, verified action.",
            "related_story_url": "https://example.com/existing",
        }],
        "selected_ongoing": [{
            "story_url": "https://example.com/existing",
            "summary": "The tracked story now includes the official action.",
            "why_still_relevant": "Officials took a new action today.",
        }],
        "story_state_proposals": [],
    }
    validated, _ = digest._validate_editorial_proposal(
        with_update, candidates, sif_candidates, tracker
    )
    update_ops = [
        op for op in validated["story_state_proposals"]
        if op["operation"] == "update"
    ]
    check(len(update_ops) == 1, validated["story_state_proposals"])
    check(
        update_ops[0]["evidence_candidate_ids"] == [followup["candidate_id"]],
        update_ops,
    )
    updated = digest._apply_story_state_proposals(
        tracker, validated, candidates, "2026-08-10"
    )
    story = updated["stories"][0]
    check(story["latest_dev"] == "Officials took a new, verified action.", story)
    check(
        digest._story_development_dates(story)
        == {"2026-08-08", "2026-08-09", "2026-08-10"},
        story,
    )


def test_developing_section_requires_significance_and_multiple_dates() -> None:
    """One-off and non-high stories never qualify, regardless of age/touches."""
    candidates, _, _ = editorial_fixture()
    one_off = {
        "title": "Single announcement",
        "url": "https://tracker.example/announcement",
        "category": "Industry",
        "latest_dev": "The original announcement.",
        "status": "active",
        "editorial_significance": "high",
        "first_seen": "2026-08-20",
        # A legacy display touch must not count as evidence.
        "last_updated": "2026-08-24",
    }
    medium_multiday = {
        "title": "Repeated but not important",
        "url": "https://tracker.example/medium",
        "category": "Industry",
        "latest_dev": "A second minor update.",
        "status": "active",
        "editorial_significance": "medium",
        "first_seen": "2026-08-20",
        "last_updated": "2026-08-22",
        "developments": [
            {"date": "2026-08-20", "url": "https://tracker.example/medium"},
            {"date": "2026-08-22", "url": "https://news.example/medium-update"},
        ],
    }
    qualified = {
        "title": "Important story with real movement",
        "url": "https://tracker.example/qualified",
        "category": "Policy",
        "latest_dev": "Officials issued a binding decision.",
        "status": "active",
        "editorial_significance": "high",
        "first_seen": "2026-08-20",
        "last_updated": "2026-08-22",
        "developments": [
            {"date": "2026-08-20", "url": "https://tracker.example/qualified"},
            {"date": "2026-08-22", "url": "https://news.example/binding-decision"},
        ],
    }
    tracker = {"stories": [one_off, medium_multiday, qualified]}
    proposal = {
        "selected_fresh": [
            {"candidate_id": candidates[0]["candidate_id"]},
            {"candidate_id": candidates[1]["candidate_id"]},
        ],
        "selected_ongoing": [
            {"story_url": story["url"], "summary": story["latest_dev"],
             "why_still_relevant": story["latest_dev"]}
            for story in tracker["stories"]
        ],
        "story_state_proposals": [],
    }
    validated, warnings = digest._validate_editorial_proposal(
        proposal, candidates, tracker["stories"], tracker
    )
    check(
        [item["story_url"] for item in validated["selected_ongoing"]]
        == [qualified["url"]],
        validated["selected_ongoing"],
    )
    check(
        sum("unqualified developing story" in warning for warning in warnings) == 2,
        warnings,
    )


def test_followup_research_targets_prior_high_significance_stories() -> None:
    today = datetime(2026, 8, 25, tzinfo=timezone.utc).date()
    stories = {
        "stories": [
            {
                "title": "Prior high story",
                "url": "https://tracker.example/high",
                "editorial_significance": "high",
                "status": "active",
                "first_seen": "2026-08-24",
                "last_updated": "2026-08-24",
            },
            {
                "title": "Prior medium story",
                "url": "https://tracker.example/medium",
                "editorial_significance": "medium",
                "status": "active",
                "first_seen": "2026-08-24",
                "last_updated": "2026-08-24",
            },
            {
                "title": "Same-day high story",
                "url": "https://tracker.example/today",
                "editorial_significance": "high",
                "status": "active",
                "first_seen": "2026-08-25",
                "last_updated": "2026-08-25",
            },
        ]
    }
    angle = digest._build_developing_followup_angle(stories, today)
    check(angle is not None, "high-priority prior story was not scheduled")
    prompt = angle["prompt"]
    check("https://tracker.example/high" in prompt, prompt)
    check("https://tracker.example/medium" not in prompt, prompt)
    check("https://tracker.example/today" not in prompt, prompt)


def test_tracker_retention_uses_evidence_inactivity() -> None:
    today = datetime(2026, 8, 25, tzinfo=timezone.utc).date()
    active_long_running = {
        "title": "Long-running active crisis",
        "url": "https://tracker.example/active",
        "editorial_significance": "high",
        "status": "active",
        "first_seen": "2026-08-01",
        "last_updated": "2026-08-24",
        "developments": [
            {"date": "2026-08-01", "url": "https://tracker.example/active"},
            {"date": "2026-08-24", "url": "https://news.example/latest-action"},
        ],
    }
    inactive_active = {
        "title": "Recently stalled story",
        "url": "https://tracker.example/stalled",
        "editorial_significance": "high",
        "status": "active",
        "first_seen": "2026-08-10",
        "last_updated": "2026-08-20",
        "developments": [
            {"date": "2026-08-10", "url": "https://tracker.example/stalled"},
            {"date": "2026-08-20", "url": "https://news.example/stalled-update"},
        ],
    }
    expired_cooled = {
        "title": "Expired cooled story",
        "url": "https://tracker.example/expired",
        "editorial_significance": "high",
        "status": "cooled",
        "first_seen": "2026-08-01",
        "last_updated": "2026-08-10",
        "developments": [
            {"date": "2026-08-10", "url": "https://tracker.example/expired"},
        ],
    }
    kept, cooled, pruned = digest._prune_and_cool_stories(
        [active_long_running, inactive_active, expired_cooled], today
    )
    kept_by_url = {story["url"]: story for story in kept}
    check(active_long_running["url"] in kept_by_url, kept)
    check(kept_by_url[inactive_active["url"]]["status"] == "cooled", kept)
    check(expired_cooled["url"] not in kept_by_url, kept)
    check((cooled, pruned) == (1, 1), (cooled, pruned))


def test_ongoing_resurface_cap_cools_recurring_story() -> None:
    """An Ongoing story surfaced on many consecutive days without an
    evidence-backed development must be dropped and cooled, so the digest
    cannot repeat the same story day after day (digest-quality audit
    2026-08-22: the 404 Media rare-books story ran in ai-tech and OpenAI's
    PORTS-Pike story in ai-hardware on five consecutive days 08-18→08-22 with
    paraphrased summaries of the same facts)."""
    from datetime import date as date_cls
    story_url = "https://example.com/recurring"
    tracker = {"stories": [{
        "title": "Recurring story",
        "url": story_url,
        "category": "Research",
        "latest_dev": "No new development.",
        "status": "active",
        "editorial_significance": "medium",
        "first_seen": "2026-08-18",
        "last_updated": "2026-08-21",
    }]}
    proposal = {
        "selected_fresh": [],
        "selected_ongoing": [{
            "story_url": story_url,
            "summary": "Same facts as yesterday.",
            "why_still_relevant": "Still the lead.",
        }],
        "story_state_proposals": [],
    }
    today = date_cls(2026, 8, 22)
    with tempfile.TemporaryDirectory() as temporary:
        digest_dir = Path(temporary)
        # Days 08-18..08-21 all surfaced the story → this run would be day 5.
        for day in range(18, 22):
            curated_dir = digest_dir / f"2026-08-{day:02d}"
            curated_dir.mkdir()
            (curated_dir / "06-curated.json").write_text(json.dumps({
                "fresh": [],
                "ongoing": [{"url": story_url, "title": "Recurring story"}],
            }))
        warnings, ops = digest._enforce_ongoing_resurface_cap(
            proposal, tracker, digest_dir, today
        )
        check(proposal["selected_ongoing"] == [], proposal["selected_ongoing"])
        check(
            any("consecutive days" in warning for warning in warnings), warnings
        )
        check(
            len(ops) == 1
            and ops[0]["operation"] == "update"
            and ops[0]["story_url"] == story_url
            and ops[0]["status"] == "cooled"
            and ops[0]["latest_dev"] == "No new development.",
            ops,
        )

        # A gap in the run resets the counter: 4 days total but not consecutive
        # means the story is not capped.
        gap_dir = Path(temporary) / "gap"
        gap_dir.mkdir()
        for day in (18, 19, 21):  # 08-20 missing
            curated_dir = gap_dir / f"2026-08-{day:02d}"
            curated_dir.mkdir()
            (curated_dir / "06-curated.json").write_text(json.dumps({
                "fresh": [],
                "ongoing": [{"url": story_url}],
            }))
        check(
            digest._consecutive_surfaced_days(gap_dir, story_url, today) == 1,
            digest._consecutive_surfaced_days(gap_dir, story_url, today),
        )

        # An evidence-backed update op (a real development) resets the cap.
        evidenced = {
            "selected_fresh": [],
            "selected_ongoing": [{
                "story_url": story_url,
                "summary": "Same facts as yesterday.",
                "why_still_relevant": "Still the lead.",
            }],
            "story_state_proposals": [{
                "operation": "update",
                "story_url": story_url,
                "evidence_candidate_ids": ["candidate-x"],
                "latest_dev": "New development.",
                "editorial_significance": "medium",
                "status": "active",
            }],
        }
        warnings, ops = digest._enforce_ongoing_resurface_cap(
            evidenced, tracker, digest_dir
        )
        check(len(evidenced["selected_ongoing"]) == 1, evidenced["selected_ongoing"])
        check(warnings == [] and ops == [], (warnings, ops))


def test_editorial_floor_and_publication_artifact() -> None:
    """The editorial floor rejects filler and Phase 8 publishes stable local data."""
    candidates, sif_candidates, tracker = editorial_fixture()
    low_one_off = {
        "title": "Low-priority one-off",
        "url": "https://second.example/one-off",
        "category": "Policy",
        "latest_dev": "Only one minor report.",
        "status": "active",
        "editorial_significance": "low",
        "first_seen": "2026-08-10",
        "last_updated": "2026-08-10",
    }
    second_sif = {
        "title": "Second qualified developing story",
        "url": "https://second.example/ongoing",
        "category": "Policy",
        "latest_dev": "A binding second development.",
        "status": "active",
        "editorial_significance": "high",
        "first_seen": "2026-08-09",
        "last_updated": "2026-08-10",
        "developments": [
            {"date": "2026-08-09", "url": "https://second.example/ongoing"},
            {"date": "2026-08-10", "url": "https://news.example/second-action"},
        ],
    }
    sif_candidates.extend([low_one_off, second_sif])

    # 1 fresh + 0 ongoing → floor uses the newest qualified story only.
    proposal = {
        "selected_fresh": [{"candidate_id": candidates[0]["candidate_id"]}],
        "selected_ongoing": [],
        "story_state_proposals": [],
    }
    validated, warnings = digest._validate_editorial_proposal(
        proposal, candidates, sif_candidates, tracker
    )
    filled = {item["story_url"] for item in validated["selected_ongoing"]}
    check(filled == {"https://second.example/ongoing"},
          validated["selected_ongoing"])
    check(
        len(validated["selected_fresh"]) + len(validated["selected_ongoing"]) == 2,
        validated,
    )
    check(
        all(item["why_still_relevant"] for item in validated["selected_ongoing"]),
        validated["selected_ongoing"],
    )

    # Floor does not add a second story once two are selected.
    both = {
        "selected_fresh": [
            {"candidate_id": candidates[0]["candidate_id"]},
            {"candidate_id": candidates[1]["candidate_id"]},
        ],
        "selected_ongoing": [{
            "story_url": "https://example.com/existing",
            "summary": "Summarized.",
            "why_still_relevant": "Relevant.",
        }],
        "story_state_proposals": [],
    }
    validated, _ = digest._validate_editorial_proposal(
        both, candidates, sif_candidates, tracker
    )
    check(len(validated["selected_ongoing"]) == 1, validated["selected_ongoing"])

    # 0 fresh + 0 ongoing and an empty pool remains honestly empty.
    empty_validated, _ = digest._validate_editorial_proposal(
        {"selected_fresh": [], "selected_ongoing": [], "story_state_proposals": []},
        candidates, [], {"stories": []}, set(),
    )
    check(not empty_validated["selected_fresh"] and not empty_validated["selected_ongoing"],
          empty_validated)

    with tempfile.TemporaryDirectory() as temporary:
        digest_dir = Path(temporary) / "world-digest"
        run_dir = digest_dir / f"{datetime.now():%Y-%m-%d}"
        digest_dir.mkdir(parents=True)
        run_dir.mkdir()
        (run_dir / "06-curated.json").write_text(json.dumps({"fresh": [], "ongoing": []}))
        (run_dir / "07-standfirst.json").write_text(json.dumps({
            "prompt_version": digest.STANDFIRST_PROMPT_VERSION,
            "standfirst": "Two verified stories reshape policy and public debate."
        }))
        fresh_story = {
            "title": "First",
            "url": "https://example.com/a",
            "summary": "First source-backed summary.",
            "category": "Policy",
            "editorial_significance": "high",
            "priority_score": 91.5,
            "priority_explanation": "High significance and broad observed coverage.",
            "candidate_id": "private-editorial-id",
        }
        ongoing_story = {
            "title": "Second",
            "url": "https://example.com/b",
            "summary": "Second source-backed summary.",
            "editorial_significance": "high",
            "priority_score": 100.0,
            "why_still_relevant": "A material development occurred today.",
            "selection_reason": "private editorial reasoning",
        }

        with patch("digest_runner.subprocess.run") as subprocess_run:
            publication_path = digest.phase_8_archive(
                digest.TOPICS["world"],
                "<html>archive</html>",
                {"stories": []},
                run_dir,
                digest_dir,
                fresh=[fresh_story],
                ongoing=[ongoing_story],
            )
        check(not subprocess_run.called, "topic archive attempted to send email")
        check((digest_dir / f"{datetime.now():%Y-%m-%d}.html").exists(),
              "daily HTML archive missing")
        publication = json.loads(publication_path.read_text())
        check(publication["slug"] == "world", publication)
        check(publication["schema_version"] == 2, publication)
        check(publication["standfirst"].startswith("Two verified"), publication["standfirst"])
        check(len(publication["fresh"]) + len(publication["ongoing"]) == 2, publication)
        check("candidate_id" not in publication["fresh"][0], publication["fresh"][0])
        check("selection_reason" not in publication["ongoing"][0], publication["ongoing"][0])
        check(publication["ongoing"][0]["why_still_relevant"].startswith("A material"),
              publication["ongoing"][0])


def test_listing_urls_rejected() -> None:
    """Section/date archive URLs (Guardian .../all) must never be selected into
    Fresh or Ongoing or enter the tracker (digest-quality audit 2026-08-21:
    world-digest ongoing entries on 08-20 and 08-21 were the same two Guardian
    .../all pages, which fetch as the section listing, not an article)."""
    listing = "https://www.theguardian.com/technology/2026/aug/18/all"
    check(digest._is_listing_url(listing), listing)
    check(digest._is_listing_url(listing + "?utm_source=x"), "query-suffixed listing")
    check(not digest._is_listing_url("https://www.theguardian.com/world/article"),
          "normal article flagged")
    check(not digest._is_listing_url("https://example.com/all-about-x"),
          "prefix segment flagged")

    fresh_day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    candidates, _ = digest._prepare_editorial_candidates([
        {
            "title": "OpenAI listing page",
            "url": listing,
            "source_domain": "theguardian.com",
            "summary": "Search result title on the listing page.",
            "category": "Technology",
            "editorial_significance": "high",
            "date_published": fresh_day,
            "source_verdict": "fresh",
            "judge_verdict": "keep",
        },
    ], set())
    tracker_listing = {
        "title": "Tracked listing",
        "url": listing,
        "category": "Technology",
        "latest_dev": "Development.",
        "status": "active",
        "editorial_significance": "medium",
        "first_seen": "2026-08-18",
        "last_updated": "2026-08-19",
    }
    proposal = {
        "selected_fresh": [
            {"candidate_id": candidates[0]["candidate_id"]},
        ],
        "selected_ongoing": [{
            "story_url": listing,
            "summary": "Still listed.",
            "why_still_relevant": "Resurfacing identically.",
        }],
        "story_state_proposals": [{
            "operation": "update",
            "story_url": listing,
            "evidence_candidate_ids": [candidates[0]["candidate_id"]],
            "latest_dev": "Updated.",
            "editorial_significance": "medium",
            "status": "active",
        }],
    }
    validated, warnings = digest._validate_editorial_proposal(
        proposal, candidates, [tracker_listing],
        {"stories": [tracker_listing]}, set(),
    )
    check(validated["selected_fresh"] == [], validated["selected_fresh"])
    check(validated["selected_ongoing"] == [], validated["selected_ongoing"])
    check(validated["story_state_proposals"] == [], validated["story_state_proposals"])
    check(any("listing URL fresh selection" in warning for warning in warnings), warnings)
    check(any("listing URL ongoing story" in warning for warning in warnings), warnings)

    # The floor must not fill a thin digest with a listing URL either.
    floor_proposal = {
        "selected_fresh": [], "selected_ongoing": [], "story_state_proposals": []
    }
    validated, _ = digest._validate_editorial_proposal(
        floor_proposal, candidates, [tracker_listing],
        {"stories": [tracker_listing]}, set(),
    )
    check(validated["selected_ongoing"] == [], validated["selected_ongoing"])


def test_stub_retry_preserves_failed_attempt_artifacts() -> None:
    """Stub/fallback retries archive the failed attempt's phase JSON instead of deleting it."""
    with tempfile.TemporaryDirectory() as temporary:
        run_dir = Path(temporary)
        names = ["01-research-raw.json", "03-urls-ranked.json", "06-curated.json"]
        for name in names:
            (run_dir / name).write_text(json.dumps({"attempt": 1, "name": name}))
        digest._archive_stub_attempt(run_dir)
        archived = sorted(p.name for p in run_dir.glob("stub-attempt-*/*.json"))
        check(archived == names, f"archived={archived}")
        check(not list(run_dir.glob("0*-*.json")), "failed attempt artifacts not preserved")


def test_stub_attempts_cleaned_after_success() -> None:
    """Archived stub-attempt subdirs are removed once the final run completes so
    audits don't double-count partial runs (digest-quality audit 2026-08-24)."""
    with tempfile.TemporaryDirectory() as temporary:
        run_dir = Path(temporary)
        stub = run_dir / "stub-attempt-20260823-080644-765214"
        stub.mkdir()
        (stub / "01-research-raw.json").write_text("{}")
        keep = run_dir / "06-curated.json"
        keep.write_text("{}")
        digest._cleanup_stub_attempts(run_dir)
        check(not stub.exists(), "stub-attempt dir not removed")
        check(keep.exists(), "final-run artifact was removed")


def test_asset_cdn_urls_rejected() -> None:
    """Publisher asset-CDN hosts (assets.theregister.com) must never be selected
    into Fresh or Ongoing or enter the tracker; they are not article hosts
    (digest-quality audit 2026-08-24: research invented assets.theregister.com
    links that 405'd and resurfaced in the tracker for five days)."""
    cdn = "https://assets.theregister.com/2026/08/19/20262/?td=keepreading&utm_source=openai"
    check(digest._is_asset_cdn_url(cdn), cdn)
    check(digest._is_asset_cdn_url(cdn + "?x=1"), "query-suffixed asset CDN")
    check(not digest._is_asset_cdn_url(
        "https://www.theregister.com/systems/2026/08/19/story/1"),
        "article host flagged")

    fresh_day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    candidates, _ = digest._prepare_editorial_candidates([
        {
            "title": "Baidu chips",
            "url": cdn,
            "source_domain": "theregister.com",
            "summary": "Baidu chip demand rising.",
            "category": "AI Infrastructure",
            "editorial_significance": "high",
            "date_published": fresh_day,
            "source_verdict": "fresh",
            "judge_verdict": "keep",
        },
    ], set())
    tracker_cdn = {
        "title": "Tracked asset-CDN story",
        "url": cdn,
        "category": "AI Infrastructure",
        "latest_dev": "Development.",
        "status": "active",
        "editorial_significance": "medium",
        "first_seen": "2026-08-20",
        "last_updated": "2026-08-20",
    }
    proposal = {
        "selected_fresh": [
            {"candidate_id": candidates[0]["candidate_id"]},
        ],
        "selected_ongoing": [{
            "story_url": cdn,
            "summary": "Still developing.",
            "why_still_relevant": "Resurfacing identically.",
        }],
        "story_state_proposals": [{
            "operation": "update",
            "story_url": cdn,
            "evidence_candidate_ids": [candidates[0]["candidate_id"]],
            "latest_dev": "Updated.",
            "editorial_significance": "medium",
            "status": "active",
        }],
    }
    validated, warnings = digest._validate_editorial_proposal(
        proposal, candidates, [tracker_cdn],
        {"stories": [tracker_cdn]}, set(),
    )
    check(validated["selected_fresh"] == [], validated["selected_fresh"])
    check(validated["selected_ongoing"] == [], validated["selected_ongoing"])
    check(validated["story_state_proposals"] == [], validated["story_state_proposals"])
    check(any("asset-CDN fresh selection" in warning for warning in warnings), warnings)
    check(any("asset-CDN ongoing story" in warning for warning in warnings), warnings)

    # The floor must not fill a thin digest with an asset-CDN URL either.
    floor_proposal = {
        "selected_fresh": [], "selected_ongoing": [], "story_state_proposals": []
    }
    validated, _ = digest._validate_editorial_proposal(
        floor_proposal, candidates, [tracker_cdn],
        {"stories": [tracker_cdn]}, set(),
    )
    check(validated["selected_ongoing"] == [], validated["selected_ongoing"])


def test_proxy_5xx_retry_with_backoff() -> None:
    """A transient proxy 503 must be retried with backoff before the editorial
    stage falls back (digest-quality audit 2026-08-24: both Mimo calls 503'd on
    08-23 and the proposal skipped the critic entirely)."""
    class FakeResponse:
        def __init__(self, status_code: int, body: dict | None = None) -> None:
            self.status_code = status_code
            self._body = body

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code} Server Error")

        def json(self) -> dict:
            return self._body

    import requests  # noqa: PLC0415

    calls = []
    sleeps = []

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        if len(calls) <= 2:
            return FakeResponse(503)
        return FakeResponse(200, {"choices": [{"message": {"content": "reviewed ok"}}]})

    with patch("digest_runner.requests.post", side_effect=fake_post), \
         patch("digest_runner._detect_model_provider",
               return_value={"provider": "fake",
                             "chat_url": "http://proxy.test/v1/chat/completions"}), \
         patch("digest_runner.time.sleep", side_effect=lambda s: sleeps.append(s)):
        content = digest._call_llm_proxy("system", "user", model="mimo-v2.5")
        check(content == "reviewed ok", content)
        check(len(calls) == 3, f"503 was not retried: {len(calls)} calls")
        check(len(sleeps) == 2, f"backoff sleeps={sleeps}")
        check(sleeps == [digest.PROXY_5XX_BACKOFF_SECONDS,
                         digest.PROXY_5XX_BACKOFF_SECONDS * 2], sleeps)

    # Exhausted 5xx retries still propagate so the stage-level fallback can act.
    calls.clear()
    def always_503(url, json=None, timeout=None):
        calls.append(url)
        return FakeResponse(503)

    with patch("digest_runner.requests.post",
               side_effect=always_503), \
         patch("digest_runner._detect_model_provider",
               return_value={"provider": "fake",
                             "chat_url": "http://proxy.test/v1/chat/completions"}), \
         patch("digest_runner.time.sleep"):
        raised = False
        try:
            digest._call_llm_proxy("system", "user", model="mimo-v2.5")
        except requests.HTTPError:
            raised = True
        check(raised, "exhausted 503 did not raise")
        check(len(calls) == digest.PROXY_5XX_RETRIES + 1,
              f"503 retried {len(calls)} times")


def main() -> None:
    tests = [
        test_url_normalization,
        test_article_cache_contract,
        test_cross_topic_dedup_precedes_fetch_queue,
        test_cross_topic_same_event_referenced_url_dedup,
        test_phase_two_cross_day_dedup_window_contract,
        test_runtime_preflight_fails_closed_on_missing_symbol,
        test_attention_phase_persists_durable_observations,
        test_phase_three_uses_product_priority,
        test_phase_four_concurrency_and_shared_cache,
        test_editorial_validation_and_state_application,
        test_editorial_critic_patch_contract,
        test_editorial_drops_stale_fresh_selection,
        test_freshness_gate_rejects_future_dates,
        test_freshness_gate_ignores_future_event_date_confirmed,
        test_editorial_caps_source_concentration,
        test_editorial_proposal_retries_with_freshness_hint,
        test_critic_fresh_removal_honored_when_all_candidates_stale,
        test_critic_emptying_valid_fresh_still_fails_closed,
        test_phase_six_fallback_and_review_chain,
        test_stub_retry_preserves_failed_attempt_artifacts,
        test_editorial_proposal_retries_primary_before_fallback,
        test_editorial_critic_retries_primary_after_transient_error,
        test_critic_rejection_fails_closed,
        test_standfirst_boundary_and_deterministic_render,
        test_tracker_updates_require_material_evidence,
        test_developing_section_requires_significance_and_multiple_dates,
        test_followup_research_targets_prior_high_significance_stories,
        test_tracker_retention_uses_evidence_inactivity,
        test_ongoing_resurface_cap_cools_recurring_story,
        test_editorial_floor_and_publication_artifact,
        test_listing_urls_rejected,
        test_stub_attempts_cleaned_after_success,
        test_asset_cdn_urls_rejected,
        test_proxy_5xx_retry_with_backoff,
    ]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
