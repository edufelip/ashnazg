import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import hwc_acceptance


class HwcAcceptanceTests(unittest.TestCase):
    def test_mode_requests_target_primary_display_instead_of_global_settings(self):
        with mock.patch.object(hwc_acceptance, "run_adb") as adb:
            hwc_acceptance._request_mode("serial", 0, deadline=100.0)
            hwc_acceptance._request_mode("serial", 1, deadline=100.0)
        self.assertEqual(adb.call_args_list, [
            mock.call("serial", ["shell", "cmd", "display", "set-user-preferred-display-mode",
                                 "720", "1280", rate, "0"], deadline=100.0)
            for rate in ("1.0", "60.0")
        ])

    def test_provenance_requires_allowed_module_paths_and_matching_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "hwcomposer.redroid.so"
            binary.write_bytes(b"module")
            descriptor = {
                "image_inspect": {
                    "id": "sha256:" + "a" * 64,
                    "config": {"labels": {
                        "org.ashnazg.hwc-sha256": hashlib.sha256(b"module").hexdigest(),
                        "org.ashnazg.hwc-paths": json.dumps(["/vendor/lib64/hw/hwcomposer.redroid.so"]),
                    }},
                },
            }
            provenance = root / "provenance.json"
            provenance.write_text(json.dumps(descriptor))
            result = hwc_acceptance.validate_provenance(provenance)
            self.assertEqual(result["paths"], ["/vendor/lib64/hw/hwcomposer.redroid.so"])
            self.assertEqual(result["sha256"], hashlib.sha256(b"module").hexdigest())

            descriptor["image_inspect"]["config"]["labels"]["org.ashnazg.hwc-paths"] = '["/tmp/other.so"]'
            provenance.write_text(json.dumps(descriptor))
            with self.assertRaisesRegex(hwc_acceptance.AcceptanceError, "unsupported HWC path"):
                hwc_acceptance.validate_provenance(provenance)

    def test_installed_module_bytes_must_match_provenance_hash(self):
        expected = hashlib.sha256(b"module").hexdigest()

        def pull(serial, arguments, check=True, deadline=None):
            Path(arguments[-1]).write_bytes(b"module")
            return "pulled"

        with mock.patch.object(hwc_acceptance, "run_adb", side_effect=pull) as adb:
            result = hwc_acceptance.verify_installed_module("serial", {
                "paths": ["/vendor/lib64/hw/hwcomposer.redroid.so"], "sha256": expected,
            })
        self.assertEqual(result["sha256"], expected)
        self.assertEqual(adb.call_args.args[1][0:2], ["pull", "/vendor/lib64/hw/hwcomposer.redroid.so"])

        with mock.patch.object(hwc_acceptance, "run_adb", side_effect=lambda *args, **kwargs: "not pullable"):
            with self.assertRaisesRegex(hwc_acceptance.AcceptanceError, "no installed HWC module matches"):
                hwc_acceptance.verify_installed_module("serial", {
                    "paths": ["/vendor/lib64/hw/hwcomposer.redroid.so"], "sha256": expected,
                })

    def test_serial_can_be_explicit_flag_or_explicit_environment(self):
        with mock.patch.dict(os.environ, {"ANDROID_SERIAL": "device-from-env"}):
            self.assertEqual(hwc_acceptance.require_serial(None), "device-from-env")
            self.assertEqual(hwc_acceptance.require_serial("device-explicit"), "device-explicit")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(hwc_acceptance.AcceptanceError, "ANDROID_SERIAL"):
                hwc_acceptance.require_serial(None)

    def test_telemetry_intervals_only_use_actual_callback_samples(self):
        dump = ('ASHNAZG_HWC_TELEMETRY_V1 {"schema":1,"selected_config":1,"callback_enabled":true,'
                '"callback_total":2,"present_successes":0,"present_timeouts":0,'
                '"callbacks":[{"timestamp_ns":100,"config_id":1,'
                '"callback_enabled":true,"sequence":1},'
                '{"timestamp_ns":16667100,"config_id":1,"callback_enabled":true,"sequence":2}]}')
        parsed = hwc_acceptance.parse_hwc_dump(dump)
        intervals = hwc_acceptance.callback_intervals(parsed)
        self.assertEqual(intervals["status"], "available")
        self.assertEqual(intervals["intervals_ns"], [16667000])
        self.assertEqual(intervals["config_ids"], [1])
        self.assertEqual(hwc_acceptance.callback_intervals({"callbacks": [], "callback_enabled": False})["status"],
                         "unavailable_callbacks_disabled")
        self.assertEqual(hwc_acceptance.callback_intervals({"callbacks": [], "callback_enabled": True})["status"],
                         "unavailable_too_few_samples")
        crossing = {"callback_enabled": True, "callbacks": [
            {"timestamp_ns": 10, "config_id": 1, "sequence": 1, "callback_enabled": True},
            {"timestamp_ns": 20, "config_id": 0, "sequence": 2, "callback_enabled": True},
        ]}
        self.assertEqual(hwc_acceptance.callback_intervals(crossing)["status"],
                         "unavailable_no_same_config_intervals")
        self.assertEqual(hwc_acceptance.callback_intervals(crossing, config_id=1)["status"],
                         "unavailable_no_same_config_intervals")
        self.assertEqual(hwc_acceptance.callback_intervals(parsed, config_id=1, after_sequence=2)["status"],
                         "unavailable_too_few_samples")

    def test_raw_evidence_is_bounded(self):
        self.assertEqual(len(hwc_acceptance.bounded_text("x" * 100, 20)), 20)
        self.assertTrue(hwc_acceptance.bounded_text("x" * 100, 20).endswith("[truncated]"))

    def test_bounded_surfaceflinger_keeps_telemetry_marker_when_truncated(self):
        raw = "x" * 100 + "\n" + hwc_acceptance.MARKER + '{"schema":1}\n' + "y" * 100
        bounded = hwc_acceptance.bounded_surfaceflinger(raw, 80)
        self.assertLessEqual(len(bounded), 80)
        self.assertIn(hwc_acceptance.MARKER, bounded)

    def test_wait_for_config_uses_one_absolute_deadline(self):
        deadline = 100.0
        now = iter([99.0, 99.5, 100.1])
        with mock.patch.object(hwc_acceptance.time, "monotonic", side_effect=lambda: next(now)), \
                mock.patch.object(hwc_acceptance.time, "sleep") as sleep, \
                mock.patch.object(hwc_acceptance, "read_hwc_telemetry", side_effect=[{"selected_config": 0},
                                                                                       {"selected_config": 0}]):
            with self.assertRaisesRegex(hwc_acceptance.AcceptanceError, "deadline"):
                hwc_acceptance.wait_for_config("serial", 1, deadline=deadline, poll_interval=0.5)
        sleep.assert_called_once_with(0.5)

    def test_restore_60_hz_is_attempted_after_transition_failure(self):
        with mock.patch.object(hwc_acceptance, "verify_installed_module", return_value={"path": "module"}), \
                mock.patch.object(hwc_acceptance, "read_hwc_telemetry",
                                  return_value={"selected_config": 1, "callback_total": 0}), \
                mock.patch.object(hwc_acceptance, "display_evidence", return_value={}), \
                mock.patch.object(hwc_acceptance, "_request_mode") as request, \
                mock.patch.object(hwc_acceptance, "wait_for_config", side_effect=[
                    hwc_acceptance.AcceptanceError("transition failed"), {"selected_config": 1}]), \
                mock.patch.object(hwc_acceptance, "validate_provenance", return_value={"image_id": "image", "paths": [], "sha256": "hash"}):
            with self.assertRaisesRegex(hwc_acceptance.AcceptanceError, "transition failed"):
                hwc_acceptance.run_acceptance("serial", Path("ignored"), timeout=1)
        self.assertEqual([call.args[:2] for call in request.call_args_list],
                         [("serial", 0), ("serial", 1)])

    def test_cli_writes_machine_readable_failure_when_serial_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "receipt.json"
            with mock.patch.dict(os.environ, {}, clear=True):
                code = hwc_acceptance.main(["--provenance", str(Path(temporary) / "missing.json"),
                                            "--output", str(output)])
            self.assertEqual(code, 1)
            receipt = json.loads(output.read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertIn("ANDROID_SERIAL", receipt["error"])


if __name__ == "__main__":
    unittest.main()
