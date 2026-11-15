import math
import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.benchmark import (adb_metadata, assert_action_result, assert_fixture_snapshot,
                               host_binary_metadata, percentile, sanitize_message, summarize,
                               require_samples)


class BenchmarkStatisticsTests(unittest.TestCase):
    def test_percentile_uses_nearest_rank(self):
        self.assertEqual(percentile([9, 1, 3, 7, 5], 50), 5)
        self.assertEqual(percentile([9, 1, 3, 7, 5], 95), 9)

    def test_percentile_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            percentile([], 50)
        with self.assertRaises(ValueError):
            percentile([1], 101)
        with self.assertRaises(ValueError):
            percentile([1, math.nan], 50)

    def test_summary_reports_distribution_and_raw_samples(self):
        result = summarize([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(result["samples_ms"], [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(result["p50_ms"], 2.0)
        self.assertEqual(result["p95_ms"], 4.0)
        self.assertEqual(result["iterations"], 4)

    def test_empty_success_samples_are_failure_not_zero(self):
        with self.assertRaises(RuntimeError):
            require_samples([], errors=[])

    def test_errors_fail_even_if_some_iterations_succeeded(self):
        with self.assertRaisesRegex(RuntimeError, "2 errors"):
            require_samples([1.0], errors=["bad response", "timeout"])

    def test_failed_iterations_cannot_be_serialized_as_zero(self):
        with self.assertRaises(ValueError):
            summarize([1.0, 0.0])

    def test_measurement_snapshot_must_be_the_initial_native_fixture(self):
        snapshot = {"nodes": [{"testId": "dev.ashnazg.fixture:id/increment", "text": "Count: 0"}]}
        self.assertIs(assert_fixture_snapshot(snapshot, "test"), snapshot)
        with self.assertRaisesRegex(RuntimeError, "not the native fixture"):
            assert_fixture_snapshot({"nodes": [{"testId": "compose-button", "text": "Count: 0"}]}, "test")
        with self.assertRaisesRegex(RuntimeError, "Count: 0"):
            assert_fixture_snapshot({"nodes": [{"testId": "dev.ashnazg.fixture:id/increment", "text": "Count: 1"}]}, "test")

    def test_action_sample_requires_settled_fresh_expected_counter_snapshot(self):
        action_result = {"settled": True, "snapshot": {"snapshotId": 3, "nodes": [
            {"testId": "dev.ashnazg.fixture:id/increment", "text": "Count: 3"},
        ]}}
        self.assertIs(assert_action_result(action_result, 3, "test action"), action_result)
        action_result["settled"] = False
        with self.assertRaisesRegex(RuntimeError, "not settled"):
            assert_action_result(action_result, 3, "test action")
        action_result["settled"] = True
        with self.assertRaisesRegex(RuntimeError, "Count: 4"):
            assert_action_result(action_result, 4, "test action")
        with self.assertRaisesRegex(RuntimeError, "snapshot"):
            assert_action_result({"settled": True}, 3, "test action")
        with self.assertRaisesRegex(RuntimeError, "Count: 3"):
            assert_action_result({"settled": True, "snapshot": {"snapshotId": 3, "nodes": [
                {"testId": "dev.ashnazg.fixture:id/increment", "text": "Count: 4"},
                {"text": "Count: 3"},
            ]}}, 3, "test action")

    def test_host_metadata_records_release_digest_without_full_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "release" / "ashnazg"
            binary.parent.mkdir()
            binary.write_bytes(b"release-fixture")
            metadata = host_binary_metadata(binary)
            self.assertEqual(metadata["profile"], "release")
            self.assertEqual(metadata["basename"], "ashnazg")
            self.assertEqual(metadata["sha256"], hashlib.sha256(b"release-fixture").hexdigest())
            self.assertNotIn(str(binary), str(metadata))

    def test_adb_metadata_omits_installed_as_path_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            adb = Path(temporary) / "adb"
            with unittest.mock.patch("scripts.benchmark.subprocess.run") as run:
                run.return_value.stdout = ("Android Debug Bridge version 1.0.41\nVersion 36.0.0\n"
                                           f"Installed as {adb}\n")
                metadata = adb_metadata(str(adb))
            self.assertEqual(metadata["basename"], "adb")
            self.assertEqual(len(metadata["version_lines"]), 2)
            self.assertNotIn(str(adb), str(metadata))

    def test_error_text_redacts_tool_and_workspace_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adb = root / "platform-tools" / "adb"
            binary = root / "target" / "release" / "ashnazg"
            with unittest.mock.patch.dict("os.environ", {"ASHNAZG_ADB": str(adb), "ASHNAZG_BIN": str(binary)}):
                safe = sanitize_message(f"failed [{adb}, {binary}, {root}/file]", root)
            self.assertNotIn(str(root), safe)
            self.assertIn("adb", safe)
            self.assertIn("ashnazg", safe)


if __name__ == "__main__":
    unittest.main()
