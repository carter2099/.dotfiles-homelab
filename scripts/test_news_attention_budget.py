#!/usr/bin/env python3
"""Focused behavioral contracts for the optional attention-stage allowance."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile

from daily_news.attention import (
    MAX_ATTENTION_STAGE_BUDGET_SECONDS,
    fetch_gdelt_attention,
    gdelt_query,
    score_attention,
)


def check(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(message)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _candidate(title: str) -> dict[str, object]:
    return {
        "title": title,
        "url": f"https://example.com/{title.casefold().replace(' ', '-')}",
        "editorial_significance": "medium",
        "event_terms": [title, "release event"],
    }


def _no_matches(item: dict, observed_at: datetime) -> dict:
    return {
        "status": "no_matches",
        "provider": "GDELT DOC 2.0",
        "query": gdelt_query(item),
        "terms": item["event_terms"],
        "observed_at": observed_at.isoformat(),
        "timeline": [],
    }


def test_exact_budget_boundary_marks_remaining_candidates_unavailable() -> None:
    clock = FakeClock()
    calls: list[str] = []

    def fetch(item: dict, observed_at: datetime) -> dict:
        calls.append(item["title"])
        clock.value += 10.0
        return _no_matches(item, observed_at)

    candidates = [
        _candidate("First event"),
        _candidate("Second event"),
        _candidate("Third event"),
    ]
    with tempfile.TemporaryDirectory() as temporary:
        scored, artifact = score_attention(
            candidates,
            Path(temporary),
            now=datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc),
            fetcher=fetch,
            request_interval=0,
            budget_seconds=10,
            clock=clock,
        )

    check(calls == ["Third event"], calls)
    check(
        [item["title"] for item in scored]
        == ["First event", "Second event", "Third event"],
        scored,
    )
    check(artifact["available"] == 1 and artifact["unavailable"] == 2, artifact)
    check(artifact["budget_exhausted"] is True, artifact)
    check(artifact["budget_exhausted_candidates"] == 2, artifact)
    by_title = {item["title"]: item for item in scored}
    check(by_title["Third event"]["attention"]["status"] == "no_matches", by_title)
    for title in ("First event", "Second event"):
        item = by_title[title]
        check(item["attention"]["status"] == "unavailable", item)
        check(item["attention"]["confidence"] == 0.0, item)
        check(
            item["attention"]["evidence"]["unavailable_reason"]
            == "attention_stage_budget_exhausted",
            item,
        )
        check("budget expired" in item["priority_explanation"], item)


def test_budget_does_not_sleep_past_next_request_boundary() -> None:
    clock = FakeClock()
    calls: list[str] = []

    def fetch(item: dict, observed_at: datetime) -> dict:
        calls.append(item["title"])
        clock.value += 1.0
        return _no_matches(item, observed_at)

    candidates = [
        _candidate("Second event"),
        _candidate("First event"),
    ]
    with tempfile.TemporaryDirectory() as temporary:
        _scored, artifact = score_attention(
            candidates,
            Path(temporary),
            now=datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc),
            fetcher=fetch,
            sleep=clock.sleep,
            request_interval=5,
            budget_seconds=5,
            clock=clock,
        )

    check(calls == ["Second event"], calls)
    check(artifact["elapsed_seconds"] == 1.0, artifact)
    check(artifact["budget_exhausted_candidates"] == 1, artifact)


def test_retry_after_cannot_cross_deadline() -> None:
    clock = FakeClock()
    calls: list[dict] = []

    class RetryResponse:
        status_code = 429
        headers = {"Retry-After": "600"}

        def raise_for_status(self) -> None:
            raise AssertionError("429 response should be handled before raise_for_status")

        def json(self) -> dict:
            return {}

    def request_get(*_args: object, **kwargs: object) -> RetryResponse:
        calls.append(kwargs)
        return RetryResponse()

    with tempfile.TemporaryDirectory() as temporary:
        observation = fetch_gdelt_attention(
            _candidate("Retry event"),
            datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc),
            request_get=request_get,
            sleep=clock.sleep,
            deadline=5,
            clock=clock,
            budget_seconds=5,
        )

    check(len(calls) == 1, calls)
    check(calls[0]["timeout"] == 5.0, calls)
    check(clock.sleeps == [], clock.sleeps)
    check(observation["status"] == "unavailable", observation)
    check(
        observation["unavailable_reason"] == "attention_stage_budget_exhausted",
        observation,
    )
    check("budget exhausted" in observation["error"], observation)


def test_attention_budget_has_a_hard_upper_bound() -> None:
    try:
        with tempfile.TemporaryDirectory() as temporary:
            score_attention(
                [_candidate("Bounded event")],
                Path(temporary),
                budget_seconds=MAX_ATTENTION_STAGE_BUDGET_SECONDS + 1,
                request_interval=0,
            )
    except ValueError as error:
        check("attention budget" in str(error), error)
    else:
        raise AssertionError("attention budget accepted a value above its hard ceiling")


def main() -> None:
    tests = [
        test_exact_budget_boundary_marks_remaining_candidates_unavailable,
        test_budget_does_not_sleep_past_next_request_boundary,
        test_retry_after_cannot_cross_deadline,
        test_attention_budget_has_a_hard_upper_bound,
    ]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
