import os
import unittest
from unittest.mock import patch

from scripts.harness import DeviceHarness, HarnessError


class WindowDiagnosticsTests(unittest.TestCase):
    def test_window_trace_is_only_enabled_by_explicit_ci_diagnostics(self):
        for setting in (None, "0", "1"):
            with self.subTest(setting=setting), patch.dict(os.environ, {}, clear=True):
                if setting is not None:
                    os.environ["ASHNAZG_CI_DIAGNOSTICS"] = setting
                with patch("scripts.harness.adb_path", return_value="adb"), \
                        patch("scripts.harness.select_device", return_value="explicit-owned-device"):
                    harness = DeviceHarness(readiness_timeout=1)
                with harness.instrument_log:
                    with patch.object(harness, "assert_available"), patch.object(harness, "adb_cmd"), \
                            patch("scripts.harness.subprocess.Popen") as spawn:
                        spawn.return_value.poll.return_value = 1
                        with self.assertRaisesRegex(HarnessError, "exited before readiness"):
                            harness.start(None, None, install=False)
                        args = spawn.call_args.args[0]
                self.assertEqual("traceWindowLookupMiss" in args, setting == "1")
                self.assertIn("explicit-owned-device", args)
                self.assertEqual(args[-1], "dev.ashnazg.agent/dev.ashnazg.agent.AgentInstrumentation")
