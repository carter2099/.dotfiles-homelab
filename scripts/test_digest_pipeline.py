#!/usr/bin/env python3
"""Focused behavioral fixtures for digest dedup, cache, editorial, and rendering."""
from __future__ import annotations

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
    candidates, _ = digest._prepare_editorial_candidates([
        {
            "title": "Primary story",
            "url": "https://example.com/primary",
            "source_domain": "example.com",
            "summary": "Primary verified summary.",
            "category": "Research",
            "importance": "high",
            "date_published": "2026-08-10",
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
            "date_published": "2026-08-10",
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
    proposal = {
        "selected_fresh": [
            {"candidate_id": first_id, "editorial_summary": "Approved summary."},
            {"candidate_id": "candidate-unknown", "editorial_summary": "Bad."},
            {"candidate_id": first_id, "editorial_summary": "Duplicate."},
        ],
        "selected_ongoing": [{
            "story_url": "https://example.com/existing",
            "summary": "Existing narrative summary.",
            "why_still_relevant": "A selected article adds evidence.",
        }],
        "story_state_proposals": [
            {
                "operation": "update",
                "story_url": "https://example.com/existing",
                "evidence_candidate_ids": [first_id],
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
        ],
    }
    validated, warnings = digest._validate_editorial_proposal(
        proposal, candidates, sif_candidates, tracker
    )
    check(len(validated["selected_fresh"]) == 1, validated)
    check(len(validated["story_state_proposals"]) == 1, validated)
    check(
        validated["balance_summary"]
        == "Validated selection: 1 fresh, 1 ongoing; 1 source domain(s); "
           "categories: Research.",
        validated["balance_summary"],
    )
    check(any("unknown candidate_id" in warning for warning in warnings), warnings)
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
        check(not responses, f"unused model responses: {responses!r}")


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
        added = {
            story.get("url"): story
            for story in updated["stories"]
            if story.get("first_seen") == "2026-08-10"
        }
        check(len(added) == 2, f"fallback did not track both fresh stories: {added}")
        check(
            all(story.get("last_updated") == "2026-08-10" for story in added.values()),
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


def main() -> None:
    tests = [
        test_url_normalization,
        test_article_cache_contract,
        test_cross_topic_dedup_precedes_fetch_queue,
        test_phase_four_concurrency_and_shared_cache,
        test_editorial_validation_and_state_application,
        test_editorial_critic_patch_contract,
        test_phase_six_fallback_and_review_chain,
        test_critic_rejection_fails_closed,
        test_intro_boundary_and_deterministic_render,
    ]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
