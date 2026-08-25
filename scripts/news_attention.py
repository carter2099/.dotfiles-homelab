#!/usr/bin/env python3
"""Observable news-attention scoring for Daily News candidates.

LLMs may supply canonical event terms, but every attention score comes from
measured news coverage. Editorial significance remains a separate input.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

SCHEMA_VERSION = 2
PROVIDER = "GDELT DOC 2.0"
GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
CACHE_TTL_HOURS = 6
REQUEST_INTERVAL_SECONDS = 6.0
REQUEST_TIMEOUT_SECONDS = 45

EDITORIAL_POINTS = {
    "high": 100.0,
    "medium": 60.0,
    "low": 25.0,
}

HIGH_SIGNIFICANCE_BASES = {
    "binding_policy_or_law",
    "broad_public_consequence",
    "major_conflict_or_disaster",
    "major_financial_scale",
    "major_product_or_platform_shift",
    "security_or_safety_incident",
    "widespread_mandatory_migration",
}

IMPACT_SCOPE_POINTS = {
    "broad": 2.0,
    "sector": 1.0,
    "niche": 0.0,
}

_STOPWORDS = {
    "about", "after", "again", "against", "amid", "among", "and", "are",
    "before", "being", "between", "could", "does", "from", "have", "how",
    "into", "just", "more", "new", "not", "over", "says", "than", "that",
    "their", "they", "this", "through", "under", "what", "when", "where",
    "which", "who", "why", "with", "will", "would", "your",
}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def normalize_editorial_significance(item: dict[str, Any]) -> dict[str, Any]:
    """Migrate the old `importance` field and enforce the editorial label."""
    legacy = item.pop("importance", None)
    value = _clean_text(item.get("editorial_significance") or legacy).lower()
    item["editorial_significance"] = value if value in EDITORIAL_POINTS else "medium"
    return item

def _significance_tokens(value: Any) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", _clean_text(value))
        if len(token) >= 3 and token.casefold() not in _STOPWORDS
    }


def _downgrade_high(item: dict[str, Any], reason: str) -> dict[str, Any]:
    item["editorial_significance"] = "medium"
    item["significance_validation"] = {
        "status": "downgraded",
        "from": "high",
        "to": "medium",
        "reason": reason,
    }
    return item


def enforce_editorial_significance(item: dict[str, Any]) -> dict[str, Any]:
    """Require source-grounded impact evidence before accepting `high`."""
    normalize_editorial_significance(item)
    if item["editorial_significance"] != "high":
        item.setdefault("significance_validation", {
            "status": "accepted",
            "reason": "high-significance gate not required",
        })
        return item

    evidence = item.get("significance_evidence")
    if not isinstance(evidence, dict):
        return _downgrade_high(item, "missing structured significance evidence")
    basis = _clean_text(evidence.get("basis")).lower()
    scope = _clean_text(evidence.get("affected_scope")).lower()
    impact = _clean_text(evidence.get("impact"))
    if basis not in HIGH_SIGNIFICANCE_BASES:
        return _downgrade_high(item, "unsupported high-significance basis")
    if scope not in {"broad", "sector"}:
        return _downgrade_high(item, "high significance requires broad or sector scope")
    if len(impact) < 30:
        return _downgrade_high(item, "impact evidence is missing or too vague")

    source_text = " ".join([
        _clean_text(item.get("title")),
        _clean_text(item.get("summary")),
        " ".join(
            _clean_text(detail)
            for detail in item.get("key_details", [])
            if isinstance(detail, str)
        ),
    ])
    impact_tokens = _significance_tokens(impact)
    source_tokens = _significance_tokens(source_text)
    required_overlap = min(3, len(impact_tokens))
    if required_overlap == 0 or len(impact_tokens & source_tokens) < required_overlap:
        return _downgrade_high(item, "impact evidence is not grounded in source facts")
    source_numbers = set(re.findall(r"\b\d[\d,.]*%?\b", source_text))
    impact_numbers = set(re.findall(r"\b\d[\d,.]*%?\b", impact))
    if not impact_numbers.issubset(source_numbers):
        return _downgrade_high(item, "impact evidence introduced unsupported numbers")

    maintenance_change = bool(re.search(
        r"\b(deprecat\w*|renam\w*|remov(?:e|ed|al)|patch(?:es)?|"
        r"release notes?|minor update|version bump)\b",
        source_text,
        re.IGNORECASE,
    ))
    high_impact_exception = basis in {
        "binding_policy_or_law",
        "security_or_safety_incident",
    }
    demonstrated_scale = bool(re.search(
        r"\b(all|every|millions?|thousands?|widely used|critical|outage|"
        r"data loss|no replacement|breaking existing|mandatory|deadline)\b|"
        r"\b\d[\d,.]*\s*(users?|customers?|organizations?|systems?)\b",
        source_text,
        re.IGNORECASE,
    ))
    if maintenance_change and not high_impact_exception and not demonstrated_scale:
        return _downgrade_high(
            item,
            "routine maintenance or deprecation lacks demonstrated broad impact",
        )

    item["significance_validation"] = {
        "status": "accepted",
        "reason": f"{basis} with {scope} affected scope",
    }
    return item


def _sanitize_term(value: Any) -> str:
    term = _clean_text(value)
    term = re.sub(r"[^\w\s.+#&/'-]", " ", term, flags=re.UNICODE)
    term = _clean_text(term).strip("-/'")
    return term[:64]


def _terms_are_sufficient(terms: list[str]) -> bool:
    return (
        len(terms) >= 2
        or (
            len(terms) == 1
            and len(re.findall(r"\w+", terms[0], flags=re.UNICODE)) >= 3
        )
    )


def event_terms(candidate: dict[str, Any]) -> list[str]:
    """Return bounded event identifiers for measurement, never a score."""
    supplied = candidate.get("event_terms")
    terms: list[str] = []
    if isinstance(supplied, list):
        for value in supplied:
            term = _sanitize_term(value)
            if (
                term
                and not (
                    " " not in term
                    and term.casefold() in _STOPWORDS
                )
                and term.casefold()
                not in {existing.casefold() for existing in terms}
            ):
                terms.append(term)
            if len(terms) == 4:
                break
    if _terms_are_sufficient(terms):
        return terms

    source = _clean_text(candidate.get("event") or candidate.get("title"))
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9.+#&'-]*", source)
    while tokens and tokens[0].casefold() in {"a", "an", "the"}:
        tokens.pop(0)
    phrase = _sanitize_term(" ".join(tokens[:4]))
    return [phrase] if len(phrase.split()) >= 3 else terms

def event_term_source(candidate: dict[str, Any]) -> str:
    supplied = candidate.get("event_terms")
    if isinstance(supplied, list):
        cleaned = [
            term for term in (_sanitize_term(value) for value in supplied)
            if term and not (" " not in term and term.casefold() in _STOPWORDS)
        ]
        if _terms_are_sufficient(cleaned):
            return "model_terms"
    return "title_fallback"


def gdelt_query(candidate: dict[str, Any]) -> str:
    parts = []
    for term in event_terms(candidate):
        parts.append(f'"{term}"' if " " in term else term)
    return " ".join(parts[:4])


def _parse_gdelt_time(value: Any) -> datetime | None:
    text = _clean_text(value)
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _normalized_title(value: Any) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", _clean_text(value))
        if len(token) >= 3 and token.casefold() not in _STOPWORDS
    }


def _independent_source_groups(articles: list[dict[str, Any]]) -> int:
    """Collapse near-identical wire/syndicated headlines into one source group."""
    groups: list[set[str]] = []
    for article in articles:
        tokens = _normalized_title(article.get("title"))
        if not tokens:
            continue
        duplicate = False
        for existing in groups:
            union = tokens | existing
            if union and len(tokens & existing) / len(union) >= 0.78:
                duplicate = True
                break
        if not duplicate:
            groups.append(tokens)
    return len(groups)

def _sample_relevance(
    candidate: dict[str, Any], articles: list[dict[str, Any]],
) -> float | None:
    if not articles:
        return None
    candidate_tokens = _normalized_title(
        f"{candidate.get('title', '')} {candidate.get('event', '')} "
        f"{' '.join(event_terms(candidate))}"
    )
    if not candidate_tokens:
        return None
    relevant = 0
    for article in articles:
        article_tokens = _normalized_title(article.get("title"))
        if len(candidate_tokens & article_tokens) >= 2:
            relevant += 1
    return relevant / len(articles)


def _age_bucket(age_hours: float | None) -> str:
    if age_hours is None:
        return "unknown"
    if age_hours <= 1:
        return "0-1h"
    if age_hours <= 3:
        return "1-3h"
    if age_hours <= 6:
        return "3-6h"
    if age_hours <= 12:
        return "6-12h"
    return "12-24h"


def _observation_from_response(
    candidate: dict[str, Any], payload: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    query = gdelt_query(candidate)
    timeline = payload.get("timeline", []) if isinstance(payload, dict) else []
    data = []
    if timeline and isinstance(timeline[0], dict):
        data = timeline[0].get("data", [])
    if not isinstance(data, list) or not data:
        return {
            "status": "no_matches",
            "provider": PROVIDER,
            "query": query,
            "terms": event_terms(candidate),
            "observed_at": now.isoformat(),
            "first_observed_at": "",
            "age_hours": None,
            "age_bucket": "unknown",
            "peak_coverage_share": 0.0,
            "mean_coverage_share": 0.0,
            "current_coverage_share": 0.0,
            "coverage_velocity_1h": 0.0,
            "current_momentum": 0.0,
            "distinct_publishers": 0,
            "independent_source_groups": 0,
            "sampled_articles": 0,
            "data_lag_minutes": None,
            "timeline": [],
        }

    point_map: dict[datetime, dict[str, Any]] = {}
    articles: list[dict[str, Any]] = []
    for raw_point in data:
        if not isinstance(raw_point, dict):
            continue
        timestamp = _parse_gdelt_time(raw_point.get("date"))
        if timestamp is None:
            continue
        try:
            value = max(0.0, float(raw_point.get("value", 0.0)))
        except (TypeError, ValueError):
            value = 0.0
        toparts = raw_point.get("toparts", [])
        if not isinstance(toparts, list):
            toparts = []
        clean_articles = [article for article in toparts if isinstance(article, dict)]
        articles.extend(clean_articles)
        point_map[timestamp] = {
            "date": timestamp.strftime("%Y%m%dT%H%M%SZ"),
            "value": value,
            "toparts": clean_articles,
        }

    if not point_map:
        return _observation_from_response(candidate, {}, now)

    latest = max(point_map)
    latest = latest.replace(
        minute=(latest.minute // 15) * 15, second=0, microsecond=0
    )
    slots = [latest - timedelta(minutes=15 * offset) for offset in reversed(range(96))]
    values = [float(point_map.get(slot, {}).get("value", 0.0)) for slot in slots]
    positive_slots = [slot for slot, value in zip(slots, values) if value > 0]
    first_seen = min(positive_slots) if positive_slots else None
    age_hours = (
        max(0.0, (now - first_seen).total_seconds() / 3600)
        if first_seen is not None else None
    )

    current_hour = sum(values[-4:]) / 4
    previous_hour = sum(values[-8:-4]) / 4
    prior_three_hours = sum(values[-16:-4]) / 12
    coverage_velocity = max(0.0, current_hour - previous_hour)
    momentum = max(0.0, current_hour - prior_three_hours)
    domains = {
        (urlsplit(_clean_text(article.get("url"))).hostname or "").removeprefix("www.")
        for article in articles
        if _clean_text(article.get("url"))
    }
    domains.discard("")

    sample_relevance = _sample_relevance(candidate, articles)
    low_quality = (
        len(articles) >= 5
        and sample_relevance is not None
        and sample_relevance < 0.20
    )
    compact_timeline = [
        {
            "date": slot.strftime("%Y%m%dT%H%M%SZ"),
            "value": round(value, 6),
        }
        for slot, value in zip(slots, values)
    ]
    return {
        "status": "unavailable" if low_quality else "ok",
        "provider": PROVIDER,
        "query": query,
        "terms": event_terms(candidate),
        "observed_at": now.isoformat(),
        "first_observed_at": first_seen.isoformat() if first_seen else "",
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "age_bucket": _age_bucket(age_hours),
        "peak_coverage_share": round(max(values), 6),
        "mean_coverage_share": round(sum(values) / len(values), 6),
        "current_coverage_share": round(current_hour, 6),
        "coverage_velocity_1h": round(coverage_velocity, 6),
        "current_momentum": round(momentum, 6),
        "distinct_publishers": len(domains),
        "independent_source_groups": _independent_source_groups(articles),
        "sampled_articles": len(articles),
        "sample_relevance": (
            round(sample_relevance, 3) if sample_relevance is not None else None
        ),
        "data_lag_minutes": round(max(0.0, (now - latest).total_seconds() / 60), 1),
        "error": (
            "sampled GDELT headlines did not match the canonical event"
            if low_quality else ""
        ),
        "timeline": compact_timeline,
    }


def fetch_gdelt_attention(
    candidate: dict[str, Any],
    now: datetime | None = None,
    *,
    request_get: Callable[..., Any] = requests.get,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch one event's rolling GDELT coverage timeline with bounded retry."""
    observed_at = now or datetime.now(timezone.utc)
    query = gdelt_query(candidate)
    terms = event_terms(candidate)
    if not _terms_are_sufficient(terms) or not query:
        return {
            "status": "unavailable",
            "provider": PROVIDER,
            "query": query,
            "terms": event_terms(candidate),
            "observed_at": observed_at.isoformat(),
            "error": "insufficient event terms",
        }

    params = {
        "query": query,
        "mode": "timelinevolinfo",
        "format": "json",
        "timespan": "1d",
    }
    headers = {
        "User-Agent": "CarterDailyNews/1.0 (+https://news.carter2099.com)",
        "Accept": "application/json",
    }
    errors: list[str] = []
    for attempt in range(2):
        if attempt:
            sleep(12.0)
        try:
            response = request_get(
                GDELT_DOC_URL,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code == 429 or response.status_code >= 500:
                errors.append(f"HTTP {response.status_code}")
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("GDELT response was not a JSON object")
            return _observation_from_response(candidate, payload, observed_at)
        except Exception as error:
            errors.append(" ".join(str(error).split())[:240])
    return {
        "status": "unavailable",
        "provider": PROVIDER,
        "query": query,
        "terms": event_terms(candidate),
        "observed_at": observed_at.isoformat(),
        "error": "; ".join(errors),
    }


def _cache_path(candidate: dict[str, Any], cache_dir: Path) -> Path:
    key = hashlib.sha256(gdelt_query(candidate).casefold().encode()).hexdigest()
    return cache_dir / f"{key}.json"


def _load_cache(candidate: dict[str, Any], cache_dir: Path, now: datetime) -> dict[str, Any] | None:
    path = _cache_path(candidate, cache_dir)
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text())
        observed = datetime.fromisoformat(cached["observed_at"])
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if now - observed > timedelta(hours=CACHE_TTL_HOURS):
            return None
        if cached.get("status") not in {"ok", "no_matches"}:
            return None
        return cached
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _save_cache(candidate: dict[str, Any], cache_dir: Path, observation: dict[str, Any]) -> None:
    if observation.get("status") not in {"ok", "no_matches"}:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(candidate, cache_dir)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(observation, ensure_ascii=False))
    temporary.replace(path)


def _percentile(value: float, values: list[float]) -> float:
    if not values:
        return 50.0
    transformed = [math.log1p(max(0.0, candidate)) for candidate in values]
    target = math.log1p(max(0.0, value))
    below = sum(candidate < target for candidate in transformed)
    equal = sum(candidate == target for candidate in transformed)
    return round(100.0 * (below + 0.5 * equal) / len(transformed), 1)


def _cohort_values(
    observations: list[dict[str, Any]], observation: dict[str, Any], field: str,
) -> list[float]:
    same_age = [
        float(candidate.get(field, 0.0) or 0.0)
        for candidate in observations
        if candidate.get("age_bucket") == observation.get("age_bucket")
    ]
    population = same_age if len(same_age) >= 3 else [
        float(candidate.get(field, 0.0) or 0.0) for candidate in observations
    ]
    return population


def _confidence(observation: dict[str, Any]) -> float:
    if observation.get("status") == "unavailable":
        return 0.0
    terms_quality = min(1.0, len(observation.get("terms", [])) / 3)
    if observation.get("status") == "no_matches":
        return 0.55 if observation.get("term_source") == "model_terms" else 0.30
    sampled = float(observation.get("sampled_articles", 0) or 0)
    groups = float(observation.get("independent_source_groups", 0) or 0)
    lag = observation.get("data_lag_minutes")
    lag_quality = 1.0 if isinstance(lag, (int, float)) and lag <= 90 else 0.5
    value = (
        0.45
        + 0.10 * terms_quality
        + 0.15 * min(1.0, sampled / 12)
        + 0.10 * min(1.0, groups / 5)
        + 0.05 * lag_quality
    )
    return round(min(0.85, value), 2)


def _priority_score(significance: str, digest_prominence: float, confidence: float) -> float:
    editorial = EDITORIAL_POINTS.get(significance, EDITORIAL_POINTS["medium"])
    editorial_weight = 0.60
    observed_weight = 0.40 * confidence
    return round(
        (editorial_weight * editorial + observed_weight * digest_prominence)
        / (editorial_weight + observed_weight),
        1,
    )

def _numeric(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def priority_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """Deterministic tie-breakers; never fall back to discovery order."""
    attention = item.get("attention")
    attention = attention if isinstance(attention, dict) else {}
    evidence = item.get("significance_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    date_value = re.sub(
        r"\D", "", _clean_text(item.get("date_confirmed") or item.get("date_published"))
    )
    return (
        _numeric(item.get("priority_score")),
        _numeric(attention.get("digest_prominence")),
        _numeric(attention.get("attention_now")),
        _numeric(attention.get("confidence")),
        IMPACT_SCOPE_POINTS.get(_clean_text(evidence.get("affected_scope")).lower(), 0.0),
        EDITORIAL_POINTS.get(
            _clean_text(item.get("editorial_significance")).lower(), 0.0
        ),
        int(date_value[:8]) if len(date_value) >= 8 else 0,
        _clean_text(item.get("title")).casefold(),
        _clean_text(item.get("url")).casefold(),
    )


def score_attention(
    candidates: list[dict[str, Any]],
    cache_dir: Path,
    *,
    now: datetime | None = None,
    fetcher: Callable[[dict[str, Any], datetime], dict[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    request_interval: float = REQUEST_INTERVAL_SECONDS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach independent attention and final product-priority scores."""
    observed_at = now or datetime.now(timezone.utc)
    effective_fetcher = fetcher or (
        lambda candidate, timestamp: fetch_gdelt_attention(candidate, timestamp)
    )
    scored = [enforce_editorial_significance(copy.deepcopy(item)) for item in candidates]
    observations: list[dict[str, Any]] = []
    requested = 0
    cache_hits = 0

    for candidate in scored:
        cached = _load_cache(candidate, cache_dir, observed_at)
        if cached is not None:
            observation = cached
            cache_hits += 1
        else:
            if requested and request_interval > 0:
                sleep(request_interval)
            try:
                observation = effective_fetcher(candidate, observed_at)
            except Exception as error:
                observation = {
                    "status": "unavailable",
                    "provider": PROVIDER,
                    "query": gdelt_query(candidate),
                    "terms": event_terms(candidate),
                    "observed_at": observed_at.isoformat(),
                    "error": " ".join(str(error).split())[:240],
                }
            requested += 1
            _save_cache(candidate, cache_dir, observation)
        observation.setdefault("term_source", event_term_source(candidate))
        observations.append(observation)

    comparable = [
        observation for observation in observations
        if observation.get("status") == "ok"
    ]
    for candidate, observation in zip(scored, observations):
        significance = candidate["editorial_significance"]
        status = observation.get("status")
        if status == "unavailable":
            attention_now = 50.0
            digest_prominence = 50.0
            normalized = {}
        elif status == "no_matches":
            attention_now = 0.0
            digest_prominence = 0.0
            normalized = {
                "publisher_saturation": 0.0,
                "coverage_velocity": 0.0,
                "source_breadth": 0.0,
                "peak_attention": 0.0,
                "attention_over_time": 0.0,
                "current_momentum": 0.0,
            }
        else:
            normalized = {
                "publisher_saturation": _percentile(
                    float(observation.get("current_coverage_share", 0.0) or 0.0),
                    _cohort_values(comparable, observation, "current_coverage_share"),
                ),
                "coverage_velocity": _percentile(
                    float(observation.get("coverage_velocity_1h", 0.0) or 0.0),
                    _cohort_values(comparable, observation, "coverage_velocity_1h"),
                ),
                "source_breadth": _percentile(
                    float(observation.get("independent_source_groups", 0.0) or 0.0),
                    _cohort_values(comparable, observation, "independent_source_groups"),
                ),
                "peak_attention": _percentile(
                    float(observation.get("peak_coverage_share", 0.0) or 0.0),
                    _cohort_values(comparable, observation, "peak_coverage_share"),
                ),
                "attention_over_time": _percentile(
                    float(observation.get("mean_coverage_share", 0.0) or 0.0),
                    _cohort_values(comparable, observation, "mean_coverage_share"),
                ),
                "current_momentum": _percentile(
                    float(observation.get("current_momentum", 0.0) or 0.0),
                    _cohort_values(comparable, observation, "current_momentum"),
                ),
            }
            # Only the available news-coverage channels are weighted. Social and
            # video remain explicitly unavailable instead of being fabricated.
            attention_now = round(
                0.50 * normalized["publisher_saturation"]
                + 0.357 * normalized["coverage_velocity"]
                + 0.143 * normalized["source_breadth"],
                1,
            )
            digest_prominence = round(
                0.50 * normalized["peak_attention"]
                + 0.30 * normalized["attention_over_time"]
                + 0.20 * normalized["current_momentum"],
                1,
            )

        confidence = _confidence(observation)
        priority = _priority_score(significance, digest_prominence, confidence)
        groups = int(observation.get("independent_source_groups", 0) or 0)
        if observation.get("status") == "ok":
            explanation = (
                f"{significance.title()} editorial significance; coverage peaked at the "
                f"{normalized['peak_attention']:.0f}th percentile across {groups} independent "
                f"source {'group' if groups == 1 else 'groups'}."
            )
        elif observation.get("status") == "no_matches":
            explanation = (
                f"{significance.title()} editorial significance; no matching GDELT coverage "
                "was observed in the rolling 24-hour window."
            )
        else:
            explanation = (
                f"{significance.title()} editorial significance; observed attention was "
                "unavailable, so priority uses editorial significance only."
            )

        candidate["attention"] = {
            "schema_version": SCHEMA_VERSION,
            "provider": PROVIDER,
            "status": observation.get("status", "unavailable"),
            "attention_now": attention_now,
            "digest_prominence": digest_prominence,
            "confidence": confidence,
            "age_bucket": observation.get("age_bucket", "unknown"),
            "normalized_signals": normalized,
            "evidence": {
                "peak_coverage_share": observation.get("peak_coverage_share", 0.0),
                "current_coverage_share": observation.get("current_coverage_share", 0.0),
                "coverage_velocity_1h": observation.get("coverage_velocity_1h", 0.0),
                "distinct_publishers": observation.get("distinct_publishers", 0),
                "independent_source_groups": groups,
                "sampled_articles": observation.get("sampled_articles", 0),
                "sample_relevance": observation.get("sample_relevance"),
                "query": observation.get("query", ""),
                "term_source": observation.get("term_source"),
                "channels_available": ["news_coverage"],
                "channels_unavailable": ["homepage_prominence", "social", "video"],
            },
        }
        candidate["priority_score"] = priority
        candidate["priority_explanation"] = explanation

    artifact = {
        "schema_version": SCHEMA_VERSION,
        "provider": PROVIDER,
        "observed_at": observed_at.isoformat(),
        "requests": requested,
        "cache_hits": cache_hits,
        "available": sum(
            observation.get("status") in {"ok", "no_matches"}
            for observation in observations
        ),
        "unavailable": sum(
            observation.get("status") == "unavailable" for observation in observations
        ),
        "observations": [
            {
                "title": candidate.get("title", ""),
                "url": candidate.get("url", ""),
                "editorial_significance": candidate["editorial_significance"],
                "priority_score": candidate["priority_score"],
                "attention": candidate["attention"],
                "raw": observation,
            }
            for candidate, observation in zip(scored, observations)
        ],
    }
    return scored, artifact
