import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.harness import DeviceHarness, HarnessError
from scripts.integration import (FIXTURE_LOG_TAG, IntegrationFailure, artifact_evidence,
                                 assert_button_label, require_tagged_log_evidence, safe_snapshot,
                                 wait_for_framework_ready)


class ReadinessClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class DeviceOwnershipTests(unittest.TestCase):
    @staticmethod
    def bare_harness(token="ci-test-secret-token"):
        harness = DeviceHarness.__new__(DeviceHarness)
        harness.token = token
        harness.serial = "emulator-5554"
        return harness

    def test_forward_collision_refuses_before_install_and_cleanup(self):
        devices = subprocess.CompletedProcess([], 0, "List of devices attached\nemulator-5554 device\n", "")
        forwards = subprocess.CompletedProcess([], 0, "emulator-5556 tcp:7777 tcp:8888\n", "")
        with patch("scripts.harness.adb_path", return_value="adb"), \
             patch("scripts.harness.subprocess.run", side_effect=[devices, forwards]) as run, \
             patch.dict(os.environ, {"ANDROID_SERIAL": ""}):
            harness = DeviceHarness()
            try:
                with self.assertRaisesRegex(HarnessError, "already forwarded"):
                    harness.start("missing-agent.apk", "missing-fixture.apk")
            finally:
                harness.cleanup()
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[1].args[0], ["adb", "forward", "--list"])

    def test_failure_snapshot_keeps_fixture_state_and_redacts_password_node(self):
        safe = safe_snapshot({"snapshotId": 7, "nodes": [
            {"id": 1, "testId": "count", "role": "button", "text": "Count: 1", "rect": [0, 0, 10, 10]},
            {"id": 2, "testId": "app:id/password", "role": "password", "text": "do-not-save", "rect": [0, 10, 10, 20]},
        ]})
        rendered = str(safe)
        self.assertIn("Count: 1", rendered)
        self.assertNotIn("do-not-save", rendered)
        self.assertNotIn("password", rendered)

    def test_log_evidence_uses_fixture_tag_and_rejects_wrong_tag(self):
        lines = ["I AshnazgFixture: count=1", "I AshnazgFixture: count=2"]
        require_tagged_log_evidence(lines, FIXTURE_LOG_TAG, "count=2")
        with self.assertRaises(IntegrationFailure):
            require_tagged_log_evidence(["I AshnazgAgent: count=2"], FIXTURE_LOG_TAG, "count=2")

    def test_artifact_manifest_includes_only_current_run_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            old = base / "run-old"
            current = base / "run-current"
            old.mkdir()
            current.mkdir()
            (old / "compose.png").write_bytes(b"old")
            (old / "failure-snapshot.json").write_text("{}")
            (current / "fixture-overlay.png").write_bytes(b"current")
            (current / "failure-logs.json").write_text("[]")
            screenshots, diagnostics = artifact_evidence(current)
            self.assertEqual([Path(p).name for p in screenshots], ["fixture-overlay.png"])
            self.assertEqual([Path(p).name for p in diagnostics], ["failure-logs.json"])

    def test_failure_diagnostics_are_disabled_without_explicit_opt_in(self):
        harness = self.bare_harness()
        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict(os.environ, {"ASHNAZG_CI_DIAGNOSTICS": "0"}), \
             patch.object(harness, "adb_cmd") as adb:
            output = Path(temporary) / "diagnostics"
            self.assertIsNone(harness.collect_failure_diagnostics(output))
            self.assertFalse(output.exists())
            adb.assert_not_called()

    def test_opted_in_failure_diagnostics_are_bounded_and_token_redacted(self):
        token = "private-ci-session-token"
        harness = self.bare_harness(token)
        fake = subprocess.CompletedProcess([], 0, "x" * 100_000 + token + "tail",
                                           "e" * 10_000 + token + "stderr-tail")
        timeout = subprocess.TimeoutExpired(["adb", "logcat"], 20,
                                            output="timeout-start " + token + " timeout-end",
                                            stderr="timeout-stderr " + token)
        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict(os.environ, {"ASHNAZG_CI_DIAGNOSTICS": "1"}), \
             patch.object(harness, "adb_cmd", side_effect=[timeout] + [fake] * 6) as adb:
            path = harness.collect_failure_diagnostics(Path(temporary))
            self.assertEqual(adb.call_count, 7)
            self.assertIsNotNone(path)
            content = path.read_text()
            diagnostic = json.loads(content)["diagnostics"]
            self.assertNotIn(token, content)
            self.assertIn("<redacted>", content)
            self.assertIn("activity_last_anr", content)
            self.assertIn("logcat_tail", content)
            self.assertIn("agent_meminfo", content)
            self.assertIn("<redacted>", diagnostic["activity_last_anr"]["stdout"])
            self.assertIn("<redacted>", diagnostic["activity_last_anr"]["stderr"])
            self.assertLessEqual(len(diagnostic["logcat_tail"]["stdout"]), 64_000)
            self.assertLessEqual(len(diagnostic["logcat_tail"]["stderr"]), 4_000)
            self.assertLessEqual(len(diagnostic["activity_last_anr"]["stderr"]), 4_000)

    def test_button_state_label_is_bound_to_test_id_not_globally_unique_text(self):
        snapshot = {"nodes": [
            {"id": 1, "testId": "rn-button", "role": "button", "text": "", "rect": [10, 10, 100, 50]},
            {"id": 2, "role": "text", "text": "React Native count: 1", "rect": [20, 20, 90, 40]},
            {"id": 3, "role": "text", "text": "React Native count: 1", "rect": [0, 60, 80, 80]},
        ]}
        assert_button_label(snapshot, "rn-button", "React Native count: 1")
        with self.assertRaisesRegex(IntegrationFailure, "expected state label"):
            assert_button_label(snapshot, "rn-button", "React Native count: 2")

    def test_framework_readiness_waits_for_valid_button_label_association(self):
        class FakeClient:
            def __init__(self):
                self.snapshots = [
                    {"nodes": [
                        {"testId": "compose-button", "enabled": True, "rect": [63, 192, 486, 318]},
                        {"text": "Compose count: 0", "rect": [126, 281, 423, 334]},
                    ]},
                    {"nodes": [
                        {"testId": "compose-button", "enabled": True, "rect": [63, 192, 486, 360]},
                        {"text": "Compose count: 0", "rect": [126, 240, 423, 293]},
                    ]},
                ]
                self.calls = 0

            def call(self, method):
                self.assert_snapshot_method(method)
                snapshot = self.snapshots[self.calls]
                self.calls += 1
                return snapshot

            @staticmethod
            def assert_snapshot_method(method):
                if method != "ui_snapshot":
                    raise AssertionError(f"unexpected client call: {method}")

        client = FakeClient()
        clock = ReadinessClock()
        with patch("scripts.integration.time.monotonic", clock.monotonic), \
             patch("scripts.integration.time.sleep", clock.sleep):
            ready = wait_for_framework_ready(client, "compose-button", "Compose count: 0")
        self.assertEqual(client.calls, 2)
        self.assertEqual(ready["nodes"][0]["testId"], "compose-button")

    def test_framework_readiness_times_out_if_button_label_never_matches(self):
        class FakeClient:
            def __init__(self):
                self.calls = 0

            def call(self, method):
                self.calls += 1
                return {"nodes": [
                    {"testId": "compose-button", "enabled": True, "rect": [63, 192, 486, 318]},
                    {"text": "Compose count: 0", "rect": [126, 281, 423, 334]},
                ]}

        client = FakeClient()
        clock = ReadinessClock()
        with patch("scripts.integration.time.monotonic", clock.monotonic), \
             patch("scripts.integration.time.sleep", clock.sleep), \
             self.assertRaisesRegex(IntegrationFailure, "did not become ready"):
            wait_for_framework_ready(client, "compose-button", "Compose count: 0",
                                     timeout_seconds=0.003, poll_interval_seconds=0.001)
        self.assertGreaterEqual(client.calls, 2)

    def test_framework_readiness_requires_enabled_button(self):
        class FakeClient:
            def call(self, method):
                return {"nodes": [
                    {"testId": "compose-button", "enabled": False, "rect": [63, 192, 486, 360]},
                    {"text": "Compose count: 0", "rect": [126, 240, 423, 293]},
                ]}

        clock = ReadinessClock()
        with patch("scripts.integration.time.monotonic", clock.monotonic), \
             patch("scripts.integration.time.sleep", clock.sleep), \
             self.assertRaisesRegex(IntegrationFailure, "did not become ready"):
            wait_for_framework_ready(FakeClient(), "compose-button", "Compose count: 0",
                                     timeout_seconds=0.003, poll_interval_seconds=0.001)

    def test_framework_readiness_propagates_rpc_failures(self):
        class FakeClient:
            calls = 0

            def call(self, method):
                self.calls += 1
                raise IntegrationFailure("guest closed the RPC connection")

        client = FakeClient()
        with self.assertRaisesRegex(IntegrationFailure, "guest closed the RPC connection"):
            wait_for_framework_ready(client, "compose-button", "Compose count: 0",
                                     timeout_seconds=1, poll_interval_seconds=0.001)
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
