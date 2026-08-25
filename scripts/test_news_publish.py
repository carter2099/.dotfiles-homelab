#!/usr/bin/env python3
"""Behavioral contracts for the static news publisher and email delivery."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import news_publish as news  # noqa: E402


def check(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(message)


def sample_publication(topic: dict, issue_date: str, marker: str) -> dict:
    return {
        "schema_version": 1,
        "date": issue_date,
        "slug": topic["web_slug"],
        "title": topic["web_title"],
        "digest_title": topic["title"],
        "source_category": topic["category"],
        "status": "published",
        "notice": "",
        "intro": f"{marker} briefing summarizes the verified developments for this category.",
        "fresh": [{
            "title": f"{marker} lead story",
            "url": f"https://example.com/{topic['web_slug']}/lead",
            "source_domain": "example.com",
            "date_published": issue_date,
            "summary": f"{marker} source-backed summary with the material facts.",
            "category": "News",
        }],
        "ongoing": [{
            "title": f"{marker} developing story",
            "url": f"https://example.com/{topic['web_slug']}/developing",
            "summary": f"{marker} ongoing source-backed summary.",
            "category": "Developing",
            "why_still_relevant": "A second-day source established a material change.",
        }],
        "generated_at": f"{issue_date}T12:00:00+00:00",
    }


def write_assets(root: Path) -> Path:
    assets = root / "assets"
    assets.mkdir()
    (assets / "news.css").write_text("body { color: #171716; }")
    (assets / "news.js").write_text("void 0;")
    return assets


def test_legacy_html_migration() -> None:
    parser = news.LegacyDigestParser()
    parser.feed("""
      <h1>Gaming Digest</h1><p>June 15, 2026</p>
      <p>A sufficiently detailed editorial introduction for the historical edition.</p>
      <h2>Fresh — Last 24 Hours</h2>
      <p><a href="https://example.com/fresh">Fresh title</a><span> · Industry</span></p>
      <p>Fresh factual summary.</p>
      <h2>Recent &amp; Relevant</h2>
      <p><a href="https://example.com/ongoing">Ongoing title</a><span> · Policy</span></p>
      <p>Ongoing factual summary.</p><p>↳ Material second-day change.</p>
    """)
    check(parser.intro.startswith("A sufficiently"), parser.intro)
    check(parser.fresh[0]["category"] == "Industry", parser.fresh)
    check(parser.fresh[0]["summary"] == "Fresh factual summary.", parser.fresh)
    check(parser.ongoing[0]["why_still_relevant"] == "Material second-day change.", parser.ongoing)


def test_publish_builds_separate_history_and_one_email() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        digests = root / "digests"
        news_dir = digests / "news"
        assets = write_assets(root)
        current_date = "2026-08-25"
        older_date = "2026-06-15"

        for key in news.TOPIC_ORDER:
            topic = news.TOPICS[key]
            run_dir = digests / topic["category"] / current_date
            run_dir.mkdir(parents=True)
            marker = topic["web_title"]
            publication = sample_publication(topic, current_date, marker)
            if key == "ai-tech":
                publication["fresh"][0]["title"] = "AI <script>alert(1)</script> lead"
            (run_dir / "publication.json").write_text(json.dumps(publication))

        gaming_dir = digests / news.TOPICS["gaming"]["category"]
        (gaming_dir / f"{older_date}.html").write_text("""
          <h1>Gaming Digest</h1><p>June 15, 2026</p>
          <p>Historical gaming briefing with enough detail to qualify as an introduction.</p>
          <h2>Fresh — Last 24 Hours</h2>
          <p><a href="https://example.com/legacy">Legacy gaming story</a><span> · Industry</span></p>
          <p>A historical source-backed summary.</p>
        """)

        sent: list[tuple[str, str, list[str]]] = []

        def fake_send(subject: str, body: str, recipients: list[str]) -> None:
            sent.append((subject, body, recipients))

        result = news.publish(
            current_date,
            digests_dir=digests,
            news_dir=news_dir,
            asset_dir=assets,
            send_func=fake_send,
        )
        check(result["categories"] == 5, result)
        check(result["dates"] == 2, result)
        check(result["email_sent"], result)
        check(len(sent) == 1, sent)
        for key in news.TOPIC_ORDER:
            topic = news.TOPICS[key]
            check(topic["web_title"].replace("&", "&amp;") in sent[0][1], sent[0][1])
            check(f"/{current_date}/{topic['web_slug']}/" in sent[0][1], sent[0][1])

        current = news_dir / "current"
        check(current.is_symlink(), current)
        ai_page = (current / current_date / "ai-tech" / "index.html").read_text()
        check("&lt;script&gt;alert(1)&lt;/script&gt;" in ai_page, ai_page)
        check("Gaming lead story" not in ai_page, "category content leaked onto AI page")
        check((current / current_date / "gaming" / "index.html").exists(), "gaming page missing")
        check((current / older_date / "gaming" / "index.html").exists(), "historical page missing")
        check("Legacy gaming story" in (current / older_date / "gaming" / "index.html").read_text(),
              "legacy story not migrated")
        check((current / "archive" / "index.html").exists(), "archive page missing")

        second = news.publish(
            current_date,
            digests_dir=digests,
            news_dir=news_dir,
            asset_dir=assets,
            send_func=fake_send,
        )
        check(not second["email_sent"], second)
        check(len(sent) == 1, f"summary email sent {len(sent)} times")
        check(len(list((news_dir / "releases").iterdir())) == 2, "rollback release retention failed")




def main() -> None:
    tests = [
        test_legacy_html_migration,
        test_publish_builds_separate_history_and_one_email,
    ]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
