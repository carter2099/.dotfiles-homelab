#!/usr/bin/env python3
"""Behavioral contracts for observable news-attention and priority scoring."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from daily_news.attention import (
    observation_from_response,
    canonicalize_publisher_url,
    enforce_editorial_significance,
    event_terms,
    fetch_gdelt_attention,
    gdelt_query,
    normalize_editorial_significance,
    priority_sort_key,
    score_attention,
)


def check(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(message)


def test_gdelt_query_uses_explicit_or_alternatives() -> None:
    candidate = {
        "title": "Anthropic model release",
        "event_terms": [
            "Claude Fable 5.1",
            "Anthropic",
            "Fable release",
        ],
    }
    check(
        gdelt_query(candidate)
        == '("Claude Fable 5.1" OR Anthropic OR "Fable release")',
        gdelt_query(candidate),
    )
    check(
        gdelt_query({"title": "Nvidia unveils Rubin GPU platform"})
        == '"Nvidia unveils Rubin GPU"',
        "single title fallback must remain one exact phrase",
    )


def test_gdelt_rate_limit_honors_retry_after_and_paces_candidates() -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    candidate = {
        "title": "Company announces model",
        "url": "https://example.com/model",
        "editorial_significance": "medium",
        "event_terms": ["Company model", "model launch"],
    }

    class FakeResponse:
        def __init__(
            self,
            status_code: int,
            payload: dict | None = None,
            headers: dict | None = None,
        ) -> None:
            self.status_code = status_code
            self._payload = payload or {}
            self.headers = headers or {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self) -> dict:
            return self._payload

    responses = [
        FakeResponse(429, headers={"Retry-After": "45"}),
        FakeResponse(200, {"timeline": []}),
    ]
    calls: list[dict] = []
    retry_sleeps: list[float] = []

    def request_get(*_: object, **kwargs: object) -> FakeResponse:
        calls.append(kwargs)
        return responses.pop(0)

    observation = fetch_gdelt_attention(
        candidate,
        now,
        request_get=request_get,
        sleep=retry_sleeps.append,
    )
    check(observation["status"] == "no_matches", observation)
    check(len(calls) == 2, calls)
    check(retry_sleeps == [45.0], retry_sleeps)
    check(observation["_next_request_delay_seconds"] == 45.0, observation)

    paced_candidates = [
        {
            "title": f"Story {index}",
            "url": f"https://example.com/story-{index}",
            "editorial_significance": "medium",
            "event_terms": [f"Story {index}", f"event {index}"],
        }
        for index in range(3)
    ]
    pacing_sleeps: list[float] = []
    paced_calls: list[str] = []

    def no_matches(item: dict, observed_at: datetime) -> dict:
        paced_calls.append(item["url"])
        result = {
            "status": "no_matches",
            "provider": "GDELT DOC 2.0",
            "query": gdelt_query(item),
            "terms": event_terms(item),
            "observed_at": observed_at.isoformat(),
            "timeline": [],
        }
        if len(paced_calls) == 1:
            result["_next_request_delay_seconds"] = 45.0
        return result

    with tempfile.TemporaryDirectory() as temporary:
        score_attention(
            paced_candidates,
            Path(temporary),
            now=now,
            fetcher=no_matches,
            sleep=pacing_sleeps.append,
            request_interval=10.0,
        )
    check(pacing_sleeps == [45.0, 10.0], pacing_sleeps)


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
    observation = observation_from_response(
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
            "significance_evidence": {
                "basis": "broad_public_consequence",
                "affected_scope": "broad",
                "impact": "The consequential quiet event affects a broad public audience.",
            },
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
        "significance_evidence": {
            "basis": "binding_policy_or_law",
            "affected_scope": "broad",
            "impact": "The major policy change creates binding broad requirements.",
        },
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


def test_high_significance_requires_grounded_broad_impact() -> None:
    deprecated = {
        "title": "Codex MCP server command deprecated",
        "summary": "OpenAI deprecated the command and directed users to a replacement app server.",
        "editorial_significance": "high",
        "significance_evidence": {
            "basis": "widespread_mandatory_migration",
            "affected_scope": "sector",
            "impact": "Users of the deprecated command can move to the replacement app server.",
        },
    }
    enforce_editorial_significance(deprecated)
    check(deprecated["editorial_significance"] == "medium", deprecated)
    check(
        "lacks demonstrated broad impact"
        in deprecated["significance_validation"]["reason"],
        deprecated,
    )

    binding = {
        "title": "National regulator adopts binding AI safety rule",
        "summary": "The national regulator adopted a binding AI safety rule covering every provider.",
        "editorial_significance": "high",
        "significance_evidence": {
            "basis": "binding_policy_or_law",
            "affected_scope": "broad",
            "impact": "The binding AI safety rule covers every national provider.",
        },
    }
    enforce_editorial_significance(binding)
    check(binding["editorial_significance"] == "high", binding)
    check(binding["significance_validation"]["status"] == "accepted", binding)


def test_confirmed_no_matches_is_low_attention_not_neutral() -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    candidate = {
        "title": "Small developer tool update",
        "url": "https://example.com/tool",
        "editorial_significance": "medium",
        "event_terms": ["Developer tool", "small update"],
    }

    def no_matches(item: dict, observed_at: datetime) -> dict:
        return {
            "status": "no_matches",
            "provider": "GDELT DOC 2.0",
            "query": "\"Developer tool\" \"small update\"",
            "terms": item["event_terms"],
            "observed_at": observed_at.isoformat(),
            "timeline": [],
        }

    with tempfile.TemporaryDirectory() as temporary:
        scored, _ = score_attention(
            [candidate],
            Path(temporary),
            now=now,
            fetcher=no_matches,
            request_interval=0,
        )
    story = scored[0]
    check(story["attention"]["attention_now"] == 0.0, story)
    check(story["attention"]["digest_prominence"] == 0.0, story)
    check(story["attention"]["confidence"] == 0.55, story)
    check(story["priority_score"] < 60.0, story)


def test_priority_ties_use_evidence_not_discovery_order() -> None:
    lower = {
        "title": "Discovered first",
        "priority_score": 80.0,
        "editorial_significance": "high",
        "attention": {
            "digest_prominence": 60.0,
            "attention_now": 70.0,
            "confidence": 0.7,
        },
        "significance_evidence": {"affected_scope": "sector"},
    }
    higher = {
        "title": "Discovered second",
        "priority_score": 80.0,
        "editorial_significance": "medium",
        "attention": {
            "digest_prominence": 90.0,
            "attention_now": 80.0,
            "confidence": 0.8,
        },
        "significance_evidence": {"affected_scope": "broad"},
    }
    ranked = sorted([lower, higher], key=priority_sort_key, reverse=True)
    check(ranked[0]["title"] == "Discovered second", ranked)


def test_event_term_fallback_and_legacy_migration() -> None:
    item = {"title": "Nvidia unveils Rubin GPU platform", "importance": "high"}
    normalize_editorial_significance(item)
    check(item["editorial_significance"] == "high", item)
    check("importance" not in item, item)
    terms = event_terms(item)
    check(len(terms) == 1 and len(terms[0].split()) >= 3, terms)
    check("Nvidia" in terms[0], terms)


def test_canonicalize_publisher_url_maps_sample_hosts_only() -> None:
    canonical = canonicalize_publisher_url(
        "https://monorepo-sample1.nyt.net/2026/08/24/world/europe/"
        "russia-drones-autonomous-ai-kill-ukraine-war.html"
    )
    check(
        canonical
        == "https://www.nytimes.com/2026/08/24/world/europe/"
        "russia-drones-autonomous-ai-kill-ukraine-war.html",
        canonical,
    )
    check(
        canonicalize_publisher_url("https://sample2.nyt.net/story?ref=test")
        == "https://www.nytimes.com/story?ref=test",
        "query preserved",
    )
    for untouched in (
        "https://www.nytimes.com/2026/08/24/world/europe/story.html",
        "https://arstechnica.com/ai/2026/08/none",
        "https://blogs.nvidia.com/blog/2026/08/none",
        "",
        "not-a-url",
    ):
        check(canonicalize_publisher_url(untouched) == untouched, untouched)


def main() -> None:
    tests = [
        test_gdelt_query_uses_explicit_or_alternatives,
        test_gdelt_rate_limit_honors_retry_after_and_paces_candidates,
        test_gdelt_timeline_observation_and_syndication_dedup,
        test_attention_and_editorial_significance_remain_separate,
        test_unavailable_attention_falls_back_to_editorial_only,
        test_high_significance_requires_grounded_broad_impact,
        test_confirmed_no_matches_is_low_attention_not_neutral,
        test_priority_ties_use_evidence_not_discovery_order,
        test_event_term_fallback_and_legacy_migration,
        test_canonicalize_publisher_url_maps_sample_hosts_only,
    ]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
