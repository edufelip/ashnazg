#!/usr/bin/env python3
"""Launch and supervise the disposable Android emulator used by public CI."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


class EmulatorFailure(RuntimeError):
    pass


def emulator_command(emulator: str, avd_name: str, port: int) -> list[str]:
    return [
        emulator, "-port", str(port), "-avd", avd_name, "-cores", "2", "-memory", "2560",
        "-no-window", "-gpu", "swiftshader_indirect", "-no-snapshot", "-noaudio", "-no-boot-anim",
    ]


def keyguard_dismiss_command(adb: str, serial: str) -> list[str]:
    return [adb, "-s", serial, "shell", "wm", "dismiss-keyguard"]


def animation_settings_commands(adb: str, serial: str) -> list[list[str]]:
    return [[adb, "-s", serial, "shell", "settings", "put", "global", key, "0"]
            for key in ("window_animation_scale", "transition_animation_scale",
                        "animator_duration_scale")]


def focused_application_window(input_dump: str) -> str | None:
    """Return the display 0 focused-window label from InputDispatcher's structured dump."""
    in_focused_windows = False
    for line in input_dump.splitlines():
        stripped = line.strip()
        if stripped.startswith("FocusedWindows:"):
            in_focused_windows = True
            continue
        if in_focused_windows and stripped and not stripped.startswith("displayId="):
            break
        if in_focused_windows:
            match = re.fullmatch(r"displayId=(\d+),\s*name='([^']+)'", stripped)
            if match and match.group(1) == "0" and match.group(2) != "<none>":
                return match.group(2)
    return None


def last_anr_status(last_anr_dump: str) -> bool | None:
    """Return False only for the explicit clean-boot marker, True for an ANR, else unknown."""
    if re.search(r"\bANR time\s*:", last_anr_dump):
        return True
    if (re.search(r"ACTIVITY MANAGER LAST ANR", last_anr_dump, re.IGNORECASE)
            and "<no ANR has occurred since boot>" in last_anr_dump):
        return False
    return None


def window_token(label: str) -> str | None:
    """Correlate InputDispatcher's hex window token with WindowState's Window{ token."""
    match = re.match(r"\s*([0-9a-fA-F]+)\s+", label)
    return match.group(1).casefold() if match else None


def window_records(window_dump: str) -> dict[str, dict[str, str | bool | None]]:
    """Parse Android 15 `dumpsys window -a windows` output, failing closed on missing fields."""
    records: dict[str, dict[str, str | bool | None]] = {}
    current_name: str | None = None
    lines: list[str] = []
    header = re.compile(r"^\s*Window #\d+ (Window\{[^}]+\}):\s*$")

    def finish() -> None:
        if current_name is None:
            return
        block = "\n".join(lines)
        attrs = next((line for line in lines if "mAttrs=" in line), "")
        display = re.search(r"\bmDisplayId=(\d+)\b", block)
        win_type = re.search(r"\bty=([A-Z][A-Z0-9_]*)\b", attrs)
        visibility = re.search(r"\bmViewVisibility=0x([0-9a-fA-F]+)\b", block)
        surface = re.search(r"\bmHasSurface=(true|false)\b", block)
        ready_for_display = re.search(r"\bisReadyForDisplay\(\)=(true|false)\b", block)
        draw_state = re.search(r"\bmDrawState=([A-Z_]+)\b", block)
        token_match = re.match(r"Window\{([0-9a-fA-F]+)\s+", current_name)
        token = token_match.group(1).casefold() if token_match else current_name
        records[token] = {
            "display_id": display.group(1) if display else None,
            "type": win_type.group(1) if win_type else None,
            "view_visibility": visibility.group(1).lower() if visibility else None,
            "has_surface": surface.group(1) == "true" if surface else None,
            "ready_for_display": ready_for_display.group(1) == "true" if ready_for_display else None,
            "draw_state": draw_state.group(1) if draw_state else None,
        }

    for line in window_dump.splitlines():
        match = header.match(line)
        if match:
            finish()
            current_name = match.group(1)
            lines = []
        elif current_name is not None:
            lines.append(line)
    finish()
    return records


def readiness_status(input_dump: str, window_dump: str, last_anr_dump: str) -> tuple[bool, str]:
    anr = last_anr_status(last_anr_dump)
    if anr is True:
        return False, "ANR recorded since boot"
    if anr is None:
        return False, "last-ANR state is unavailable"
    focused = focused_application_window(input_dump)
    if focused is None:
        return False, "display 0 has no focused window"
    token = window_token(focused)
    if token is None:
        return False, "focused window token has an unknown format"
    record = window_records(window_dump).get(token)
    if record is None:
        return False, "focused window has no matching WindowManager record"
    if record["display_id"] != "0":
        return False, "focused window is not on display 0"
    if record["type"] not in {"APPLICATION", "BASE_APPLICATION"}:
        return False, "focused window is not an application window"
    if record["view_visibility"] != "0":
        return False, "focused application window is not visible"
    if record["has_surface"] is not True or record["ready_for_display"] is not True:
        return False, "focused application window has no ready surface"
    if record["draw_state"] != "HAS_DRAWN":
        return False, "focused application window has not drawn"
    return True, "focused application window is drawn"


def process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def terminate_process_group(process: subprocess.Popen, *, grace_seconds: float = 10.0) -> None:
    """Terminate only the new session/process group owned by this supervisor."""
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace_seconds
    try:
        process.wait(timeout=max(0.0, grace_seconds))
    except subprocess.TimeoutExpired:
        pass
    while process_group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def require_ci_environment(env: dict[str, str] | os._Environ) -> None:
    if env.get("GITHUB_ACTIONS", "").casefold() != "true":
        raise EmulatorFailure("the owned emulator supervisor is restricted to GitHub Actions CI")


def check_port_available(adb: str, port: int) -> None:
    serial = f"emulator-{port}"
    result = subprocess.run([adb, "devices", "-l"], check=True, text=True, capture_output=True, timeout=15)
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if fields and fields[0] == serial:
            raise EmulatorFailure(f"refusing to use existing adb emulator serial {serial}")
    for candidate in (port, port + 1):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError as exc:
                raise EmulatorFailure(f"refusing occupied emulator port {candidate}: {exc}") from exc


def avd_config_path(avd_home: Path, name: str) -> Path:
    return avd_home / f"{name}.avd" / "config.ini"


def set_avd_resources(config_path: Path) -> None:
    existing = config_path.read_text(encoding="utf-8").splitlines()
    values = {"hw.cpu.ncore": "2", "hw.ramSize": "2560"}
    output: list[str] = []
    seen: set[str] = set()
    for line in existing:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in values:
            if key not in seen:
                output.append(f"{key}={values[key]}")
                seen.add(key)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={value}")
    config_path.write_text("\n".join(output) + "\n", encoding="utf-8")


class EmulatorSupervisor:
    def __init__(self, *, sdk: Path, output_dir: Path, port: int = 5554, boot_timeout: float = 600,
                 ready_timeout: float = 600, poll_interval: float = 0.5):
        self.sdk = sdk
        self.output_dir = output_dir
        self.port = port
        self.boot_timeout = boot_timeout
        self.ready_timeout = ready_timeout
        self.poll_interval = poll_interval
        self.serial = f"emulator-{port}"
        self.adb = str(sdk / "platform-tools" / "adb")
        self.emulator = str(sdk / "emulator" / "emulator")
        self.avdmanager = shutil.which("avdmanager") or str(
            sdk / "cmdline-tools" / "latest" / "bin" / "avdmanager")
        self.avd_home: Path | None = None
        self.avd_name: str | None = None
        self.process: subprocess.Popen | None = None
        self.log_stream = None
        self.ready = False
        self.tests_started = False

    def _run(self, *args: str, check: bool = True, timeout: float = 20) -> subprocess.CompletedProcess:
        return subprocess.run([self.adb, "-s", self.serial, *args], check=check,
                              text=True, capture_output=True, timeout=timeout)

    def _create_avd(self) -> dict[str, str]:
        run_id = os.environ.get("GITHUB_RUN_ID", "local")
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
        unique = uuid.uuid4().hex[:8]
        self.avd_name = f"ashnazg_ci_{run_id}_{attempt}_{os.getpid()}_{unique}"
        runner_temp = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
        self.avd_home = Path(tempfile.mkdtemp(prefix="ashnazg-avd-", dir=runner_temp))
        env = os.environ.copy()
        env["ANDROID_AVD_HOME"] = str(self.avd_home)
        command = [self.avdmanager, "create", "avd", "-n", self.avd_name,
                   "-k", "system-images;android-35;default;x86_64", "--device", "pixel_6"]
        result = subprocess.run(command, input="no\n", check=True, text=True, capture_output=True,
                                timeout=120, env=env)
        config = avd_config_path(self.avd_home, self.avd_name)
        if not config.is_file():
            raise EmulatorFailure(f"avdmanager did not create expected config: {config.name}")
        set_avd_resources(config)
        return env

    def _start_emulator(self, env: dict[str, str]) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_stream = (self.output_dir / "emulator.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            emulator_command(self.emulator, self.avd_name or "", self.port),
            stdin=subprocess.DEVNULL, stdout=self.log_stream, stderr=subprocess.STDOUT,
            env=env, start_new_session=True,
        )

    def _assert_running(self) -> None:
        if self.process is not None and self.process.poll() is not None:
            raise EmulatorFailure(f"owned emulator exited with status {self.process.returncode}")

    def _wait_for_boot(self) -> None:
        deadline = time.monotonic() + self.boot_timeout
        last_state = "adb device not available"
        while time.monotonic() < deadline:
            self._assert_running()
            state = self._run("get-state", check=False, timeout=5)
            if state.returncode == 0 and state.stdout.strip() == "device":
                anr = self._run("shell", "dumpsys", "activity", "lastanr", check=False, timeout=10)
                if anr.returncode == 0 and last_anr_status(anr.stdout) is True:
                    raise EmulatorFailure("emulator recorded an ANR while booting")
                boot = self._run("shell", "getprop", "sys.boot_completed", check=False, timeout=5)
                if boot.returncode == 0 and boot.stdout.strip() == "1":
                    return
                last_state = f"sys.boot_completed={boot.stdout.strip()!r}"
            else:
                last_state = state.stderr.strip() or state.stdout.strip() or "adb device not available"
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        raise EmulatorFailure(f"emulator did not report boot complete within {self.boot_timeout:g}s: {last_state}")

    def _readiness_snapshot(self) -> tuple[str, str, str]:
        anr = self._run("shell", "dumpsys", "activity", "lastanr", check=False, timeout=15)
        input_state = self._run("shell", "dumpsys", "input", check=False, timeout=15)
        windows = self._run("shell", "dumpsys", "window", "-a", "windows", check=False, timeout=15)
        return (anr.stdout if anr.returncode == 0 else "",
                input_state.stdout if input_state.returncode == 0 else "",
                windows.stdout if windows.returncode == 0 else "")

    def _wait_for_application_window(self) -> None:
        deadline = time.monotonic() + self.ready_timeout
        last_reason = "readiness checks unavailable"
        prior_ready_window: str | None = None
        while time.monotonic() < deadline:
            self._assert_running()
            anr, input_state, windows = self._readiness_snapshot()
            status, reason = readiness_status(input_state, windows, anr)
            if last_anr_status(anr) is True:
                raise EmulatorFailure("emulator recorded an ANR before tests: " + reason)
            if status:
                focused = focused_application_window(input_state)
                if focused == prior_ready_window:
                    self.ready = True
                    return
                prior_ready_window = focused
                last_reason = "focused application window has not yet remained ready across consecutive checks"
            else:
                prior_ready_window = None
                last_reason = reason
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        raise EmulatorFailure(f"no focused, drawn application window within {self.ready_timeout:g}s: {last_reason}")

    def start(self) -> None:
        require_ci_environment(os.environ)
        if self.port < 5554 or self.port % 2 != 0:
            raise EmulatorFailure("emulator port must be an even port >= 5554")
        if not all(Path(path).is_file() for path in (self.adb, self.emulator, self.avdmanager)):
            raise EmulatorFailure("Android SDK adb, emulator, or avdmanager executable is missing")
        check_port_available(self.adb, self.port)
        env = self._create_avd()
        self._start_emulator(env)
        self._wait_for_boot()
        for command in animation_settings_commands(self.adb, self.serial):
            result = subprocess.run(command, check=False, text=True, capture_output=True, timeout=15)
            if result.returncode != 0:
                raise EmulatorFailure("failed to disable emulator animations: " + result.stderr[-500:])
        anr, _, _ = self._readiness_snapshot()
        if last_anr_status(anr) is True:
            raise EmulatorFailure("emulator recorded an ANR before keyguard dismissal")
        if last_anr_status(anr) is None:
            # Service startup can lag sys.boot_completed. The regular readiness poll below
            # keeps checking; keyguard dismissal does not inject input.
            pass
        dismiss = subprocess.run(keyguard_dismiss_command(self.adb, self.serial), check=False,
                                 text=True, capture_output=True, timeout=15)
        if dismiss.returncode != 0:
            raise EmulatorFailure("WindowManager keyguard dismissal failed: " + dismiss.stderr[-500:])
        self._wait_for_application_window()

    def collect_boot_diagnostics(self, reason: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        commands = {
            "activity_last_anr": ("shell", "dumpsys", "activity", "lastanr"),
            "input": ("shell", "dumpsys", "input"),
            "windows": ("shell", "dumpsys", "window", "-a", "windows"),
            "logcat": ("logcat", "-d", "-t", "2000", "-v", "brief"),
        }
        output = {"stage": "pre_test_boot_readiness", "reason": reason, "diagnostics": {}}
        for name, args in commands.items():
            try:
                result = self._run(*args, check=False, timeout=20)
                output["diagnostics"][name] = {
                    "exit_code": result.returncode,
                    "stdout": result.stdout[-64_000:],
                    "stderr": result.stderr[-4_000:],
                }
            except (OSError, subprocess.SubprocessError) as exc:
                output["diagnostics"][name] = {"error": f"{type(exc).__name__}: {exc}"[-4_000:]}
        path = self.output_dir / "boot-diagnostics.json"
        path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
        return path

    def run_checks(self) -> None:
        checks = [
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            [sys.executable, "scripts/integration.py", "--framework-fixtures", "--output",
             "artifacts/integration.json", "--artifacts", "artifacts/integration"],
            [sys.executable, "scripts/benchmark.py", "--iterations", "10", "--output",
             "artifacts/benchmark.json"],
        ]
        env = os.environ.copy()
        env["ANDROID_SERIAL"] = self.serial
        timeouts = (300, 1200, 1200)
        for command, timeout in zip(checks, timeouts):
            self.tests_started = True
            subprocess.run(command, check=True, env=env, timeout=timeout)

    def stop(self) -> None:
        try:
            if self.process is not None:
                terminate_process_group(self.process)
        finally:
            if self.log_stream is not None:
                self.log_stream.close()
            if self.avd_home is not None:
                shutil.rmtree(self.avd_home, ignore_errors=True)


def sdk_root() -> Path:
    value = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not value:
        raise EmulatorFailure("ANDROID_HOME or ANDROID_SDK_ROOT is required")
    return Path(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5554)
    parser.add_argument("--boot-timeout", type=float, default=600)
    parser.add_argument("--ready-timeout", type=float, default=600)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/emulator"))
    args = parser.parse_args(argv)
    try:
        require_ci_environment(os.environ)
        supervisor = EmulatorSupervisor(sdk=sdk_root(), output_dir=args.output_dir,
                                        port=args.port, boot_timeout=args.boot_timeout,
                                        ready_timeout=args.ready_timeout,
                                        poll_interval=args.poll_interval)
    except EmulatorFailure as exc:
        print(f"CI emulator setup failed: {exc}", file=sys.stderr)
        return 1
    try:
        supervisor.start()
    except (EmulatorFailure, OSError, subprocess.SubprocessError) as exc:
        if supervisor.process is not None and not supervisor.tests_started:
            supervisor.collect_boot_diagnostics(str(exc))
        print(f"CI emulator setup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if not supervisor.ready:
            supervisor.stop()
    try:
        supervisor.run_checks()
    finally:
        supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
