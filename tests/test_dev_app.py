import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts import dev_app


class DevAppTests(unittest.TestCase):
    def test_install_requires_explicit_online_serial_and_uses_update_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "app.apk"
            apk.touch()
            with patch.object(dev_app, "require_online_serial"), patch.object(dev_app, "run_adb") as run:
                dev_app.install_apk("adb", "phone-1", str(apk))
            self.assertEqual(run.call_args.args[2], ["install", "-r", str(apk)])

    def test_reverse_setup_never_removes_preexisting_mapping(self):
        with patch.object(dev_app, "require_online_serial"), patch.object(
                dev_app.subprocess, "run", return_value=type("Result", (), {"stdout": ""})()), patch.object(
                dev_app, "run_adb", side_effect=[RuntimeError("server failed")]) as run:
            with self.assertRaisesRegex(RuntimeError, "server failed"):
                dev_app.setup_reverses("adb", "phone-1", [("tcp:8081", "tcp:8081"), ("tcp:8082", "tcp:8082")])
        self.assertNotIn("--remove", str(run.call_args_list))

    def test_reverse_refuses_conflicting_foreign_mapping(self):
        result = type("Result", (), {"stdout": "phone-1 tcp:8081 tcp:9000\n"})()
        with patch.object(dev_app, "require_online_serial"), patch.object(
                dev_app.subprocess, "run", return_value=result), patch.object(dev_app, "run_adb") as run:
            with self.assertRaisesRegex(dev_app.DevAppError, "already"):
                dev_app.setup_reverses("adb", "phone-1", [("tcp:8081", "tcp:8081")])
            run.assert_not_called()

    def test_reverse_creation_uses_no_rebind_and_rolls_back_only_created(self):
        result = type("Result", (), {"stdout": "phone-1 tcp:8000 tcp:8000\n"})()
        with patch.object(dev_app, "require_online_serial"), patch.object(
                dev_app.subprocess, "run", return_value=result) as command, patch.object(
                dev_app, "run_adb", side_effect=["", RuntimeError("failed")]) as run:
            with self.assertRaisesRegex(RuntimeError, "failed"):
                dev_app.setup_reverses("adb", "phone-1", [
                    ("tcp:8000", "tcp:8000"), ("tcp:8081", "tcp:8081"), ("tcp:8082", "tcp:8082")])
            self.assertEqual(run.call_args_list[0].args[2], ["reverse", "--no-rebind", "tcp:8081", "tcp:8081"])
            removes = [call.args[0] for call in command.call_args_list if "--remove" in call.args[0]]
            self.assertEqual(removes, [["adb", "-s", "phone-1", "reverse", "--remove", "tcp:8081"]])

    def test_invalid_server_port_fails_before_device_mutation(self):
        with patch.object(dev_app, "install_apk") as install:
            with self.assertRaises(SystemExit):
                dev_app.main(["--apk", "/missing.apk", "--serial", "phone-1", "--server-port", "0"])
            install.assert_not_called()

    def test_checklist_marks_app_identity_as_unselected(self):
        checklist = dev_app.acceptance_checklist("/tmp/app.apk", "phone-1")
        self.assertIn("app_selection: pending", checklist)
        self.assertIn("preserve_app_data: yes", checklist)
        self.assertIn("real_app_proof: not_claimed", checklist)


if __name__ == "__main__":
    unittest.main()
