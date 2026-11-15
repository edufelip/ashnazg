import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.harness import DeviceHarness, HarnessError


class WindowDiagnosticsTests(unittest.TestCase):
    def test_window_trace_is_only_enabled_by_explicit_ci_diagnostics(self):
        for setting in (None, "0", "1"):
            with self.subTest(setting=setting), patch.dict(os.environ, {}, clear=True):
                if setting is not None:
                    os.environ["ASHNAZG_CI_DIAGNOSTICS"] = setting
                harness = DeviceHarness.__new__(DeviceHarness)
                harness.adb = "adb"
                harness.serial = "explicit-owned-device"
                harness.token = "test-token"
                harness.readiness_timeout = 1
                with tempfile.TemporaryFile(mode="w+b") as log:
                    harness.instrument_log = log
                    with patch.object(harness, "assert_available"), patch.object(harness, "adb_cmd"), \
                            patch("scripts.harness.subprocess.Popen") as spawn:
                        spawn.return_value.poll.return_value = 1
                        with self.assertRaisesRegex(HarnessError, "exited before readiness"):
                            harness.start(None, None, install=False)
                        args = spawn.call_args.args[0]
                self.assertEqual("traceWindowLookupMiss" in args, setting == "1")
                self.assertIn("explicit-owned-device", args)
                self.assertEqual(args[-1], "dev.ashnazg.agent/dev.ashnazg.agent.AgentInstrumentation")
