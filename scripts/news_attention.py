#!/usr/bin/env python3
"""Public Daily News attention-scoring owner.

Implementation lives in :mod:`daily_news.attention`; this stable module path is
kept for callers that use the historical script name.
"""
from daily_news.attention import (  # noqa: F401
    EDITORIAL_POINTS,
    SCHEMA_VERSION,
    canonicalize_publisher_url,
    enforce_editorial_significance,
    event_term_source,
    event_terms,
    fetch_gdelt_attention,
    gdelt_query,
    normalize_editorial_significance,
    priority_sort_key,
    score_attention,
)


__all__ = (
    "EDITORIAL_POINTS",
    "SCHEMA_VERSION",
    "canonicalize_publisher_url",
    "enforce_editorial_significance",
    "event_term_source",
    "event_terms",
    "fetch_gdelt_attention",
    "gdelt_query",
    "normalize_editorial_significance",
    "priority_sort_key",
    "score_attention",
)
