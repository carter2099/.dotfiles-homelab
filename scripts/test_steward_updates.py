#!/usr/bin/env python3
"""Behavioral tests for steward delayed-update selection and rollback."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import steward_runner as steward


class DelayedUpdateTests(unittest.TestCase):
    NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    def test_release_maturity_boundary_is_seven_days(self):
        self.assertTrue(steward._release_is_mature(
            "2026-08-18T12:00:00Z", now=self.NOW))
        self.assertFalse(steward._release_is_mature(
            "2026-08-18T12:00:01Z", now=self.NOW))

    def test_searxng_selects_newest_mature_tag(self):
        tags = [
            {"name": "2026.8.17-aaaa1111", "last_updated": "2026-08-17T12:00:00Z",
             "digest": "sha256:" + "1" * 64},
            {"name": "2026.8.18-bbbb2222", "last_updated": "2026-08-18T12:00:00Z",
             "digest": "sha256:" + "2" * 64},
            {"name": "2026.8.19-cccc3333", "last_updated": "2026-08-19T12:00:00Z",
             "digest": "sha256:" + "3" * 64},
        ]
        target = steward._select_mature_searxng_tag(
            tags, "2026.8.17-aaaa1111", now=self.NOW)
        self.assertEqual(target["name"], "2026.8.18-bbbb2222")

    def test_llama_selects_newest_mature_release(self):
        releases = [
            {"tag_name": "b10453", "published_at": "2026-08-17T12:00:00Z"},
            {"tag_name": "b10488", "published_at": "2026-08-18T12:00:00Z"},
            {"tag_name": "b10500", "published_at": "2026-08-19T12:00:00Z"},
        ]
        target = steward._select_mature_llama_release(
            releases, "b10453", now=self.NOW)
        self.assertEqual(target["tag_name"], "b10488")

    def test_searxng_failed_health_check_restores_pin(self):
        old_digest = "sha256:" + "1" * 64
        new_digest = "sha256:" + "2" * 64
        tags = [
            {"name": "2026.8.17-aaaa1111", "last_updated": "2026-08-17T12:00:00Z",
             "digest": old_digest},
            {"name": "2026.8.18-bbbb2222", "last_updated": "2026-08-18T12:00:00Z",
             "digest": new_digest},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            compose = home / "searxng" / "docker-compose.yml"
            compose.parent.mkdir()
            original = (
                "services:\n  core:\n"
                f"    image: docker.io/searxng/searxng@{old_digest}\n"
            )
            compose.write_text(original)
            with (
                patch.object(steward, "HOME", home),
                patch.object(steward, "run_capture", return_value="2026.8.17-aaaa1111"),
                patch.object(steward, "run"),
                patch.object(steward, "_wait_searxng_healthy",
                             side_effect=[False, True]),
            ):
                result = steward._p1_searxng_update(tags=tags, now=self.NOW)
            self.assertEqual(result["status"], "reverted")
            self.assertEqual(compose.read_text(), original)

    def test_searxng_success_keeps_new_immutable_pin(self):
        old_digest = "sha256:" + "1" * 64
        new_digest = "sha256:" + "2" * 64
        tags = [
            {"name": "2026.8.17-aaaa1111", "last_updated": "2026-08-17T12:00:00Z",
             "digest": old_digest},
            {"name": "2026.8.18-bbbb2222", "last_updated": "2026-08-18T12:00:00Z",
             "digest": new_digest},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            compose = home / "searxng" / "docker-compose.yml"
            compose.parent.mkdir()
            compose.write_text(
                "services:\n  core:\n"
                f"    image: docker.io/searxng/searxng@{old_digest}\n"
            )
            with (
                patch.object(steward, "HOME", home),
                patch.object(steward, "run_capture", return_value="2026.8.17-aaaa1111"),
                patch.object(steward, "run"),
                patch.object(steward, "_wait_searxng_healthy", return_value=True),
            ):
                result = steward._p1_searxng_update(tags=tags, now=self.NOW)
            self.assertEqual(result["status"], "ok")
            self.assertIn(new_digest, compose.read_text())

    def test_llama_reports_verified_remote_rollback(self):
        releases = [
            {"tag_name": "b10488", "published_at": "2026-08-18T12:00:00Z"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "update.sh"
            helper.write_text("exit 1\n")
            with (
                patch.object(steward, "LLAMA_CPP_UPDATE_SCRIPT", helper),
                patch.object(steward, "run_capture",
                             return_value="/opt/llama.cpp/b10453/bin/llama-server"),
                patch.object(steward, "run_capture_ok",
                             return_value=("", "ROLLBACK_OK b10453", 1)),
            ):
                result = steward._p1_llama_cpp_update(
                    releases=releases, now=self.NOW)
            self.assertEqual(result["status"], "reverted")
            self.assertEqual(result["reverted_to"], "b10453")


if __name__ == "__main__":
    unittest.main(verbosity=2)
