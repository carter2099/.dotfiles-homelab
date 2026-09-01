#!/usr/bin/env python3
"""Behavioral tests for steward delayed-update selection and rollback."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from unittest.mock import MagicMock, Mock, patch
from steward import dotfiles

from steward import audit, fixes, report, updates


class DelayedUpdateTests(unittest.TestCase):
    NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    def test_release_maturity_boundary_is_seven_days(self):
        self.assertTrue(updates._release_is_mature(
            "2026-08-18T12:00:00Z", now=self.NOW))
        self.assertFalse(updates._release_is_mature(
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
        target = updates._select_mature_searxng_tag(
            tags, "2026.8.17-aaaa1111", now=self.NOW)
        self.assertEqual(target["name"], "2026.8.18-bbbb2222")

    def test_llama_selects_newest_mature_release(self):
        releases = [
            {"tag_name": "b10453", "published_at": "2026-08-17T12:00:00Z"},
            {"tag_name": "b10488", "published_at": "2026-08-18T12:00:00Z"},
            {"tag_name": "b10500", "published_at": "2026-08-19T12:00:00Z"},
        ]
        target = updates._select_mature_llama_release(
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
                patch.object(updates, "HOME", home),
                patch.object(updates, "run_capture", return_value="2026.8.17-aaaa1111"),
                patch.object(updates, "run"),
                patch.object(updates, "_wait_searxng_healthy",
                             side_effect=[False, True]),
            ):
                result = updates._p1_searxng_update(tags=tags, now=self.NOW)
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
                patch.object(updates, "HOME", home),
                patch.object(updates, "run_capture", return_value="2026.8.17-aaaa1111"),
                patch.object(updates, "run"),
                patch.object(updates, "_wait_searxng_healthy", return_value=True),
            ):
                result = updates._p1_searxng_update(tags=tags, now=self.NOW)
            self.assertEqual(result["status"], "ok")
            self.assertIn(new_digest, compose.read_text())

    def test_openwebui_reports_update_without_mutating_production(self):
        release = {
            "tag_name": "v0.11.3",
            "draft": False,
            "prerelease": False,
            "html_url": "https://github.com/open-webui/open-webui/releases/tag/v0.11.3",
            "published_at": "2026-08-31T14:55:53Z",
        }
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(release).encode()
        with tempfile.TemporaryDirectory() as tmp:
            compose = Path(tmp) / "docker-compose.yml"
            original = (
                "services:\n  open-webui:\n"
                "    image: ghcr.io/open-webui/open-webui:0.11.1\n"
            )
            compose.write_text(original)
            with (
                patch.object(updates, "OPENWEBUI_COMPOSE", compose),
                patch.object(updates.urllib.request, "urlopen", return_value=response),
                patch.object(updates, "run") as run_mock,
                patch.object(updates, "run_capture") as run_capture_mock,
            ):
                result = updates._p1_openwebui()
            final_compose = compose.read_text()

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["current_tag"], "0.11.1")
        self.assertEqual(result["latest_tag"], "0.11.3")
        self.assertFalse(result["local_mutation"])
        self.assertEqual(final_compose, original)
        self.assertFalse(updates._p1_deploy_step_ok(result))
        run_mock.assert_not_called()
        run_capture_mock.assert_not_called()
        self.assertIn("/update-openweb-ui", report._html_updates({"steps": [result]}))
        self.assertIn(
            "open-webui update available",
            report._tldr_collect_updates({"steps": [result]})[0][0],
        )

    def test_openwebui_never_reports_a_downgrade(self):
        release = {
            "tag_name": "v0.11.3",
            "draft": False,
            "prerelease": False,
        }
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(release).encode()
        with tempfile.TemporaryDirectory() as tmp:
            compose = Path(tmp) / "docker-compose.yml"
            compose.write_text(
                "services:\n  open-webui:\n"
                "    image: ghcr.io/open-webui/open-webui:0.11.4\n"
            )
            with (
                patch.object(updates, "OPENWEBUI_COMPOSE", compose),
                patch.object(updates.urllib.request, "urlopen", return_value=response),
            ):
                result = updates._p1_openwebui()

        self.assertEqual(result["status"], "current")
        self.assertFalse(result["local_mutation"])

    def test_llama_reports_verified_remote_rollback(self):
        releases = [
            {"tag_name": "b10488", "published_at": "2026-08-18T12:00:00Z"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "update.sh"
            helper.write_text("exit 1\n")
            with (
                patch.object(updates, "LLAMA_CPP_UPDATE_SCRIPT", helper),
                patch.object(updates, "run_capture",
                             return_value="/opt/llama.cpp/b10453/bin/llama-server"),
                patch.object(updates, "run_capture_ok",
                             return_value=("", "ROLLBACK_OK b10453", 1)),
            ):
                result = updates._p1_llama_cpp_update(
                    releases=releases, now=self.NOW)
            self.assertEqual(result["status"], "reverted")
            self.assertEqual(result["reverted_to"], "b10453")

    def test_judge_verdict_overrides_worker_pass(self):
        packet = {
            "verdict": "ATTENTION",
            "confirmed": [{"claim": "manual action remains"}],
        }
        self.assertEqual(audit._final_audit_verdict("PASS", packet), "ATTENTION")
        self.assertEqual(audit._final_audit_verdict("DRIFT", {}), "UNVERIFIABLE")
        self.assertEqual(
            audit._final_audit_verdict(
                "PASS", {"confirmed": [{"claim": "missing judge verdict"}]}
            ),
            "UNVERIFIABLE",
        )
        self.assertEqual(
            audit._final_audit_verdict(
                "DRIFT", {"judge_error": "timeout", "confirmed": []}
            ),
            "judge-failed",
        )

    def test_audit_judge_packet_schema_fails_closed(self):
        worker = audit._prepare_audit_worker_packet({
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
                audit._validate_audit_judge_packet(packet, worker)
        valid = {
            "verdict": "PASS",
            "confirmed": [],
            "rejected": [{
                "id": "finding-1",
                "claim": "tracked config differs",
                "reason": "not reproduced",
            }],
        }
        self.assertIs(audit._validate_audit_judge_packet(valid, worker), valid)
        with self.assertRaises(ValueError):
            audit._prepare_audit_worker_packet({
                "verdict": "ATTENTION",
                "findings": [{"claim": "", "evidence": "x", "fix": "y"}],
            })

    def test_worker_packet_retry_on_prose_only_output(self):
        section = {"name": "agent-fleet-review", "guidance": "inspect", "timeout": 600}
        prose_cut_short = (
            "Timers firing correctly so far. Now digging into dependabot outcomes... "
            "Need to veri"
        )
        retry_packet = (
            '```json\n{"verdict": "UNVERIFIABLE", "findings": ['
            '{"claim": "fleet checks incomplete", "evidence": "run cut short", '
            '"fix": "re-run section manually"}]}\n```'
        )
        judge_packet = (
            '```json\n{"verdict": "UNVERIFIABLE", "confirmed": [], "rejected": ['
            '{"id": "finding-1", "claim": "fleet checks incomplete", '
            '"reason": "not independently verified"}]}\n```'
        )
        with patch.object(
            audit, "_call_omp_p",
            side_effect=[prose_cut_short, retry_packet, judge_packet],
        ) as mock_call:
            result = audit._run_audit_agent_pair(section, {}, "hash1")
        self.assertEqual(result["verdict"], "UNVERIFIABLE")
        self.assertNotEqual(result["verdict"], "worker-failed")
        self.assertEqual(mock_call.call_count, 3)
        self.assertIn("Emit ONLY the fenced", mock_call.call_args_list[1].args[0])

    def test_worker_failed_persists_only_after_retry_missing_packet(self):
        section = {"name": "agent-fleet-review", "guidance": "inspect", "timeout": 600}
        prose = "investigating fleet state without ever emitting a packet"
        with patch.object(
            audit, "_call_omp_p",
            side_effect=[prose, prose],
        ) as mock_call:
            result = audit._run_audit_agent_pair(section, {}, "hash1")
        self.assertEqual(result["verdict"], "worker-failed")
        self.assertIn("retry also failed", result["error"])
        self.assertEqual(mock_call.call_count, 2)

    def test_audit_cache_requires_complete_judge_provenance(self):
        worker = audit._prepare_audit_worker_packet({
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
        self.assertTrue(audit._audit_artifact_cacheable(artifact))
        artifact["judge_confirmed"] = []
        self.assertFalse(audit._audit_artifact_cacheable(artifact))
        artifact["judge_error"] = "timeout"
        self.assertFalse(audit._audit_artifact_cacheable(artifact))

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
        to_fix, report_only = fixes._p7b_fix_candidates(sections)
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
        to_fix, report_only = fixes._p7b_fix_candidates(invented)
        self.assertEqual(to_fix, [])
        self.assertEqual(report_only[0]["section"], "security-posture")

    def test_security_collector_retains_unresolved_incident(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            doc = home / "notes" / "docs" / "homelab" / "opencode-go-proxy.md"
            doc.parent.mkdir(parents=True)

            def collect():
                with (
                    patch.object(audit, "HOME", home),
                    patch.object(audit.urllib.request, "urlopen",
                                 side_effect=OSError("offline")),
                    patch.object(audit, "run_capture", return_value=""),
                    patch.object(audit, "_gather_repo_secrets",
                                 return_value={"findings_summary": "clean"}),
                ):
                    return audit._audit_collector_4_security()

            doc.write_text("Known credential incident: unresolved.\n")
            unresolved = collect()
            self.assertEqual(
                unresolved["known_credential_incident"]["status"], "unresolved"
            )
            verdict, confirmed = audit._apply_deterministic_audit_guards(
                "security-posture", unresolved, "PASS", []
            )
            self.assertEqual(verdict, "ATTENTION")
            self.assertEqual(len(confirmed), 1)

            doc.unlink()
            missing = collect()
            self.assertEqual(
                missing["known_credential_incident"]["status"], "unverifiable"
            )
            verdict, _ = audit._apply_deterministic_audit_guards(
                "security-posture", missing, "PASS", []
            )
            self.assertEqual(verdict, "ATTENTION")

            doc.write_text("Known credential incident: resolved.\n")
            resolved = collect()
            verdict, confirmed = audit._apply_deterministic_audit_guards(
                "security-posture", resolved, "PASS", []
            )
            self.assertEqual((verdict, confirmed), ("PASS", []))


class GamingRigMaintenanceTests(unittest.TestCase):
    def _linux_probe(self):
        return (
            {
                "step": "platform_probe",
                "status": "ok",
                "os": "Linux",
                "host": updates.RIG_SSH_ALIAS,
            },
            True,
        )

    def _healthy(self, step="health"):
        return {
            "step": step,
            "status": "ok",
            "checks": [
                {"step": "disk", "status": "ok"},
                {"step": "failed_units", "status": "ok"},
                {"step": "nvidia_smi", "status": "ok"},
                {"step": "llama_swap", "status": "ok"},
                {"step": "model_endpoint", "status": "ok"},
            ],
        }

    @staticmethod
    def _json_response(payload):
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = None
        response.read.return_value = json.dumps(payload).encode()
        return response

    def test_ssh_argv_is_pinned_bounded_and_quoted(self):
        argv = updates._rig_ssh_command(
            ["bun", "add", "-g", "pkg@1.2.3;touch /tmp/should-not-run"]
        )
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectTimeout=10", argv)
        self.assertIn("gamingrig-linux", argv)
        self.assertNotIn("StrictHostKeyChecking=no", argv)
        self.assertIn(
            "'pkg@1.2.3;touch /tmp/should-not-run'",
            argv,
        )
        self.assertIn("env", argv)
        self.assertIn(f"PATH={updates.RIG_REMOTE_PATH}", argv)
        self.assertEqual(
            updates.RIG_MODEL_ENDPOINT,
            "http://192.168.4.103:8080/v1/models",
        )

    def test_offline_rig_is_skipped_without_follow_up_commands(self):
        with patch.object(
            updates, "_rig_ssh",
            return_value=("", "Connection timed out", 255),
        ) as ssh:
            result = updates._p1_gamingrig_maintenance()
        self.assertEqual(result["status"], "skipped")
        self.assertIn("offline or sleeping", result["reason"])
        ssh.assert_called_once()

    def test_refusal_and_no_route_are_the_other_offline_signatures(self):
        for message in ("Connection refused", "No route to host"):
            with self.subTest(message=message), patch.object(
                updates, "_rig_ssh", return_value=("", message, 255)
            ):
                result = updates._p1_gamingrig_maintenance()
            self.assertEqual(result["status"], "skipped")
            self.assertIn("offline or sleeping", result["reason"])


    def test_windows_rig_is_skipped_only_with_trusted_proxy_corroboration(self):
        with (
            patch.object(
                updates, "_rig_ssh",
                return_value=("MINGW64_NT-10.0", "", 0),
            ) as ssh,
            patch.object(
                updates.urllib.request,
                "urlopen",
                return_value=self._json_response({"rig_os": "windows"}),
            ) as urlopen,
        ):
            result = updates._p1_gamingrig_maintenance()
        self.assertEqual(result["status"], "skipped")
        self.assertIn("trusted llm-proxy", result["reason"])
        self.assertEqual(urlopen.call_count, 1)
        ssh.assert_called_once()

    def test_uncorroborated_windows_probe_is_failure(self):
        with (
            patch.object(
                updates, "_rig_ssh",
                return_value=("MINGW64_NT-10.0", "", 0),
            ) as ssh,
            patch.object(
                updates.urllib.request,
                "urlopen",
                return_value=self._json_response({"rig_os": "linux"}),
            ),
        ):
            result = updates._p1_gamingrig_maintenance()
        self.assertEqual(result["status"], "failed")
        self.assertIn("not corroborated", result["error"])
        ssh.assert_called_once()

    def test_auth_failure_is_not_classified_as_offline(self):
        with patch.object(
            updates, "_rig_ssh",
            return_value=("", "Permission denied (publickey)", 255),
        ) as ssh:
            result = updates._p1_gamingrig_maintenance()
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("offline or sleeping", result.get("reason", ""))
        ssh.assert_called_once()

    def test_host_key_mismatch_is_failure_without_windows_corroboration(self):
        with (
            patch.object(
                updates, "_rig_ssh",
                return_value=("", "Host key verification failed", 255),
            ) as ssh,
            patch.object(
                updates, "_rig_windows_health_corroboration",
                return_value={"status": "failed", "error": "rig_os=linux"},
            ),
        ):
            result = updates._p1_gamingrig_maintenance()
        self.assertEqual(result["status"], "failed")
        self.assertIn("host key mismatch", result["reason"])
        self.assertIn("did not corroborate Windows", result["error"])
        ssh.assert_called_once()

    def test_linux_host_key_mismatch_is_skipped_when_windows_is_corroborated(self):
        with (
            patch.object(
                updates, "_rig_ssh",
                return_value=("", "Host key verification failed", 255),
            ) as ssh,
            patch.object(
                updates, "_rig_windows_health_corroboration",
                return_value={"status": "ok", "rig_os": "windows"},
            ),
        ):
            result = updates._p1_gamingrig_maintenance()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["substeps"][0]["os"], "Windows")
        self.assertIn("trusted llm-proxy", result["reason"])
        ssh.assert_called_once()


    def test_linux_noop_maintenance_is_success(self):
        with (
            patch.object(updates, "_rig_platform_probe",
                         return_value=self._linux_probe()),
            patch.object(updates, "_rig_apt_upgrade",
                         return_value={"step": "apt_upgrade", "status": "ok",
                                       "upgraded_count": 0}),
            patch.object(updates, "_rig_herdr_update",
                         return_value={"step": "herdr_update", "status": "skipped",
                                       "reason": "already current"}),

            patch.object(updates, "_rig_omp_update",
                         return_value={"step": "omp_update", "status": "skipped",
                                       "reason": "already current"}),
            patch.object(updates, "_rig_health_checks",
                         return_value=self._healthy()),
            patch.object(updates, "_rig_reboot_required",
                         return_value={"step": "reboot_required", "status": "skipped",
                                       "required": False}),
        ):
            result = updates._p1_gamingrig_maintenance()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result.get("rebooted"))
        self.assertEqual(len(result["substeps"]), 6)

    def test_apt_upgrade_reports_planned_and_actual_change_counts(self):
        responses = [
            {"step": "apt_update", "status": "ok", "stdout_tail": "ok"},
            {
                "step": "apt_upgrade_plan",
                "status": "ok",
                "stdout_tail": "3 upgraded, 0 newly installed, 0 to remove.",
            },
            {
                "step": "apt_upgrade_apply",
                "status": "ok",
                "stdout_tail": "1 upgraded, 0 newly installed, 0 to remove.",
            },
        ]
        with patch.object(
            updates, "_rig_command_result", side_effect=responses
        ) as command:
            result = updates._rig_apt_upgrade()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["planned_count"], 3)
        self.assertEqual(result["upgraded_count"], 1)
        self.assertEqual(command.call_count, 3)

    def test_apt_upgrade_parses_full_apply_output_before_tail(self):
        full_apply = "2 upgraded, 0 newly installed, 0 to remove.\n" + ("x" * 2000)
        responses = [
            {"step": "apt_update", "status": "ok", "stdout_tail": "ok"},
            {
                "step": "apt_upgrade_plan",
                "status": "ok",
                "stdout_tail": "5 upgraded, 0 newly installed, 0 to remove.",
            },
            {
                "step": "apt_upgrade_apply",
                "status": "ok",
                "stdout_tail": full_apply[-700:],
                "_full_stdout": full_apply,
                "_full_stderr": "",
            },
        ]
        with patch.object(updates, "_rig_command_result", side_effect=responses):
            result = updates._rig_apt_upgrade()
        self.assertEqual(result["upgraded_count"], 2)
        self.assertEqual(result["planned_count"], 5)
        self.assertNotIn("_full_stdout", result["substeps"][-1])

    def test_model_endpoint_requires_exact_retained_ids(self):
        expected = list(updates.RIG_REQUIRED_MODEL_IDS)
        payload = {
            "object": "list",
            "data": [{"id": model_id} for model_id in expected],
        }
        response = json.dumps(payload)
        responses = [
            {"step": "disk", "status": "ok", "stdout_tail": "/dev/root 50%"},
            {"step": "failed_units", "status": "ok", "exit_code": 0},
            {"step": "nvidia_smi", "status": "ok", "stdout_tail": "RTX 5070"},
            {"step": "llama_swap", "status": "ok", "exit_code": 0},
            {
                "step": "model_endpoint",
                "status": "ok",
                "stdout_tail": response[-700:],
                "_full_stdout": response,
                "_full_stderr": "",
            },
        ]
        with patch.object(updates, "_rig_command_result", side_effect=responses):
            result = updates._rig_health_checks()
        model_check = result["checks"][-1]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(model_check["model_ids"], expected)
        self.assertNotIn("_full_stdout", model_check)

    def test_model_endpoint_malformed_empty_and_missing_ids_fail_health(self):
        expected = list(updates.RIG_REQUIRED_MODEL_IDS)
        invalid_payloads = [
            "",
            json.dumps({"object": "list", "data": []}),
            json.dumps({
                "object": "list",
                "data": [{"id": model_id} for model_id in expected[:-1]],
            }),
            json.dumps({
                "object": "list",
                "data": [{"id": model_id} for model_id in expected]
                + [{"id": "unexpected-model"}],
            }),
            json.dumps({
                "object": "list",
                "data": [{"name": expected[0]}]
                + [{"id": model_id} for model_id in expected[1:]],
            }),
            json.dumps({
                "object": "list",
                "data": [{"id": model_id} for model_id in expected[:-1]]
                + [{"id": expected[0]}],
            }),
            "{not-json",
        ]
        for response in invalid_payloads:
            with self.subTest(response=response[:30]):
                responses = [
                    {"step": "disk", "status": "ok", "stdout_tail": "/dev/root 50%"},
                    {"step": "failed_units", "status": "ok", "exit_code": 0},
                    {"step": "nvidia_smi", "status": "ok", "stdout_tail": "RTX 5070"},
                    {"step": "llama_swap", "status": "ok", "exit_code": 0},
                    {
                        "step": "model_endpoint",
                        "status": "ok",
                        "stdout_tail": response[-700:],
                        "_full_stdout": response,
                        "_full_stderr": "",
                    },
                ]
                with patch.object(
                    updates, "_rig_command_result", side_effect=responses
                ):
                    result = updates._rig_health_checks()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["checks"][-1]["status"], "failed")

    def test_omp_update_rolls_back_with_bun_after_broken_smoke(self):
        responses = [
            ("omp/1.0.0", "", 0),       # pre-version
            ("", "update failed", 1),   # update
            ("omp/1.1.0", "", 0),       # post-version
            ("", "broken binary", 1),   # post-update smoke
            ("", "", 0),                # bun rollback
            ("omp/1.0.0", "", 0),       # reverted version
            ("", "", 0),                # reverted smoke
        ]
        with patch.object(updates, "_rig_ssh", side_effect=responses) as ssh:
            result = updates._rig_omp_update()
        self.assertEqual(result["status"], "reverted")
        self.assertEqual(result["reverted_to"], "omp/1.0.0")
        rollback_call = ssh.call_args_list[4].args[0]
        self.assertEqual(rollback_call[-1],
                         "@oh-my-pi/pi-coding-agent@1.0.0")
        self.assertNotIn(";", " ".join(rollback_call))


    def test_wait_for_linux_requires_a_changed_boot_id(self):
        old_boot = "11111111-1111-1111-1111-111111111111"
        new_boot = "22222222-2222-2222-2222-222222222222"
        with (
            patch.object(
                updates,
                "_rig_ssh",
                side_effect=[
                    (old_boot, "", 0),
                    (new_boot, "", 0),
                ],
            ),
            patch.object(updates.time, "monotonic", side_effect=[0, 1]),
            patch.object(updates.time, "sleep"),

        ):
            result = updates._rig_wait_for_linux(old_boot, timeout_s=2)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["boot_id"], new_boot)
        self.assertEqual(result["attempts"], 2)
    def test_post_reboot_health_polls_until_every_check_is_healthy(self):
        failed = self._healthy()
        failed["status"] = "failed"
        failed["error"] = "model_endpoint: HTTP 503"
        with (
            patch.object(
                updates, "_rig_health_checks",
                side_effect=[failed, self._healthy()],
            ) as health,
            patch.object(updates.time, "monotonic", side_effect=[0, 0]),
            patch.object(updates.time, "sleep") as sleep,
        ):
            result = updates._rig_wait_for_health(timeout_s=2)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["step"], "post_reboot_health")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(health.call_count, 2)
        sleep.assert_called_once()

    def test_reboot_accepts_established_disconnect_but_not_timeout(self):
        with patch.object(
            updates, "_rig_ssh",
            return_value=("", "Connection timed out", 255),
        ):
            timed_out = updates._rig_reboot()
        self.assertEqual(timed_out["status"], "failed")
        self.assertFalse(timed_out["requested"])

        with patch.object(
            updates, "_rig_ssh",
            return_value=("", "Connection to gamingrig-linux closed", 255),
        ):
            closed = updates._rig_reboot()
        self.assertEqual(closed["status"], "ok")
        self.assertTrue(closed["requested"])

    def test_reboot_rearms_bootnext_waits_for_linux_and_rechecks_health(self):
        with (
            patch.object(updates, "_rig_platform_probe",
                         return_value=self._linux_probe()),
            patch.object(updates, "_rig_apt_upgrade",
                         return_value={"step": "apt_upgrade", "status": "ok",
                                       "upgraded_count": 1}),
            patch.object(updates, "_rig_herdr_update",
                         return_value={"step": "herdr_update", "status": "skipped"}),
            patch.object(updates, "_rig_omp_update",
                         return_value={"step": "omp_update", "status": "skipped"}),
            patch.object(updates, "_rig_health_checks",
                         side_effect=[self._healthy(), self._healthy("post_reboot_health")]) as health,
            patch.object(updates, "_rig_reboot_required",
                         return_value={"step": "reboot_required", "status": "ok",
                                       "required": True}),
            patch.object(updates, "_rig_arm_bootnext",
                         return_value={"step": "bootnext", "status": "ok",
                                       "entry": "0001"}),
            patch.object(updates, "_rig_boot_id",
                         return_value={"step": "pre_reboot_boot_id", "status": "ok",
                                       "boot_id": "11111111-1111-1111-1111-111111111111"}),
            patch.object(updates, "_rig_reboot",
                         return_value={"step": "reboot", "status": "ok"}),
            patch.object(updates, "_rig_wait_for_linux",
                         return_value={"step": "ssh_return", "status": "ok",
                                       "os": "Linux"}) as wait,
        ):
            result = updates._p1_gamingrig_maintenance()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["reboot_requested"])
        self.assertTrue(result["new_boot_observed"])
        self.assertTrue(result["rebooted"])

        self.assertTrue(result["post_reboot_health_passed"])
        self.assertEqual(health.call_count, 2)
        self.assertEqual(result["post_reboot_health"]["status"], "ok")
        wait.assert_called_once_with(
            "11111111-1111-1111-1111-111111111111")
    def test_failed_ssh_return_does_not_claim_rebooted_or_rechecked(self):
        with (
            patch.object(updates, "_rig_platform_probe",
                         return_value=self._linux_probe()),
            patch.object(updates, "_rig_apt_upgrade",
                         return_value={"step": "apt_upgrade", "status": "ok",
                                       "upgraded_count": 0}),
            patch.object(updates, "_rig_herdr_update",
                         return_value={"step": "herdr_update", "status": "skipped"}),
            patch.object(updates, "_rig_omp_update",
                         return_value={"step": "omp_update", "status": "skipped"}),
            patch.object(updates, "_rig_health_checks",
                         return_value=self._healthy()),
            patch.object(updates, "_rig_reboot_required",
                         return_value={"step": "reboot_required", "status": "ok",
                                       "required": True}),
            patch.object(updates, "_rig_arm_bootnext",
                         return_value={"step": "bootnext", "status": "ok"}),
            patch.object(updates, "_rig_boot_id",
                         return_value={"step": "pre_reboot_boot_id", "status": "ok",
                                       "boot_id": "11111111-1111-1111-1111-111111111111"}),
            patch.object(updates, "_rig_reboot",
                         return_value={"step": "reboot", "status": "ok",
                                       "requested": True}),
            patch.object(updates, "_rig_wait_for_linux",
                         return_value={"step": "ssh_return", "status": "failed",
                                       "error": "new boot not observed"}),
        ):
            result = updates._p1_gamingrig_maintenance()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["reboot_requested"])
        self.assertFalse(result["new_boot_observed"])
        self.assertFalse(result["rebooted"])
        self.assertFalse(result["post_reboot_health_passed"])
        rendered = report._html_gamingrig_update(result)
        self.assertIn("ssh_return", rendered)
        self.assertNotIn("rebooted;", rendered)


    def test_post_update_health_failure_is_result_data(self):
        failed = self._healthy()
        failed["status"] = "failed"
        failed["checks"][-1] = {
            "step": "model_endpoint",
            "status": "failed",
            "error": "HTTP 503",
        }
        failed["error"] = "model_endpoint: HTTP 503"
        with (
            patch.object(updates, "_rig_platform_probe",
                         return_value=self._linux_probe()),
            patch.object(updates, "_rig_apt_upgrade",
                         return_value={"step": "apt_upgrade", "status": "ok",
                                       "upgraded_count": 0}),
            patch.object(updates, "_rig_herdr_update",
                         return_value={"step": "herdr_update", "status": "skipped"}),
            patch.object(updates, "_rig_omp_update",
                         return_value={"step": "omp_update", "status": "skipped"}),
            patch.object(updates, "_rig_health_checks", return_value=failed),
            patch.object(updates, "_rig_reboot_required",
                         return_value={"step": "reboot_required", "status": "skipped",
                                       "required": False}),
        ):
            result = updates._p1_gamingrig_maintenance()
        self.assertEqual(result["status"], "failed")
        self.assertIn("model_endpoint", result["error"])

    def test_gamingrig_updates_are_signal_only_and_reach_tldr(self):
        applied = {
            "steps": [{
                "step": "gamingrig_maintenance",
                "host": "gamingrig-linux",
                "status": "failed",
                "substeps": [
                    {"step": "apt_upgrade", "status": "ok", "upgraded_count": 0},
                    {"step": "omp_update", "status": "reverted",
                     "reverted_to": "omp/1.0.0"},
                    {"step": "health", "status": "failed", "checks": [{
                        "step": "model_endpoint", "status": "failed",
                        "error": "HTTP 503",
                    }]},
                ],
            }],
        }
        html = report._html_updates(applied)
        updates, failures = report._tldr_collect_updates(applied)

        self.assertIn("rolled back", html)
        self.assertIn("model_endpoint", html)
        self.assertEqual(failures, 2)
        self.assertTrue(any("rolled back" in item for item in updates))
        self.assertTrue(any("HTTP 503" in item for item in updates))
    def test_all_failed_reboot_gates_are_rendered(self):
        names = (
            "reboot_required", "pre_reboot_boot_id", "bootnext",
            "reboot", "ssh_return", "post_reboot_health",
        )
        substeps = [
            {"step": name, "status": "failed", "error": f"{name} failed"}
            for name in names
        ]
        rendered = report._html_gamingrig_update({
            "step": "gamingrig_maintenance",
            "host": "gamingrig-linux",
            "status": "failed",
            "substeps": substeps,
        })
        for name in names:
            self.assertIn(name, rendered)

    def test_rig_apply_failures_lead_tldr_health_and_needs_carter(self):
        applied = {
            "steps": [
                {
                    "step": "other_update",
                    "status": "ok",
                },
                {
                    "step": "gamingrig_maintenance",
                    "host": "gamingrig-linux",
                    "status": "failed",
                    "substeps": [{
                        "step": "apt_upgrade",
                        "status": "failed",
                        "error": "apt unavailable",
                    }],
                },
            ],
        }
        facts = report._build_tldr_facts(
            applied,
            {"sections": []},
            {"plans": {}, "ideas": {}},
            {"sections": []},
            {},
        )
        self.assertFalse(facts["health_ok"])

        self.assertIn("apt unavailable", facts["health_issues"][0])
        self.assertIn("gaming-rig apply failure", facts["needs_carter"][0])
        deterministic = report._tldr_deterministic(facts)
        self.assertTrue(deterministic.startswith("Health issues:"))
        self.assertIn("apt unavailable", deterministic)
    def test_gamingrig_runs_before_local_apt_failure(self):
        calls = []
        remote = {
            "step": "gamingrig_maintenance",
            "host": updates.RIG_SSH_ALIAS,
            "status": "ok",
            "local_mutation": False,
            "substeps": [],
        }
        local_failure = {
            "step": "apt_upgrade",
            "status": "failed",
            "error": "apt failed",
        }
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)

            def run_remote(*, dry_run=False):
                calls.append("rig")
                return remote

            def run_apt():
                calls.append("apt")
                return local_failure

            with (
                patch.object(updates, "_p1_gamingrig_maintenance", run_remote),
                patch.object(updates, "_p1_apt_upgrade", run_apt),
            ):
                result = updates.phase_1_apply(run_dir)
        self.assertEqual(calls, ["rig", "apt"])
        self.assertEqual(result["steps"][0]["step"], "gamingrig_maintenance")

class DotfilesP9bTests(unittest.TestCase):
    def _git(self, *args, cwd=None):
        return subprocess.run(
            ["/usr/bin/git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    def _fixture_repo(self, tmp):
        bare = tmp / "dotfiles.git"
        work = tmp / "home"
        self._git("init", "--bare", str(bare))
        self._git("clone", str(bare), str(work))
        self._git("config", "user.name", "P9b Test", cwd=work)
        self._git("config", "user.email", "p9b@example.test", cwd=work)
        config = work / ".config" / "demo"
        config.parent.mkdir(parents=True)
        config.write_text("initial\n")
        self._git("add", "--", ".config/demo", cwd=work)
        self._git("commit", "-m", "initial", cwd=work)
        self._git("push", "origin", "HEAD:main", cwd=work)
        self._git("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")
        self._git("--git-dir", str(bare), "config", "core.bare", "false")
        self._git("--git-dir", str(bare), "config", "core.worktree", str(work))
        self._git("--git-dir", str(bare), "config", "user.name", "P9b Test")
        self._git("--git-dir", str(bare), "config", "user.email", "p9b@example.test")
        return bare, work, config

    def test_recursive_session_evidence_and_exit_marker(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name) / "sessions"
            nested = root / "project" / "deeper"
            nested.mkdir(parents=True)
            active = nested / "active.jsonl"
            active.write_text(json.dumps({
                "type": "session",
                "id": "active-1",
                "title": "Bounded title",
                "cwd": "/tmp/project",
            }) + "\n" + json.dumps({
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "edited .config/demo"}],
                },
            }) + "\n")
            evidence = dotfiles.collect_active_session_evidence(
                root,
                now=datetime.now(timezone.utc),
            )
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0]["session_id"], "active-1")
            active.write_text(active.read_text() + json.dumps({
                "type": "custom",
                "customType": "session_exit",
            }) + "\n")
            self.assertEqual(
                dotfiles.collect_active_session_evidence(
                    root,
                    now=datetime.now(timezone.utc),
                ),
                [],
            )

    def test_strict_classification_rejects_missing_or_extra_paths(self):
        with self.assertRaises(ValueError):
            dotfiles.validate_classification_packet(
                {"classifications": [{
                    "path": ".config/demo",
                    "classification": "unrelated",
                    "reason": "ok",
                }]},
                [".config/demo", ".zshrc"],
            )
        with self.assertRaises(ValueError):
            dotfiles.validate_classification_packet(
                {
                    "classifications": [{
                        "path": ".config/demo",
                        "classification": "unknown",
                        "reason": "ok",
                    }],
                    "extra": True,
                },
                [".config/demo"],
            )

    def test_parent_commits_exact_unrelated_path_without_push(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            bare, home, config = self._fixture_repo(Path(tmp_name))
            config.write_text("changed by test\n")
            run_dir = home / "run"
            run_dir.mkdir()
            response = lambda prompt, **kwargs: (
                '```json\n{"classifications":[{"path": ".config/demo", '
                '"classification": "unrelated", "reason": "test fixture"}]}\n```'
            )
            result = dotfiles.phase_9b_dotfiles(
                run_dir,
                home=home,
                git_dir=bare,
                session_root=home / "sessions",
                push=False,
                omp_call=response,
            )
            self.assertEqual(result["status"], "committed")
            self.assertEqual(result["push"], "not_requested")
            self.assertEqual(result["paths"], [".config/demo"])
            self.assertEqual(
                self._git(
                    "--git-dir", str(bare), "show", "--format=", "HEAD:.config/demo"
                ).stdout,
                "changed by test\n",
            )

    def test_classifier_is_toolless_and_artifact_omits_raw_context_and_diffs(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            bare, home, config = self._fixture_repo(Path(tmp_name))
            config.write_text("changed by test\n")
            session_root = home / "sessions"
            session_root.mkdir()
            (session_root / "active.jsonl").write_text(json.dumps({
                "type": "session",
                "id": "active-root",
                "title": "Root session",
                "cwd": str(home),
            }) + "\n" + json.dumps({
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "unrelated active work"}],
                },
            }) + "\n")
            captured = {}

            def response(prompt, **kwargs):
                captured["prompt"] = prompt
                captured["kwargs"] = kwargs
                return json.dumps({"classifications": [{
                    "path": ".config/demo",
                    "classification": "unrelated",
                    "reason": "different task",
                }]})

            run_dir = home / "run"
            run_dir.mkdir()
            result = dotfiles.phase_9b_dotfiles(
                run_dir,
                dry_run=True,
                home=home,
                git_dir=bare,
                session_root=session_root,
                push=False,
                omp_call=response,
            )
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(
                tuple(captured["kwargs"]["extra_args"]),
                dotfiles.CLASSIFIER_ISOLATION_ARGS,
            )
            artifact = json.loads((run_dir / "09b-dotfiles.json").read_text())
            self.assertNotIn("diffs", artifact)
            self.assertNotIn("recent_context", json.dumps(artifact))
            self.assertNotIn("changed by test", json.dumps(artifact))

    def test_reviewed_diff_race_is_rejected_before_staging(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            bare, home, config = self._fixture_repo(Path(tmp_name))
            config.write_text("first version\n")

            def response(prompt, **kwargs):
                config.write_text("second version\n")
                return json.dumps({"classifications": [{
                    "path": ".config/demo",
                    "classification": "unrelated",
                    "reason": "test fixture",
                }]})

            run_dir = home / "run"
            run_dir.mkdir()
            result = dotfiles.phase_9b_dotfiles(
                run_dir,
                home=home,
                git_dir=bare,
                session_root=home / "sessions",
                push=False,
                omp_call=response,
            )
            self.assertEqual(result["status"], "ambiguous")
            self.assertIn("diff changed", result["reason"])
            self.assertEqual(
                self._git(
                    "--git-dir", str(bare), "show", "--format=", "HEAD:.config/demo"
                ).stdout,
                "initial\n",
            )

    def test_credential_like_diff_is_never_sent_to_classifier(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            bare, home, config = self._fixture_repo(Path(tmp_name))
            config.write_text('api_key = "not-a-real-but-secret-shaped-value"\n')
            run_dir = home / "run"
            run_dir.mkdir()
            response = Mock(side_effect=AssertionError("sensitive diff reached OMP"))
            result = dotfiles.phase_9b_dotfiles(
                run_dir,
                home=home,
                git_dir=bare,
                session_root=home / "sessions",
                push=False,
                omp_call=response,
            )
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(
                result["classifications"][".config/demo"]["classification"],
                "sensitive",
            )
            response.assert_not_called()

    def test_malformed_recent_session_skips_all_paths(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            bare, home, config = self._fixture_repo(Path(tmp_name))
            config.write_text("changed by test\n")
            session_root = home / "sessions" / "nested"
            session_root.mkdir(parents=True)
            (session_root / "broken.jsonl").write_text("{not-json\n")
            run_dir = home / "run"
            run_dir.mkdir()
            response = Mock(side_effect=AssertionError("OMP must not classify ambiguity"))
            result = dotfiles.phase_9b_dotfiles(
                run_dir,
                home=home,
                git_dir=bare,
                session_root=home / "sessions",
                push=False,
                omp_call=response,
            )
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(
                result["classifications"][".config/demo"]["classification"],
                "ambiguous",
            )
            response.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
