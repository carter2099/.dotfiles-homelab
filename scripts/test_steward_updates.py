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

    def test_judge_verdict_overrides_worker_pass(self):
        packet = {
            "verdict": "ATTENTION",
            "confirmed": [{"claim": "manual action remains"}],
        }
        self.assertEqual(steward._final_audit_verdict("PASS", packet), "ATTENTION")
        self.assertEqual(steward._final_audit_verdict("DRIFT", {}), "UNVERIFIABLE")
        self.assertEqual(
            steward._final_audit_verdict(
                "PASS", {"confirmed": [{"claim": "missing judge verdict"}]}
            ),
            "UNVERIFIABLE",
        )
        self.assertEqual(
            steward._final_audit_verdict(
                "DRIFT", {"judge_error": "timeout", "confirmed": []}
            ),
            "judge-failed",
        )

    def test_audit_judge_packet_schema_fails_closed(self):
        worker = steward._prepare_audit_worker_packet({
            "verdict": "DRIFT",
            "findings": [{
                "claim": "tracked config differs",
                "evidence": "diff output",
                "fix": "sync the tracked copy",
            }],
        })
        invalid = [
            [],
            {"verdict": "PASS", "confirmed": {}, "rejected": []},
            {"verdict": "PASS", "confirmed": [{"id": "", "claim": "x"}], "rejected": []},
            {"verdict": "UNKNOWN", "confirmed": [], "rejected": []},
            {"verdict": "PASS", "confirmed": [], "rejected": []},
            {
                "verdict": "PASS",
                "confirmed": [{
                    "id": "finding-1",
                    "claim": "tracked config differs",
                    "evidence": "reproduced diff",
                    "fix": "sync the tracked copy",
                }],
                "rejected": [],
            },
            {
                "verdict": "ATTENTION",
                "confirmed": [{
                    "id": "finding-1",
                    "claim": "different claim",
                    "evidence": "claimed reproduction",
                    "fix": "sync the tracked copy",
                }],
                "rejected": [],
            },
        ]
        for packet in invalid:
            with self.assertRaises(ValueError):
                steward._validate_audit_judge_packet(packet, worker)
        valid = {
            "verdict": "PASS",
            "confirmed": [],
            "rejected": [{
                "id": "finding-1",
                "claim": "tracked config differs",
                "reason": "not reproduced",
            }],
        }
        self.assertIs(steward._validate_audit_judge_packet(valid, worker), valid)
        with self.assertRaises(ValueError):
            steward._prepare_audit_worker_packet({
                "verdict": "ATTENTION",
                "findings": [{"claim": "", "evidence": "x", "fix": "y"}],
            })
        self.assertNotIn("judge-failed", steward._REAL_VERDICTS)

    def test_audit_cache_requires_complete_judge_provenance(self):
        worker = steward._prepare_audit_worker_packet({
            "verdict": "ATTENTION",
            "findings": [{
                "claim": "tracked config differs",
                "evidence": "diff output",
                "fix": "sync tracked config",
            }],
        })
        artifact = {
            "verdict": "ATTENTION",
            "worker_verdict": "ATTENTION",
            "judge_verdict": "ATTENTION",
            "worker_findings": worker["findings"],
            "judge_confirmed": [{
                "id": "finding-1",
                "claim": "tracked config differs",
                "evidence": "reproduced diff",
                "fix": "sync tracked config",
            }],
            "judge_rejected": [],
            "judge_error": "",
        }
        self.assertTrue(steward._audit_artifact_cacheable(artifact))
        artifact["judge_confirmed"] = []
        self.assertFalse(steward._audit_artifact_cacheable(artifact))
        artifact["judge_error"] = "timeout"
        self.assertFalse(steward._audit_artifact_cacheable(artifact))

    def test_version_currency_is_report_only(self):
        sections = [
            {
                "name": "version-currency",
                "verdict": "DRIFT",
                "judge_confirmed": [{"id": "finding-1", "claim": "Traefik newer"}],
                "worker_findings": [{"id": "finding-1", "claim": "Traefik newer"}],
            },
            {
                "name": "config-doc-drift",
                "verdict": "ATTENTION",
                "judge_confirmed": [{"id": "finding-1", "claim": "tracked config differs"}],
                "worker_findings": [{"id": "finding-1", "claim": "tracked config differs"}],
            },
            {
                "name": "digest-quality",
                "verdict": "PASS",
                "judge_confirmed": [],
                "worker_findings": [],
            },
        ]
        to_fix, report_only = steward._p7b_fix_candidates(sections)
        self.assertEqual(to_fix, [(
            "config-doc-drift",
            [{"id": "finding-1", "claim": "tracked config differs"}],
        )])
        self.assertEqual(report_only, [{
            "section": "version-currency",
            "status": "report-only",
            "findings_count": 1,
        }])

        invented = [{
            "name": "security-posture",
            "verdict": "ATTENTION",
            "worker_findings": [],
            "judge_confirmed": [{"claim": "historical credential unresolved"}],
        }]
        to_fix, report_only = steward._p7b_fix_candidates(invented)
        self.assertEqual(to_fix, [])
        self.assertEqual(report_only[0]["section"], "security-posture")

    def test_security_collector_retains_unresolved_incident(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            doc = home / "notes" / "docs" / "homelab" / "opencode-go-proxy.md"
            doc.parent.mkdir(parents=True)

            def collect():
                with (
                    patch.object(steward, "HOME", home),
                    patch.object(steward.urllib.request, "urlopen",
                                 side_effect=OSError("offline")),
                    patch.object(steward, "run_capture", return_value=""),
                    patch.object(steward, "_gather_repo_secrets",
                                 return_value={"findings_summary": "clean"}),
                ):
                    return steward._audit_collector_4_security()

            doc.write_text("Known credential incident: unresolved.\n")
            unresolved = collect()
            self.assertEqual(
                unresolved["known_credential_incident"]["status"], "unresolved"
            )
            verdict, confirmed = steward._apply_deterministic_audit_guards(
                "security-posture", unresolved, "PASS", []
            )
            self.assertEqual(verdict, "ATTENTION")
            self.assertEqual(len(confirmed), 1)

            doc.unlink()
            missing = collect()
            self.assertEqual(
                missing["known_credential_incident"]["status"], "unverifiable"
            )
            verdict, _ = steward._apply_deterministic_audit_guards(
                "security-posture", missing, "PASS", []
            )
            self.assertEqual(verdict, "ATTENTION")

            doc.write_text("Known credential incident: resolved.\n")
            resolved = collect()
            verdict, confirmed = steward._apply_deterministic_audit_guards(
                "security-posture", resolved, "PASS", []
            )
            self.assertEqual((verdict, confirmed), ("PASS", []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
