#!/usr/bin/env python3
"""Build and publish the static daily news site from curated digest artifacts."""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import uuid
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from digest_runner import (
    DIGESTS_DIR,
    TOPICS,
    _fallback_standfirst,
    _validate_standfirst,
)
from news_attention import EDITORIAL_POINTS, normalize_editorial_significance
from send_digest import send as smtp_send

HOME = Path.home()
NEWS_DIR = DIGESTS_DIR / "news"
PUBLICATIONS_DIR = NEWS_DIR / "publications"
RELEASES_DIR = NEWS_DIR / "releases"
CURRENT_SITE = NEWS_DIR / "current"
ASSET_DIR = HOME / "news" / "assets"
BASE_URL = "https://news.carter2099.com"
SUMMARY_RECIPIENT = "carter2099@pm.me"
TOPIC_ORDER = tuple(TOPICS)
PUBLICATION_SCHEMA_VERSION = 2


class LegacyDigestParser(HTMLParser):
    """Extract the stable story fields from the historical email HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section: str | None = None
        self.title = ""
        self.standfirst = ""
        self.fresh: list[dict[str, str]] = []
        self.ongoing: list[dict[str, str]] = []
        self._h1: list[str] | None = None
        self._h2: list[str] | None = None
        self._p: list[str] | None = None
        self._p_had_story_link = False
        self._a: list[str] | None = None
        self._a_href = ""
        self._span: list[str] | None = None
        self._current_story: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "h1":
            self._h1 = []
        elif tag == "h2":
            self._h2 = []
        elif tag == "p":
            self._p = []
            self._p_had_story_link = False
        elif tag == "a" and self.section:
            self._a = []
            self._a_href = attrs_dict.get("href") or ""
            self._p_had_story_link = True
        elif tag == "span" and self._current_story is not None:
            self._span = []

    def handle_data(self, data: str) -> None:
        for target in (self._h1, self._h2, self._p, self._a, self._span):
            if target is not None:
                target.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._h1 is not None:
            self.title = _clean_text("".join(self._h1))
            self._h1 = None
        elif tag == "h2" and self._h2 is not None:
            heading = _clean_text("".join(self._h2)).casefold()
            if "fresh" in heading:
                self.section = "fresh"
            elif "ongoing" in heading or "recent" in heading or "relevant" in heading:
                self.section = "ongoing"
            self._h2 = None
        elif tag == "a" and self._a is not None and self.section:
            story = {
                "title": _clean_text("".join(self._a)),
                "url": self._a_href,
            }
            host = urlsplit(self._a_href).hostname
            if host:
                story["source_domain"] = host.removeprefix("www.")
            target = self.fresh if self.section == "fresh" else self.ongoing
            target.append(story)
            self._current_story = story
            self._a = None
            self._a_href = ""
        elif tag == "span" and self._span is not None:
            if self._current_story is not None:
                category = _clean_text("".join(self._span)).lstrip("· ")
                if category:
                    self._current_story["category"] = category
            self._span = None
        elif tag == "p" and self._p is not None:
            text = _clean_text("".join(self._p))
            if self.section is None:
                if self.title and len(text) >= 40 and "carter2099.com" not in text:
                    self.standfirst = self.standfirst or text
            elif self._current_story is not None and not self._p_had_story_link and text:
                if text.startswith("↳"):
                    self._current_story["why_still_relevant"] = text.lstrip("↳ ")
                elif not self._current_story.get("summary"):
                    self._current_story["summary"] = text
            self._p = None
            self._p_had_story_link = False


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _valid_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    except ValueError:
        return False


def _safe_url(value: Any) -> str:
    candidate = _clean_text(value)
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return candidate


def _public_story(story: dict[str, Any], *, ongoing: bool = False) -> dict[str, Any]:
    normalized = normalize_editorial_significance(dict(story))
    result: dict[str, Any] = {}
    for key in (
        "title", "source_domain", "date_published", "date_confirmed", "summary",
        "category", "editorial_significance", "author", "event",
        "priority_explanation",
    ):
        value = _clean_text(normalized.get(key))
        if value:
            result[key] = value
    url = _safe_url(normalized.get("url"))
    if url:
        result["url"] = url
    try:
        result["priority_score"] = round(float(
            normalized.get(
                "priority_score",
                EDITORIAL_POINTS[normalized["editorial_significance"]],
            )
        ), 1)
    except (TypeError, ValueError):
        result["priority_score"] = EDITORIAL_POINTS[normalized["editorial_significance"]]
    attention = normalized.get("attention")
    if isinstance(attention, dict):
        result["attention"] = {
            key: attention.get(key)
            for key in (
                "schema_version", "provider", "status", "attention_now",
                "digest_prominence", "confidence", "age_bucket",
                "normalized_signals", "evidence",
            )
            if attention.get(key) is not None
        }
    if ongoing:
        why = _clean_text(normalized.get("why_still_relevant"))
        if why:
            result["why_still_relevant"] = why
    return result


def _topic_for_slug(slug: str) -> tuple[str, dict[str, Any]]:
    for key in TOPIC_ORDER:
        topic = TOPICS[key]
        if topic["web_slug"] == slug:
            return key, topic
    raise KeyError(slug)


def _empty_publication(topic: dict[str, Any], issue_date: str) -> dict[str, Any]:
    return {
        "schema_version": PUBLICATION_SCHEMA_VERSION,
        "date": issue_date,
        "slug": topic["web_slug"],
        "title": topic["web_title"],
        "source_category": topic["category"],
        "status": "unavailable",
        "notice": "",
        "standfirst": "No section was published for this category on this date.",
        "fresh": [],
        "ongoing": [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _normalize_publication(
    raw: dict[str, Any], topic: dict[str, Any], issue_date: str,
) -> dict[str, Any]:
    publication = _empty_publication(topic, issue_date)
    publication.update({
        "status": _clean_text(raw.get("status")) or "published",
        "notice": _clean_text(raw.get("notice")),
        "standfirst": _clean_text(raw.get("standfirst") or raw.get("intro")),
        "generated_at": _clean_text(raw.get("generated_at"))
        or datetime.now(timezone.utc).isoformat(),
    })
    publication["fresh"] = [
        item for item in (
            _public_story(story) for story in raw.get("fresh", []) if isinstance(story, dict)
        ) if item.get("title") and item.get("url")
    ]
    publication["ongoing"] = [
        item for item in (
            _public_story(story, ongoing=True)
            for story in raw.get("ongoing", []) if isinstance(story, dict)
        ) if item.get("title") and item.get("url")
    ]
    publication["fresh"].sort(
        key=lambda item: float(item.get("priority_score", 0.0) or 0.0),
        reverse=True,
    )
    valid_standfirst, _ = _validate_standfirst(
        publication["standfirst"],
        publication["fresh"] + publication["ongoing"],
    )
    if not valid_standfirst:
        publication["standfirst"] = _fallback_standfirst(
            publication["fresh"], publication["ongoing"]
        )
    return publication


def _publication_from_run(
    topic: dict[str, Any], issue_date: str, run_dir: Path,
) -> dict[str, Any] | None:
    publication_path = run_dir / "publication.json"
    if publication_path.exists():
        try:
            raw = json.loads(publication_path.read_text())
            if isinstance(raw, dict):
                return _normalize_publication(raw, topic, issue_date)
        except (json.JSONDecodeError, OSError):
            pass

    curated_path = run_dir / "06-curated.json"
    if not curated_path.exists():
        return None
    try:
        curated = json.loads(curated_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(curated, dict):
        return None

    standfirst = ""
    standfirst_path = run_dir / "07-standfirst.json"
    legacy_intro_path = run_dir / "07-intro.json"
    source_path = standfirst_path if standfirst_path.exists() else legacy_intro_path
    if source_path.exists():
        try:
            copy_raw = json.loads(source_path.read_text())
            if isinstance(copy_raw, dict):
                standfirst = _clean_text(
                    copy_raw.get("standfirst") or copy_raw.get("intro")
                )
        except (json.JSONDecodeError, OSError):
            pass
    raw = {
        "status": "published" if curated.get("fresh") or curated.get("ongoing") else "empty",
        "standfirst": standfirst,
        "fresh": curated.get("fresh", []),
        "ongoing": curated.get("ongoing", []),
        "generated_at": datetime.fromtimestamp(
            curated_path.stat().st_mtime, timezone.utc
        ).isoformat(),
    }
    return _normalize_publication(raw, topic, issue_date)


def _publication_from_legacy_html(
    topic: dict[str, Any], issue_date: str, archive_path: Path,
) -> dict[str, Any] | None:
    try:
        parser = LegacyDigestParser()
        parser.feed(archive_path.read_text())
    except (OSError, UnicodeError):
        return None
    if not parser.fresh and not parser.ongoing:
        return None
    raw = {
        "status": "published",
        "standfirst": parser.standfirst,
        "fresh": parser.fresh,
        "ongoing": parser.ongoing,
        "generated_at": datetime.fromtimestamp(
            archive_path.stat().st_mtime, timezone.utc
        ).isoformat(),
    }
    return _normalize_publication(raw, topic, issue_date)


def _source_dates(digest_dir: Path) -> set[str]:
    dates: set[str] = set()
    if not digest_dir.exists():
        return dates
    for child in digest_dir.iterdir():
        name = child.stem if child.is_file() and child.suffix == ".html" else child.name
        if _valid_date(name):
            dates.add(name)
    return dates


def collect_source_publication(
    digests_dir: Path, topic: dict[str, Any], issue_date: str,
) -> dict[str, Any] | None:
    digest_dir = digests_dir / topic["category"]
    run_publication = _publication_from_run(topic, issue_date, digest_dir / issue_date)
    if run_publication is not None:
        return run_publication
    archive_path = digest_dir / f"{issue_date}.html"
    if archive_path.exists():
        return _publication_from_legacy_html(topic, issue_date, archive_path)
    return None


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def sync_publications(
    digests_dir: Path = DIGESTS_DIR,
    publications_dir: Path = PUBLICATIONS_DIR,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Import every available topic/date into the durable publication archive."""
    publications_dir.mkdir(parents=True, exist_ok=True)
    all_dates: set[str] = set()
    for key in TOPIC_ORDER:
        all_dates.update(_source_dates(digests_dir / TOPICS[key]["category"]))

    editions: dict[str, dict[str, dict[str, Any]]] = {}
    for issue_date in sorted(all_dates):
        date_editions: dict[str, dict[str, Any]] = {}
        for key in TOPIC_ORDER:
            topic = TOPICS[key]
            publication = collect_source_publication(digests_dir, topic, issue_date)
            if publication is None:
                continue
            destination = publications_dir / issue_date / f"{topic['web_slug']}.json"
            _atomic_json(destination, publication)
            date_editions[topic["web_slug"]] = publication
        if date_editions:
            editions[issue_date] = date_editions

    manifest = {
        "schema_version": PUBLICATION_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dates": [
            {
                "date": issue_date,
                "categories": [
                    TOPICS[key]["web_slug"] for key in TOPIC_ORDER
                    if TOPICS[key]["web_slug"] in editions[issue_date]
                ],
            }
            for issue_date in sorted(editions, reverse=True)
        ],
    }
    _atomic_json(publications_dir / "manifest.json", manifest)
    return editions


def load_publications(
    publications_dir: Path = PUBLICATIONS_DIR,
) -> dict[str, dict[str, dict[str, Any]]]:
    editions: dict[str, dict[str, dict[str, Any]]] = {}
    if not publications_dir.exists():
        return editions
    for date_dir in sorted(publications_dir.iterdir()):
        if not date_dir.is_dir() or not _valid_date(date_dir.name):
            continue
        date_editions: dict[str, dict[str, Any]] = {}
        for path in date_dir.glob("*.json"):
            try:
                _, topic = _topic_for_slug(path.stem)
                raw = json.loads(path.read_text())
                if isinstance(raw, dict):
                    date_editions[path.stem] = _normalize_publication(
                        raw, topic, date_dir.name
                    )
            except (KeyError, OSError, json.JSONDecodeError):
                continue
        if date_editions:
            editions[date_dir.name] = date_editions
    return editions


def _edition_date(issue_date: str) -> str:
    parsed = date.fromisoformat(issue_date)
    return parsed.strftime("%A, %B %-d, %Y")


def _story_meta(story: dict[str, Any], section_title: str = "") -> str:
    parts = [section_title] if section_title else []
    if story.get("category"):
        parts.append(str(story["category"]))
    if story.get("source_domain"):
        parts.append(str(story["source_domain"]))
    published = story.get("date_confirmed") or story.get("date_published")
    if published:
        parts.append(str(published))
    return " · ".join(parts)


def _render_story(
    story: dict[str, Any],
    *,
    lead: bool = False,
    ongoing: bool = False,
    section_title: str = "",
) -> str:
    title = html.escape(str(story.get("title", "")))
    url = html.escape(_safe_url(story.get("url")), quote=True)
    summary = html.escape(str(story.get("summary", "")))
    meta = html.escape(_story_meta(story, section_title))
    why = ""
    if ongoing and story.get("why_still_relevant"):
        why = (
            '<p class="story-context"><span>What changed</span> '
            f'{html.escape(str(story["why_still_relevant"]))}</p>'
        )
    classes = "story lead-story" if lead else ("story ongoing-story" if ongoing else "story")
    priority = html.escape(str(story.get("priority_score", "")), quote=True)
    return (
        f'<article class="{classes}" data-priority="{priority}">'
        f'<p class="story-meta">{meta}</p>'
        f'<h2 class="story-title"><a href="{url}" target="_blank" '
        f'rel="noopener noreferrer">{title}<span class="external" aria-hidden="true">↗</span></a></h2>'
        f'<p class="story-summary">{summary}</p>{why}'
        '</article>'
    )


def _category_nav(issue_date: str, active_slug: str) -> str:
    front_current = ' aria-current="page"' if active_slug == "front-page" else ""
    links = [f'<a href="/{issue_date}/"{front_current}>Front Page</a>']
    for key in TOPIC_ORDER:
        topic = TOPICS[key]
        slug = topic["web_slug"]
        current = ' aria-current="page"' if slug == active_slug else ""
        links.append(
            f'<a href="/{issue_date}/{slug}/"{current}>'
            f'{html.escape(topic["web_title"])}</a>'
        )
    return "".join(links)


def _date_options(dates: list[str], issue_date: str) -> str:
    return "".join(
        f'<option value="{candidate}"{" selected" if candidate == issue_date else ""}>'
        f'{html.escape(_edition_date(candidate))}</option>'
        for candidate in dates
    )


def _page_description(publication: dict[str, Any]) -> str:
    standfirst = _clean_text(publication.get("standfirst"))
    return standfirst[:157] + "…" if len(standfirst) > 160 else standfirst


def render_category_page(
    publication: dict[str, Any],
    issue_date: str,
    dates: list[str],
    editions: dict[str, dict[str, dict[str, Any]]],
) -> str:
    slug = publication["slug"]
    title = publication["title"]
    date_index = dates.index(issue_date)
    newer = dates[date_index - 1] if date_index > 0 else None
    older = dates[date_index + 1] if date_index + 1 < len(dates) else None
    count = len(publication["fresh"]) + len(publication["ongoing"])
    count_text = f"{count} {'story' if count == 1 else 'stories'}"
    notice = ""
    if publication.get("notice"):
        notice = f'<aside class="edition-notice">{html.escape(publication["notice"])}</aside>'
    if publication["status"] == "unavailable":
        notice = '<aside class="edition-notice">No edition was published for this category on this date.</aside>'

    fresh = publication["fresh"]
    if fresh:
        lead = _render_story(fresh[0], lead=True)
        remaining = "".join(_render_story(story) for story in fresh[1:])
        fresh_html = lead + (f'<div class="story-grid">{remaining}</div>' if remaining else "")
    else:
        fresh_html = '<p class="empty-state">No fresh stories were selected for this edition.</p>'

    ongoing = publication["ongoing"]
    ongoing_html = "".join(
        _render_story(story, ongoing=True) for story in ongoing
    ) or '<p class="empty-state">No developing stories were selected for this edition.</p>'

    older_link = (
        f'<a class="edition-link" href="/{older}/{slug}/"><span>Older edition</span>'
        f'<strong>{html.escape(_edition_date(older))}</strong></a>' if older else '<span></span>'
    )
    newer_link = (
        f'<a class="edition-link align-right" href="/{newer}/{slug}/"><span>Newer edition</span>'
        f'<strong>{html.escape(_edition_date(newer))}</strong></a>' if newer else '<span></span>'
    )
    canonical = f"{BASE_URL}/{issue_date}/{slug}/"
    description = html.escape(_page_description(publication), quote=True)
    standfirst = html.escape(publication["standfirst"])
    page_title = html.escape(f"{title} — {_edition_date(issue_date)}")

    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{page_title}</title>
  <meta name="description" content="{description}">
  <link rel="canonical" href="{canonical}">
  <link rel="stylesheet" href="/assets/news.css">
  <script src="/assets/news.js" defer></script>
</head>
<body>
<a class="skip-link" href="#content">Skip to stories</a>
<header class="site-header">
  <div class="utility-bar shell">
    <a class="publication-name" href="/{issue_date}/">Daily News</a>
    <div class="edition-controls">
      <a class="archive-link" href="/archive/">Archive</a>
      <label for="edition-date">Edition</label>
      <select id="edition-date" data-category="{html.escape(slug, quote=True)}">
        {_date_options(dates, issue_date)}
      </select>
    </div>
  </div>
  <div class="masthead shell">
    <p class="eyebrow">{html.escape(_edition_date(issue_date))}</p>
    <h1>{html.escape(title)}</h1>
    <p>{html.escape(count_text)}</p>
  </div>
  <nav class="category-nav" aria-label="News categories"><div class="shell">
    {_category_nav(issue_date, slug)}
  </div></nav>
</header>
<main id="content" class="shell">
  {notice}
  <section class="introduction" aria-labelledby="briefing-heading">
    <p class="section-label" id="briefing-heading">In brief</p>
    <p class="introduction-copy">{standfirst}</p>
  </section>
  <section class="fresh-section" aria-labelledby="fresh-heading">
    <div class="section-heading"><h2 id="fresh-heading">Latest</h2><span>Last 24 hours</span></div>
    {fresh_html}
  </section>
  <section class="ongoing-section" aria-labelledby="ongoing-heading">
    <div class="section-heading"><h2 id="ongoing-heading">Developing and ongoing</h2><span>Material updates across days</span></div>
    <div class="ongoing-list">{ongoing_html}</div>
  </section>
  <nav class="edition-pagination" aria-label="Adjacent editions">{older_link}{newer_link}</nav>
</main>
<footer class="site-footer"><div class="shell">
  <span>Updated daily after curation completes. Attention data provided by <a href="https://www.gdeltproject.org/" target="_blank" rel="noopener noreferrer">GDELT</a>.</span><a href="/archive/">Browse all editions</a>
</div></footer>
</body>
</html>
'''


def _front_page_sections(
    date_editions: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    sections: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    for key in TOPIC_ORDER:
        topic = TOPICS[key]
        publication = date_editions.get(topic["web_slug"])
        if publication is None:
            continue
        source = publication["fresh"] or publication["ongoing"]
        stories = sorted(
            (dict(story) for story in source),
            key=lambda story: float(story.get("priority_score", 0.0) or 0.0),
            reverse=True,
        )[:2]
        for story in stories:
            story["_section_slug"] = topic["web_slug"]
            story["_section_title"] = topic["web_title"]
            story["_ongoing"] = not bool(publication["fresh"])
            all_candidates.append(story)
        sections.append({
            "slug": topic["web_slug"],
            "title": topic["web_title"],
            "stories": stories,
        })
    lead = max(
        all_candidates,
        key=lambda story: float(story.get("priority_score", 0.0) or 0.0),
        default=None,
    )
    return lead, sections


def render_front_page(
    date_editions: dict[str, dict[str, Any]],
    issue_date: str,
    dates: list[str],
) -> str:
    lead, sections = _front_page_sections(date_editions)
    date_index = dates.index(issue_date)
    newer = dates[date_index - 1] if date_index > 0 else None
    older = dates[date_index + 1] if date_index + 1 < len(dates) else None
    selected_count = sum(len(section["stories"]) for section in sections)
    if lead is not None:
        lead_html = _render_story(
            lead,
            lead=True,
            ongoing=bool(lead.get("_ongoing")),
            section_title=str(lead.get("_section_title", "")),
        )
        description_text = _clean_text(lead.get("summary"))
    else:
        lead_html = '<p class="empty-state">No front-page stories were selected.</p>'
        description_text = "The highest-priority stories from each Daily News section."

    section_html = []
    lead_url = _safe_url(lead.get("url")) if lead else ""
    for section in sections:
        stories = [
            story for story in section["stories"]
            if _safe_url(story.get("url")) != lead_url
        ]
        if stories:
            cards = "".join(
                _render_story(
                    story,
                    ongoing=bool(story.get("_ongoing")),
                    section_title=section["title"],
                )
                for story in stories
            )
        else:
            cards = '<p class="front-section-reference">Lead story above.</p>'
        section_html.append(
            f'<section class="front-section" aria-labelledby="front-{html.escape(section["slug"], quote=True)}">'
            f'<div class="front-section-heading"><h2 id="front-{html.escape(section["slug"], quote=True)}">'
            f'<a href="/{issue_date}/{html.escape(section["slug"], quote=True)}/">'
            f'{html.escape(section["title"])}</a></h2><span>Top coverage</span></div>'
            f'<div class="front-story-list">{cards}</div></section>'
        )

    older_link = (
        f'<a class="edition-link" href="/{older}/"><span>Older front page</span>'
        f'<strong>{html.escape(_edition_date(older))}</strong></a>'
        if older else '<span></span>'
    )
    newer_link = (
        f'<a class="edition-link align-right" href="/{newer}/"><span>Newer front page</span>'
        f'<strong>{html.escape(_edition_date(newer))}</strong></a>'
        if newer else '<span></span>'
    )
    description = html.escape(
        description_text[:157] + "…" if len(description_text) > 160 else description_text,
        quote=True,
    )
    canonical = f"{BASE_URL}/{issue_date}/"
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Front Page — {html.escape(_edition_date(issue_date))}</title>
  <meta name="description" content="{description}">
  <link rel="canonical" href="{canonical}">
  <link rel="stylesheet" href="/assets/news.css">
  <script src="/assets/news.js" defer></script>
</head>
<body class="front-page">
<a class="skip-link" href="#content">Skip to stories</a>
<header class="site-header">
  <div class="utility-bar shell">
    <a class="publication-name" href="/{issue_date}/">Daily News</a>
    <div class="edition-controls">
      <a class="archive-link" href="/archive/">Archive</a>
      <label for="edition-date">Edition</label>
      <select id="edition-date" data-category="front-page">{_date_options(dates, issue_date)}</select>
    </div>
  </div>
  <div class="masthead shell">
    <p class="eyebrow">{html.escape(_edition_date(issue_date))}</p>
    <h1>Front Page</h1>
    <p>{len(sections)} sections · {selected_count} top stories</p>
  </div>
  <nav class="category-nav" aria-label="News categories"><div class="shell">
    {_category_nav(issue_date, "front-page")}
  </div></nav>
</header>
<main id="content" class="shell front-page-main">
  <section class="front-lead-section" aria-label="Lead story">{lead_html}</section>
  <div class="front-sections">{''.join(section_html)}</div>
  <nav class="edition-pagination" aria-label="Adjacent front pages">{older_link}{newer_link}</nav>
</main>
<footer class="site-footer"><div class="shell">
  <span>Priority combines editorial consequence with observed coverage attention. Data provided by <a href="https://www.gdeltproject.org/" target="_blank" rel="noopener noreferrer">GDELT</a>.</span><a href="/archive/">Browse all editions</a>
</div></footer>
</body>
</html>
'''


def render_archive_page(
    dates: list[str], editions: dict[str, dict[str, dict[str, Any]]],
) -> str:
    rows = []
    for issue_date in dates:
        links = [f'<a href="/{issue_date}/">Front Page</a>']
        for key in TOPIC_ORDER:
            topic = TOPICS[key]
            slug = topic["web_slug"]
            if slug in editions.get(issue_date, {}):
                links.append(
                    f'<a href="/{issue_date}/{slug}/">{html.escape(topic["web_title"])}</a>'
                )
        rows.append(
            '<li><time datetime="{date}">{label}</time><div>{links}</div></li>'.format(
                date=issue_date,
                label=html.escape(_edition_date(issue_date)),
                links="".join(links),
            )
        )
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archive — Daily News</title>
  <meta name="description" content="Previous daily news editions by date and category.">
  <link rel="canonical" href="{BASE_URL}/archive/">
  <link rel="stylesheet" href="/assets/news.css">
</head>
<body class="archive-page">
<a class="skip-link" href="#content">Skip to editions</a>
<header class="site-header compact-header">
  <div class="utility-bar shell"><a class="publication-name" href="/">Daily News</a></div>
  <div class="masthead shell"><p class="eyebrow">Past coverage</p><h1>Edition archive</h1><p>{len(dates)} daily editions</p></div>
</header>
<main id="content" class="shell archive-main">
  <ol class="archive-list">{''.join(rows)}</ol>
</main>
<footer class="site-footer"><div class="shell"><span>Five focused categories, one daily edition.</span><a href="/">Latest news</a></div></footer>
</body>
</html>
'''


def _write_page(root: Path, relative: str, content: str) -> None:
    path = root / relative / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def build_site(
    editions: dict[str, dict[str, dict[str, Any]]],
    news_dir: Path = NEWS_DIR,
    asset_dir: Path = ASSET_DIR,
) -> Path:
    if not editions:
        raise ValueError("no news publications are available")
    dates = sorted(editions, reverse=True)
    build_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    release = news_dir / "releases" / build_id
    (release / "assets").mkdir(parents=True)
    for asset_name in ("news.css", "news.js"):
        shutil.copy(asset_dir / asset_name, release / "assets" / asset_name)

    for issue_date in dates:
        date_editions = editions[issue_date]
        for key in TOPIC_ORDER:
            topic = TOPICS[key]
            slug = topic["web_slug"]
            publication = date_editions.get(slug) or _empty_publication(topic, issue_date)
            page = render_category_page(publication, issue_date, dates, editions)
            _write_page(release, f"{issue_date}/{slug}", page)
        _write_page(
            release,
            issue_date,
            render_front_page(date_editions, issue_date, dates),
        )

    latest = dates[0]
    for key in TOPIC_ORDER:
        topic = TOPICS[key]
        slug = topic["web_slug"]
        category_dates = [d for d in dates if slug in editions[d]]
        if not category_dates:
            continue
        category_date = category_dates[0]
        page = render_category_page(
            editions[category_date][slug], category_date, dates, editions
        )
        _write_page(release, slug, page)

    (release / "index.html").write_text(
        render_front_page(editions[latest], latest, dates)
    )
    _write_page(release, "archive", render_archive_page(dates, editions))
    (release / "404.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Page not found — Daily News</title><link rel="stylesheet" href="/assets/news.css">'
        '</head><body class="error-page"><main><p class="eyebrow">404</p><h1>Page not found</h1>'
        '<p>The edition or category you requested does not exist.</p><a href="/">Latest news</a>'
        '</main></body></html>'
    )
    _atomic_json(release / "build.json", {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "latest_date": latest,
        "dates": len(dates),
        "pages": len(dates) * len(TOPIC_ORDER) + len(TOPIC_ORDER) + len(dates) + 3,
    })

    current = news_dir / "current"
    temporary_link = news_dir / f".current-{uuid.uuid4().hex}"
    temporary_link.symlink_to(Path("releases") / build_id)
    temporary_link.replace(current)

    releases = sorted(
        (path for path in (news_dir / "releases").iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old_release in releases[2:]:
        shutil.rmtree(old_release)
    return release


def render_summary_email(
    issue_date: str,
    editions: dict[str, dict[str, Any]],
    base_url: str = BASE_URL,
) -> str:
    sections = []
    for key in TOPIC_ORDER:
        topic = TOPICS[key]
        slug = topic["web_slug"]
        publication = editions.get(slug) or _empty_publication(topic, issue_date)
        standfirst = html.escape(publication["standfirst"])
        link = f"{base_url}/{issue_date}/{slug}/"
        count = len(publication["fresh"]) + len(publication["ongoing"])
        sections.append(f'''
<tr><td style="padding:22px 30px;border-top:1px solid #dedbd3;">
  <p style="margin:0 0 5px;color:#77736b;font:600 11px/1.3 Arial,sans-serif;text-transform:uppercase;letter-spacing:1.2px;">{count} {'story' if count == 1 else 'stories'}</p>
  <h2 style="margin:0 0 8px;color:#171716;font:700 21px/1.2 Georgia,serif;">{html.escape(topic['web_title'])}</h2>
  <p style="margin:0 0 12px;color:#45433f;font:14px/1.55 Arial,sans-serif;">{standfirst}</p>
  <a href="{html.escape(link, quote=True)}" style="color:#7b2f2f;font:700 13px/1.4 Arial,sans-serif;text-decoration:none;">Read {html.escape(topic['web_title'])} →</a>
</td></tr>''')
    today_link = f"{base_url}/{issue_date}/"
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#f2f0ea;color:#171716;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f0ea;padding:24px 10px;"><tr><td align="center">
<table role="presentation" width="620" cellpadding="0" cellspacing="0" style="width:100%;max-width:620px;background:#fffdfa;border:1px solid #dedbd3;">
<tr><td style="padding:30px;border-top:4px solid #171716;">
  <p style="margin:0 0 7px;color:#77736b;font:600 11px/1.3 Arial,sans-serif;text-transform:uppercase;letter-spacing:1.2px;">{html.escape(_edition_date(issue_date))}</p>
  <h1 style="margin:0 0 10px;color:#171716;font:700 32px/1.05 Georgia,serif;">Today’s news</h1>
  <p style="margin:0 0 18px;color:#45433f;font:15px/1.55 Arial,sans-serif;">The day’s leading coverage across AI, agents, hardware, gaming, and world affairs.</p>
  <a href="{html.escape(today_link, quote=True)}" style="display:inline-block;background:#171716;color:#fffdfa;padding:10px 15px;font:700 13px/1 Arial,sans-serif;text-decoration:none;">Open the front page</a>
</td></tr>
{''.join(sections)}
<tr><td style="padding:18px 30px;border-top:1px solid #dedbd3;color:#77736b;font:12px/1.5 Arial,sans-serif;">news.carter2099.com</td></tr>
</table></td></tr></table></body></html>'''




def send_summary_once(
    issue_date: str,
    editions: dict[str, dict[str, Any]],
    news_dir: Path = NEWS_DIR,
    *,
    send_func: Callable[[str, str, list[str]], Any] = smtp_send,
    recipient: str = SUMMARY_RECIPIENT,
    base_url: str = BASE_URL,
) -> bool:
    marker = news_dir / "mail" / f"{issue_date}.sent.json"
    if marker.exists():
        print(f"[mail] already sent for {issue_date}; skipping")
        return False
    body = render_summary_email(issue_date, editions, base_url=base_url)
    subject = f"Daily News — {date.fromisoformat(issue_date).strftime('%B %-d, %Y')}"
    send_func(subject, body, [recipient])
    _atomic_json(marker, {
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "date": issue_date,
        "recipient": recipient,
        "subject": subject,
    })
    return True


def publish(
    issue_date: str,
    *,
    digests_dir: Path = DIGESTS_DIR,
    news_dir: Path = NEWS_DIR,
    asset_dir: Path = ASSET_DIR,
    send_email: bool = True,
    send_func: Callable[[str, str, list[str]], Any] = smtp_send,
) -> dict[str, Any]:
    if not _valid_date(issue_date):
        raise ValueError(f"invalid publication date: {issue_date}")
    publications_dir = news_dir / "publications"
    editions = sync_publications(digests_dir, publications_dir)
    if issue_date not in editions:
        raise RuntimeError(f"no curated digest artifacts found for {issue_date}")

    release = build_site(editions, news_dir, asset_dir)
    print(f"[site] activated {release.name} ({len(editions)} dates)")

    mailed = False
    if send_email:
        mailed = send_summary_once(
            issue_date,
            editions[issue_date],
            news_dir,
            send_func=send_func,
        )
        print(f"[mail] {'sent' if mailed else 'unchanged'}")

    return {
        "date": issue_date,
        "release": str(release),
        "dates": len(editions),
        "categories": len(editions[issue_date]),
        "email_sent": mailed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish curated digest artifacts as the daily news web application"
    )
    parser.add_argument(
        "--date", default=datetime.now(timezone.utc).date().isoformat(),
        help="UTC digest date (YYYY-MM-DD)",
    )
    parser.add_argument("--skip-email", action="store_true", help="Build without sending email")
    args = parser.parse_args()
    result = publish(args.date, send_email=not args.skip_email)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
