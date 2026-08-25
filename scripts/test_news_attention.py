#!/usr/bin/env python3
"""Behavioral contracts for observable news-attention and priority scoring."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from news_attention import (
    _observation_from_response,
    event_terms,
    normalize_editorial_significance,
    score_attention,
)


def check(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(message)


def test_gdelt_timeline_observation_and_syndication_dedup() -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    data = []
    for index, value in enumerate((0.05, 0.10, 0.15, 0.20, 0.40, 0.60, 0.80)):
        minute = 15 * index
        hour = 10 + minute // 60
        minute = minute % 60
        toparts = []
        if index == 6:
            toparts = [
                {"url": "https://one.example/story", "title": "Company closes major AI deal"},
                {"url": "https://two.example/copy", "title": "Company closes a major AI deal"},
                {"url": "https://three.example/analysis", "title": "Rival responds to company AI deal"},
            ]
        data.append({
            "date": f"20260825T{hour:02d}{minute:02d}00Z",
            "value": value,
            "toparts": toparts,
        })
    candidate = {
        "title": "Company closes major AI deal",
        "event_terms": ["Company", "AI deal"],
    }
    observation = _observation_from_response(
        candidate,
        {"timeline": [{"series": "Volume Intensity", "data": data}]},
        now,
    )
    check(observation["status"] == "ok", observation)
    check(observation["peak_coverage_share"] == 0.8, observation)
    check(observation["coverage_velocity_1h"] > 0, observation)
    check(observation["distinct_publishers"] == 3, observation)
    check(observation["independent_source_groups"] == 2, observation)
    check(observation["age_bucket"] in {"1-3h", "3-6h"}, observation)
    check(len(observation["timeline"]) == 96, observation)


def test_attention_and_editorial_significance_remain_separate() -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    candidates = [
        {
            "title": "Consequential but quiet event",
            "url": "https://example.com/high",
            "importance": "high",
            "event_terms": ["Consequential", "quiet event"],
        },
        {
            "title": "Fast breaking product event",
            "url": "https://example.com/breakout",
            "editorial_significance": "medium",
            "event_terms": ["Product", "breaking event"],
        },
        {
            "title": "Routine quiet update",
            "url": "https://example.com/quiet",
            "editorial_significance": "medium",
            "event_terms": ["Routine", "quiet update"],
        },
    ]

    def observation(candidate: dict, observed_at: datetime) -> dict:
        breakout = "breaking" in candidate["title"].casefold()
        quiet = "Routine" in candidate["title"]
        multiplier = 10 if breakout else (1 if quiet else 2)
        return {
            "status": "ok",
            "provider": "GDELT DOC 2.0",
            "query": " ".join(event_terms(candidate)),
            "terms": event_terms(candidate),
            "observed_at": observed_at.isoformat(),
            "first_observed_at": "2026-08-25T08:00:00+00:00",
            "age_hours": 4.0,
            "age_bucket": "3-6h",
            "peak_coverage_share": 0.1 * multiplier,
            "mean_coverage_share": 0.03 * multiplier,
            "current_coverage_share": 0.08 * multiplier,
            "coverage_velocity_1h": 0.02 * multiplier,
            "current_momentum": 0.01 * multiplier,
            "distinct_publishers": multiplier,
            "independent_source_groups": multiplier,
            "sampled_articles": multiplier,
            "data_lag_minutes": 15.0,
            "timeline": [],
        }

    with tempfile.TemporaryDirectory() as temporary:
        cache = Path(temporary)
        scored, artifact = score_attention(
            candidates,
            cache,
            now=now,
            fetcher=observation,
            request_interval=0,
        )
        by_url = {item["url"]: item for item in scored}
        high = by_url["https://example.com/high"]
        breakout = by_url["https://example.com/breakout"]
        quiet = by_url["https://example.com/quiet"]
        check("importance" not in high, high)
        check(high["editorial_significance"] == "high", high)
        check(breakout["editorial_significance"] == "medium", breakout)
        check(
            breakout["attention"]["digest_prominence"]
            > quiet["attention"]["digest_prominence"],
            scored,
        )
        check(breakout["priority_score"] > quiet["priority_score"], scored)
        check(high["priority_score"] > quiet["priority_score"], scored)
        check(artifact["available"] == 3 and artifact["unavailable"] == 0, artifact)
        check("llm_popularity_score" not in str(artifact), artifact)

        calls = []

        def must_not_fetch(candidate: dict, observed_at: datetime) -> dict:
            calls.append(candidate)
            raise AssertionError("cache miss")

        cached, cached_artifact = score_attention(
            candidates,
            cache,
            now=now,
            fetcher=must_not_fetch,
            request_interval=0,
        )
        check(not calls, calls)
        check(cached_artifact["cache_hits"] == 3, cached_artifact)
        check([item["priority_score"] for item in cached]
              == [item["priority_score"] for item in scored], cached)


def test_unavailable_attention_falls_back_to_editorial_only() -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    candidate = {
        "title": "Major policy change",
        "url": "https://example.com/policy",
        "editorial_significance": "high",
        "event_terms": ["Major policy", "government action"],
    }

    def unavailable(item: dict, observed_at: datetime) -> dict:
        return {
            "status": "unavailable",
            "provider": "GDELT DOC 2.0",
            "query": "Major policy government action",
            "terms": item["event_terms"],
            "observed_at": observed_at.isoformat(),
            "error": "HTTP 429",
        }

    with tempfile.TemporaryDirectory() as temporary:
        scored, artifact = score_attention(
            [candidate],
            Path(temporary),
            now=now,
            fetcher=unavailable,
            request_interval=0,
        )
    check(scored[0]["attention"]["confidence"] == 0.0, scored)
    check(scored[0]["priority_score"] == 100.0, scored)
    check(artifact["unavailable"] == 1, artifact)


def test_event_term_fallback_and_legacy_migration() -> None:
    item = {"title": "Nvidia unveils Rubin GPU platform", "importance": "high"}
    normalize_editorial_significance(item)
    check(item["editorial_significance"] == "high", item)
    check("importance" not in item, item)
    terms = event_terms(item)
    check(len(terms) == 1 and len(terms[0].split()) >= 3, terms)
    check("Nvidia" in terms[0], terms)


def main() -> None:
    tests = [
        test_gdelt_timeline_observation_and_syndication_dedup,
        test_attention_and_editorial_significance_remain_separate,
        test_unavailable_attention_falls_back_to_editorial_only,
        test_event_term_fallback_and_legacy_migration,
    ]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
