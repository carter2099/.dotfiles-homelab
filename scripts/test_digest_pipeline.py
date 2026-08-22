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
                "importance": "high",
                "date_published": "2026-08-10",
            },
            {
                "title": "Unique",
                "url": "https://example.com/unique",
                "importance": "medium",
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
            "importance": "high",
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
            "importance": "medium",
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
        "importance": "medium",
        "first_seen": "2026-08-08",
        "last_updated": "2026-08-08",
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
                "importance": "high",
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
    check(len(validated["story_state_proposals"]) == 1, validated)
    check(
        validated["balance_summary"]
        == "Validated selection: 2 fresh, 0 ongoing; 1 source domain(s); "
           "categories: Research.",
        validated["balance_summary"],
    )
    check(any("unknown candidate_id" in warning for warning in warnings), warnings)
    check(
        any("cross-story tracker update" in warning for warning in warnings),
        warnings,
    )
    original = json.loads(json.dumps(tracker))
    updated = digest._apply_story_state_proposals(
        tracker, validated, candidates, "2026-08-10"
    )
    check(tracker == original, "state application mutated its input")
    check(updated["stories"][0]["latest_dev"] == "New verified development.", updated)
    check(updated["stories"][0]["last_updated"] == "2026-08-10", updated)


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
    """Ongoing-window candidates must never ship under "Fresh — Last 24 Hours"
    (digest-quality audit 2026-08-12: agentic-platform shipped a 5d-old Claude
    Code story and ai-hardware a 2d-old RTX story under Fresh). A stale-dropped
    candidate is treated as unselected, so it gets no tracker add either."""
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
            "importance": "high",
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
    # The stale-dropped candidate is still treated as unselected: it gets no
    # tracker add. The deterministic floor fills the digest with the
    # pre-existing active SIF story (digest-quality audit 2026-08-21), so the
    # only state op is the synthesized tracker touch for that story.
    check(
        validated["story_state_proposals"]
        == [{
            "operation": "update",
            "story_url": "https://example.com/existing",
            "evidence_candidate_ids": [],
            "latest_dev": "Previous development.",
            "importance": "medium",
            "status": "active",
        }],
        validated["story_state_proposals"],
    )
    check(
        not any(
            op.get("operation") == "add" and op.get("candidate_id") == stale["candidate_id"]
            for op in validated["story_state_proposals"]
        ),
        "stale-dropped candidate received a tracker add",
    )
    updated = digest._apply_story_state_proposals(
        tracker, validated, candidates, "2026-08-10"
    )
    check(
        not any(story.get("url") == stale["url"] for story in updated["stories"]),
        "stale-dropped candidate entered the tracker",
    )
    check(
        updated["stories"][0]["last_updated"] == "2026-08-10",
        "floor-filled ongoing story was not tracker-touched",
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
            "importance": "high" if index == 0 else "medium",
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
            "importance": "medium",
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

        def build(title: str, url: str, day: str, importance: str) -> dict:
            return {
                "title": title,
                "url": url,
                "source_domain": "example.com",
                "summary": f"{title} verified summary.",
                "category": "Research",
                "importance": importance,
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
                "importance": "high",
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
            "importance": "high",
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
                "importance": "high",
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
                "selection_reason": "Highest importance.",
                "related_story_url": None,
            }],
            "selected_ongoing": [],
            "story_state_proposals": [{
                "operation": "add",
                "candidate_id": selected_id,
                "evidence_candidate_ids": [selected_id],
                "latest_dev": "Reviewed factual summary.",
                "importance": "high",
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
                "selection_reason": "Highest importance.",
                "related_story_url": None,
            }],
            "selected_ongoing": [],
            "story_state_proposals": [{
                "operation": "add",
                "candidate_id": selected_id,
                "evidence_candidate_ids": [selected_id],
                "latest_dev": "Retried primary summary.",
                "importance": "high",
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
                "selection_reason": "Highest importance.",
                "related_story_url": None,
            }],
            "selected_ongoing": [],
            "story_state_proposals": [{
                "operation": "add",
                "candidate_id": selected_id,
                "evidence_candidate_ids": [selected_id],
                "latest_dev": "Reviewed factual summary.",
                "importance": "high",
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
                "importance": "high",
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
        # The rejected model proposal's state change must not be applied, but the
        # source-ranked fallback now records today's selected stories in the
        # tracker so a curation-model outage no longer freezes stories-in-flight
        # (digest-quality audit fix).
        check(
            not any(
                story.get("latest_dev") == "Proposed summary."
                for story in updated["stories"]
            ),
            "rejected state proposal was applied",
        )
        # The source-ranked fallback records today's selected stories in the
        # tracker (steward digest-quality contract); compare against the real
        # run date so the test is deterministic on any day.
        today = datetime.now().strftime("%Y-%m-%d")
        added = {
            story.get("url"): story
            for story in updated["stories"]
            if story.get("first_seen") == today
        }
        check(len(added) == 2, f"fallback did not track both fresh stories: {added}")
        check(
            all(story.get("last_updated") == today for story in added.values()),
            added,
        )
        check(
            artifact["editorial"]["review_status"] == "rejected_fallback",
            artifact,
        )


def test_intro_boundary_and_deterministic_render() -> None:
    stories = [{"title": "Safe <Title>", "summary": "Verified 12% result."}]
    valid, _ = digest._validate_intro(
        "Today’s result reached 12%. The verified change leads the digest.", stories
    )
    check(valid, "source-backed intro was rejected")
    valid, reason = digest._validate_intro(
        "Today’s result reached 99%. The change leads the digest.", stories
    )
    check(not valid and "99" in reason, reason)
    valid, reason = digest._validate_intro(
        "UnknownAI leads today’s digest. The verified change follows.", stories
    )
    check(not valid and "unknownai" in reason, reason)

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
        {"title": "Test Digest"}, fresh, ongoing, "Approved intro."
    )
    check("Safe &lt;Title&gt;" in rendered, "title was not escaped")
    check('href="https://example.com/story?a=1&amp;b=2"' in rendered,
          "URL was not safely rendered")
    check("↳ New evidence." in rendered, "ongoing rationale missing")
    check("{{FRESH_STORIES}}" not in rendered, "template placeholder remained")
    check("STORY BLOCK TEMPLATE" not in rendered, "template instructions leaked")


def test_tracker_touch_updates_last_updated_for_ongoing_selection() -> None:
    """A tracker story selected into Ongoing without a model update op must get
    a deterministic tracker touch so last_updated advances (digest-quality
    audit 2026-08-21: the 404 Media rare-books story and the OpenAI Agents JS
    guide resurfaced 08-19→08-21 while last_updated stayed 2026-08-18)."""
    candidates, sif_candidates, tracker = editorial_fixture()
    proposal = {
        "selected_fresh": [
            {"candidate_id": candidates[0]["candidate_id"]},
            {"candidate_id": candidates[1]["candidate_id"]},
        ],
        "selected_ongoing": [{
            "story_url": "https://example.com/existing",
            "summary": "Still relevant summary.",
            "why_still_relevant": "Story is still developing.",
        }],
        "story_state_proposals": [],
    }
    validated, _ = digest._validate_editorial_proposal(
        proposal, candidates, sif_candidates, tracker
    )
    touches = [
        op for op in validated["story_state_proposals"]
        if op["operation"] == "update"
    ]
    check(len(touches) == 1, validated["story_state_proposals"])
    check(
        touches[0]["story_url"] == "https://example.com/existing"
        and touches[0]["latest_dev"] == "Previous development."
        and touches[0]["status"] == "active",
        touches,
    )
    original = json.loads(json.dumps(tracker))
    updated = digest._apply_story_state_proposals(
        tracker, validated, candidates, "2026-08-10"
    )
    check(tracker == original, "state application mutated its input")
    story = updated["stories"][0]
    check(story["last_updated"] == "2026-08-10", story)
    check(story["latest_dev"] == "Previous development.", story)

    # A model-supplied update op suppresses the synthesized touch (no dupes).
    # Evidence must be a same-story candidate (digest-quality audit 2026-08-22).
    same_story = {
        **candidates[0],
        "title": "Existing narrative update",
        "url": "https://example.com/existing",
        "candidate_id": "candidate-same-story",
    }
    candidates.append(same_story)
    with_update = copy.deepcopy(proposal)
    with_update["selected_fresh"] = [{"candidate_id": same_story["candidate_id"]}]
    with_update["story_state_proposals"] = [{
        "operation": "update",
        "story_url": "https://example.com/existing",
        "evidence_candidate_ids": [same_story["candidate_id"]],
        "latest_dev": "Model-verified development.",
        "importance": "high",
        "status": "active",
    }]
    validated, _ = digest._validate_editorial_proposal(
        with_update, candidates, sif_candidates, tracker
    )
    update_ops = [
        op for op in validated["story_state_proposals"]
        if op["operation"] == "update"
    ]
    check(len(update_ops) == 1, validated["story_state_proposals"])
    check(
        update_ops[0]["latest_dev"] == "Model-verified development.",
        update_ops,
    )


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
        "importance": "medium",
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
            proposal, tracker, digest_dir
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
                "importance": "medium",
                "status": "active",
            }],
        }
        warnings, ops = digest._enforce_ongoing_resurface_cap(
            evidenced, tracker, digest_dir
        )
        check(len(evidenced["selected_ongoing"]) == 1, evidenced["selected_ongoing"])
        check(warnings == [] and ops == [], (warnings, ops))


def test_editorial_floor_and_thin_send_guard() -> None:
    """A selection below two stories must be filled from the active SIF pool
    when available, and a digest that stays below two stories must not be
    emailed (digest-quality audit 2026-08-21: agentic-platform 08-20 and
    gaming-digest 08-20 each shipped a single-link digest)."""
    candidates, sif_candidates, tracker = editorial_fixture()
    second_sif = {
        "title": "Second tracked story",
        "url": "https://second.example/ongoing",
        "category": "Policy",
        "latest_dev": "Second development.",
        "status": "active",
        "importance": "low",
        "first_seen": "2026-08-09",
        "last_updated": "2026-08-09",
    }
    sif_candidates.append(second_sif)

    # 1 fresh + 0 ongoing → floor fills the most-recently-updated SIF story.
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

    # 0 fresh + 0 ongoing and an empty pool: the digest stays below two and the
    # Phase 8 guard archives instead of emailing.
    empty_validated, _ = digest._validate_editorial_proposal(
        {"selected_fresh": [], "selected_ongoing": [], "story_state_proposals": []},
        candidates, [], {"stories": []}, set(),
    )
    check(not empty_validated["selected_fresh"] and not empty_validated["selected_ongoing"],
          empty_validated)

    with tempfile.TemporaryDirectory() as temporary:
        digest_dir = Path(temporary) / "world-digest"
        run_dir = Path(temporary) / "run"
        digest_dir.mkdir(parents=True)
        run_dir.mkdir(parents=True)

        calls: list[object] = []

        def fake_send(*_args: object, **_kwargs: object) -> object:
            calls.append(_args)
            return type("Result", (), {"returncode": 0, "stderr": ""})()

        html = "<html>thin</html>"
        with patch("digest_runner.subprocess.run", side_effect=fake_send):
            digest.phase_8_send_archive(
                digest.TOPICS["world"], html, {"stories": []}, run_dir, digest_dir,
                fresh=[], ongoing=[{"url": "https://example.com/only"}],
            )
        check(not calls, f"thin digest was emailed: {calls}")
        check((digest_dir / f"{datetime.now():%Y-%m-%d}.html").exists(),
              "thin digest was not archived")

        calls.clear()
        second_dir = Path(temporary) / "world-digest-two"
        second_run = Path(temporary) / "run-two"
        second_dir.mkdir(parents=True)
        second_run.mkdir(parents=True)
        with patch("digest_runner.subprocess.run", side_effect=fake_send):
            digest.phase_8_send_archive(
                digest.TOPICS["world"], html, {"stories": []}, second_run, second_dir,
                fresh=[{"url": "https://example.com/a"}],
                ongoing=[{"url": "https://example.com/b"}],
            )
        check(len(calls) == 1, f"two-story digest was not emailed: {calls}")


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
            "importance": "high",
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
        "importance": "medium",
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
            "importance": "medium",
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


def main() -> None:
    tests = [
        test_url_normalization,
        test_article_cache_contract,
        test_cross_topic_dedup_precedes_fetch_queue,
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
        test_intro_boundary_and_deterministic_render,
        test_tracker_touch_updates_last_updated_for_ongoing_selection,
        test_ongoing_resurface_cap_cools_recurring_story,
        test_editorial_floor_and_thin_send_guard,
        test_listing_urls_rejected,
    ]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
