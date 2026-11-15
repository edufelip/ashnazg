import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.ci_emulator import (
    animation_settings_commands,
    EmulatorSupervisor,
    emulator_command,
    focused_application_window,
    keyguard_dismiss_command,
    last_anr_status,
    readiness_status,
    require_ci_environment,
    check_port_available,
    terminate_process_group,
    window_records,
)


class CiEmulatorReadinessTests(unittest.TestCase):
    def test_focused_application_window_is_read_from_display_zero(self):
        dump = """Input Dispatcher State:
  FocusedWindows:
    displayId=0, name='a1b2 com.android.launcher3/.Launcher'
    displayId=2, name='c3d4 com.example.secondary/.MainActivity'
"""
        self.assertEqual(
            focused_application_window(dump),
            "a1b2 com.android.launcher3/.Launcher",
        )

    def test_no_focus_missing_focus_and_system_window_are_not_ready(self):
        self.assertIsNone(focused_application_window("FocusedWindows: <none>\n"))
        self.assertIsNone(focused_application_window("FocusedWindows:\n    displayId=2, name='c3d4 com.example/.Other'\n"))
        input_dump = "FocusedWindows:\n  displayId=0, name='f00d com.android.systemui/.Dialog'\n"
        windows = """Window #0 Window{f00d u0 com.android.systemui/.Dialog}:
  mDisplayId=0
  mAttrs={(0,0)(fillxfill) ty=STATUS_BAR fmt=TRANSLUCENT}
  mHasSurface=true
  mViewVisibility=0x0
  WindowStateAnimator: mDrawState=HAS_DRAWN
"""
        self.assertEqual(readiness_status(input_dump, windows,
                                          "ACTIVITY MANAGER LAST ANR\n<no ANR has occurred since boot>"),
                         (False, "focused window is not an application window"))

    def test_only_visible_drawn_application_window_with_focus_is_ready(self):
        focused = "a1b2 com.android.launcher3/.Launcher"
        window = "Window{a1b2 u0 com.android.launcher3/.Launcher}"
        input_dump = f"Input Dispatcher State:\n  FocusedWindows:\n    displayId=0, name='{focused}'\n"
        windows = f"""Window #0 {window}:
  mDisplayId=0
  mAttrs={{(0,0)(fillxfill) ty=BASE_APPLICATION fmt=OPAQUE}}
  mViewVisibility=0x0
  mHasSurface=true isReadyForDisplay()=true
  WindowStateAnimator: mDrawState=HAS_DRAWN
"""
        clean_anr = "ACTIVITY MANAGER LAST ANR\n<no ANR has occurred since boot>"
        self.assertEqual(readiness_status(input_dump, windows, clean_anr),
                         (True, "focused application window is drawn"))
        records = window_records(windows)
        self.assertEqual(records["a1b2"]["type"], "BASE_APPLICATION")

    def test_undrawn_invisible_mismatched_or_missing_window_is_not_ready(self):
        focused = "a1b2 com.android.launcher3/.Launcher"
        window = "Window{a1b2 u0 com.android.launcher3/.Launcher}"
        input_dump = f"FocusedWindows:\n  displayId=0, name='{focused}'\n"
        clean_anr = "ACTIVITY MANAGER LAST ANR\n<no ANR has occurred since boot>"
        for flags in (
            "mViewVisibility=0x0\n  mHasSurface=true\n  mDrawState=DRAW_PENDING",
            "mViewVisibility=0x8\n  mHasSurface=true\n  mDrawState=HAS_DRAWN",
            "mViewVisibility=0x0\n  mHasSurface=false\n  mDrawState=HAS_DRAWN",
        ):
            windows = f"Window #0 {window}:\n  mDisplayId=0\n  mAttrs={{ty=APPLICATION}}\n  {flags}\n"
            self.assertFalse(readiness_status(input_dump, windows, clean_anr)[0])
        self.assertFalse(readiness_status(input_dump, "", clean_anr)[0])

    def test_anr_is_fail_closed_even_when_window_looks_ready(self):
        focused = "a1b2 com.android.launcher3/.Launcher"
        window = "Window{a1b2 u0 com.android.launcher3/.Launcher}"
        input_dump = f"FocusedWindows:\n  displayId=0, name='{focused}'\n"
        windows = f"Window #0 {window}:\n  mDisplayId=0\n  mAttrs={{ty=APPLICATION}}\n  mViewVisibility=0x0\n  mHasSurface=true\n  mDrawState=HAS_DRAWN\n"
        clean_anr = "ACTIVITY MANAGER LAST ANR\n<no ANR has occurred since boot>"
        self.assertEqual(last_anr_status(clean_anr), False)
        self.assertEqual(last_anr_status("ANR time: Nov 15\nReason: Input dispatching timed out"), True)
        self.assertIsNone(last_anr_status("ACTIVITY MANAGER LAST ANR"))
        self.assertIsNone(last_anr_status("unexpected dumpsys output"))
        self.assertEqual(readiness_status(input_dump, windows, "ANR time: Nov 15"),
                         (False, "ANR recorded since boot"))
        self.assertFalse(readiness_status(input_dump, windows, "unexpected output")[0])
        self.assertTrue(last_anr_status("ANR time: Nov 15\n<no ANR has occurred since boot>"))

    def test_animation_disable_settings_are_exact_and_non_input(self):
        commands = animation_settings_commands("adb", "emulator-5554")
        self.assertEqual([command[-2] for command in commands], [
            "window_animation_scale", "transition_animation_scale", "animator_duration_scale",
        ])
        self.assertTrue(all(command[-1] == "0" and "input" not in command for command in commands))

    def test_emulator_command_has_no_input_injection_and_preserves_profile(self):
        command = emulator_command("/sdk/emulator/emulator", "ashnazg_ci_test", 5554)
        self.assertEqual(command[:4], ["/sdk/emulator/emulator", "-port", "5554", "-avd"])
        self.assertIn("ashnazg_ci_test", command)
        self.assertEqual(command[command.index("-cores") + 1], "2")
        self.assertEqual(command[command.index("-memory") + 1], "2560")
        self.assertIn("-no-window", command)
        self.assertIn("-gpu", command)
        self.assertIn("swiftshader_indirect", command)
        self.assertNotIn("input", " ".join(command).casefold())

    def test_windowmanager_keyguard_dismissal_is_not_an_input_event(self):
        command = keyguard_dismiss_command("/sdk/platform-tools/adb", "emulator-5554")
        self.assertEqual(command, ["/sdk/platform-tools/adb", "-s", "emulator-5554",
                                   "shell", "wm", "dismiss-keyguard"])
        self.assertNotIn("input", command)

    def test_supervisor_refuses_non_github_environment(self):
        with self.assertRaisesRegex(Exception, "restricted to GitHub Actions"):
            require_ci_environment({"GITHUB_ACTIONS": "false"})
        require_ci_environment({"GITHUB_ACTIONS": "true"})

    def test_preexisting_adb_serial_is_refused_before_emulator_launch(self):
        result = subprocess.CompletedProcess([], 0,
                                             "List of devices attached\nemulator-5554 device product:sdk\n", "")
        with patch("scripts.ci_emulator.subprocess.run", return_value=result) as run:
            with self.assertRaisesRegex(Exception, "refusing to use existing adb emulator serial"):
                check_port_available("adb", 5554)
        run.assert_called_once()

    def test_window_records_are_isolated_and_ignore_nonapplication_types(self):
        dump = """Window #0 Window{app u0 org.example/.Main}:
  mDisplayId=0
  mAttrs={(0,0) ty=APPLICATION}
  mViewVisibility=0x0
  mHasSurface=true
  mDrawState=HAS_DRAWN
Window #1 Window{bar u0 com.android.systemui/.StatusBar}:
  mDisplayId=0
  mAttrs={(0,0) ty=STATUS_BAR}
  mViewVisibility=0x0
  mHasSurface=true
  mDrawState=HAS_DRAWN
"""
        records = window_records(dump)
        app_window = "Window{app u0 org.example/.Main}"
        bar_window = "Window{bar u0 com.android.systemui/.StatusBar}"
        self.assertEqual(set(records), {app_window, bar_window})
        self.assertEqual(records[app_window]["type"], "APPLICATION")
        self.assertEqual(records[bar_window]["type"], "STATUS_BAR")


class ProcessGroupCleanupTests(unittest.TestCase):
    def test_cleanup_terminates_only_the_owned_process_group(self):
        process = Mock()
        process.pid = 1234
        process.wait.return_value = 0
        with patch("scripts.ci_emulator.os.killpg") as killpg, \
             patch("scripts.ci_emulator.process_group_exists", return_value=False):
            terminate_process_group(process, grace_seconds=0)
        killpg.assert_called_once_with(1234, signal.SIGTERM)
        self.assertEqual(process.wait.call_args_list, [
            unittest.mock.call(timeout=0.0),
            unittest.mock.call(timeout=5),
        ])

    def test_cleanup_escalates_only_if_owned_group_survives(self):
        process = Mock()
        process.pid = 9876
        process.wait.side_effect = [subprocess.TimeoutExpired("emulator", 1), 0]
        with patch("scripts.ci_emulator.os.killpg") as killpg, \
             patch("scripts.ci_emulator.process_group_exists", side_effect=[True, True, False]):
            terminate_process_group(process, grace_seconds=0)
        self.assertEqual(killpg.call_args_list, [
            unittest.mock.call(9876, signal.SIGTERM),
            unittest.mock.call(9876, signal.SIGKILL),
        ])


class SupervisorStartupTests(unittest.TestCase):
    def test_start_disables_animations_then_dismisses_keyguard_without_input(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory)
            for executable in ("platform-tools/adb", "emulator/emulator", "cmdline-tools/latest/bin/avdmanager"):
                path = sdk / executable
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            supervisor = EmulatorSupervisor(sdk=sdk, output_dir=sdk / "artifacts")
            order = Mock()
            with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}), \
                 patch("scripts.ci_emulator.check_port_available") as check_port, \
                 patch.object(supervisor, "_create_avd", return_value={"ANDROID_AVD_HOME": directory}) as create_avd, \
                 patch.object(supervisor, "_start_emulator") as launch, \
                 patch.object(supervisor, "_wait_for_boot") as wait_boot, \
                 patch.object(supervisor, "_readiness_snapshot", return_value=(
                     "ACTIVITY MANAGER LAST ANR\n<no ANR has occurred since boot>", "", "")) as snapshot, \
                 patch.object(supervisor, "_wait_for_application_window", side_effect=lambda: setattr(supervisor, "ready", True)) as wait_ready, \
                 patch("scripts.ci_emulator.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                order.attach_mock(check_port, "preflight")
                order.attach_mock(create_avd, "avd")
                order.attach_mock(launch, "launch")
                order.attach_mock(wait_boot, "boot")
                order.attach_mock(snapshot, "health")
                order.attach_mock(run, "run")
                order.attach_mock(wait_ready, "ready")
                supervisor.start()
            names = [call[0] for call in order.method_calls]
            self.assertEqual(names[:5], ["preflight", "avd", "launch", "boot", "run"])
            self.assertEqual(names[-2:], ["run", "ready"])
            run_commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(run_commands[:3], animation_settings_commands(supervisor.adb, supervisor.serial))
            self.assertEqual(run_commands[3], [supervisor.adb, "-s", supervisor.serial,
                                                "shell", "wm", "dismiss-keyguard"])
            self.assertTrue(all("input" not in command for command in run_commands))
            snapshot.assert_called_once()

    def test_each_check_has_a_bounded_timeout_and_child_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = EmulatorSupervisor(sdk=Path(directory), output_dir=Path(directory))
            supervisor.serial = "emulator-5560"
            with patch.dict("os.environ", {}, clear=True), patch("scripts.ci_emulator.subprocess.run") as run:
                supervisor.run_checks()
            self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [300, 1200, 1200])
            self.assertTrue(all(call.kwargs["env"]["ANDROID_SERIAL"] == "emulator-5560"
                                for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
