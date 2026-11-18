"""Shared, explicit-device Android setup for integration and benchmark runners."""

from __future__ import annotations

import os
import json
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


class HarnessError(RuntimeError):
    pass


def adb_path() -> str:
    configured = os.environ.get("ASHNAZG_ADB")
    value = configured or shutil.which("adb")
    if not value:
        raise HarnessError("adb was not found; install Android platform-tools")
    return value


def select_device(adb: str | None = None) -> str:
    """Use ANDROID_SERIAL or select the sole running emulator; never guess among devices."""
    adb = adb or adb_path()
    result = subprocess.run([adb, "devices", "-l"], check=True, text=True, capture_output=True)
    devices = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            devices.append((fields[0], line))
    selected = os.environ.get("ANDROID_SERIAL")
    if selected:
        if not any(serial == selected for serial, _ in devices):
            raise HarnessError(f"ANDROID_SERIAL={selected!r} is not an online adb device")
        return selected
    emulators = [serial for serial, _ in devices if serial.startswith("emulator-")]
    if len(emulators) != 1:
        raise HarnessError(
            "set ANDROID_SERIAL explicitly, or provide exactly one online emulator; "
            f"found {len(emulators)} emulator(s) and {len(devices)} online device(s)"
        )
    return emulators[0]


class DeviceHarness:
    def __init__(self, readiness_timeout: float = 45.0, *, local_port: int = 7777):
        if type(local_port) is not int or not 0 <= local_port <= 65535:
            raise HarnessError("invalid local ADB forwarding port")
        self.local_port = local_port
        self.adb = adb_path()
        self.serial = select_device(self.adb)
        self.token = secrets.token_urlsafe(32)
        self.readiness_timeout = readiness_timeout
        self.instrumentation: subprocess.Popen | None = None
        self.instrument_log = tempfile.TemporaryFile(mode="w+b")
        self.forward_created = False
        self.started_at: float | None = None
        self.ready_at: float | None = None

    def adb_cmd(self, *args: str, check: bool = True, capture_output: bool = True,
                text: bool = True, timeout: float | None = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.adb, "-s", self.serial, *args], check=check,
            capture_output=capture_output, text=text, timeout=timeout, stdin=subprocess.DEVNULL,
        )

    def install(self, agent_apk: Path, fixture_apk: Path) -> None:
        for apk in (agent_apk, fixture_apk):
            if not apk.is_file():
                raise HarnessError(f"required APK does not exist: {apk}")
            self.adb_cmd("install", "-r", str(apk), timeout=180)

    def assert_available(self) -> None:
        """Refuse an existing local port forward before any package or app mutation."""
        existing = subprocess.run([self.adb, "forward", "--list"], check=True, text=True, capture_output=True).stdout.splitlines()
        for line in existing:
            fields = line.split()
            if len(fields) != 3:
                continue
            serial, local, remote = fields
            if local == f"tcp:{self.local_port}" or (serial == self.serial and remote == "tcp:7777"):
                raise HarnessError(f"ADB agent port is already forwarded for {serial}; refusing to overwrite it")

    @property
    def agent_addr(self) -> str:
        return f"127.0.0.1:{self.local_port}"

    def open_forward(self) -> None:
        result = self.adb_cmd("forward", "--no-rebind", f"tcp:{self.local_port}", "tcp:7777")
        if self.local_port == 0:
            port = result.stdout.strip()
            if not port.isascii() or not port.isdecimal() or len(port) > 5 or not 1 <= int(port) <= 65535:
                raise HarnessError("ADB did not return an allocated forwarding port")
            self.local_port = int(port)
        self.forward_created = True

    def close_forward(self, timeout: float = 120) -> None:
        if self.forward_created:
            self.adb_cmd("forward", "--remove", f"tcp:{getattr(self, 'local_port', 7777)}",
                         check=False, **({"timeout": timeout} if timeout != 120 else {}))
            self.forward_created = False

    def start(self, agent_apk: Path, fixture_apk: Path, *, install: bool = True) -> None:
        self.assert_available()
        self.started_at = time.perf_counter()
        if install:
            self.install(agent_apk, fixture_apk)
        self.open_forward()
        instrumentation_args = [self.adb, "-s", self.serial, "shell", "am", "instrument", "-w",
                                "-e", "token", self.token]
        if os.environ.get("ASHNAZG_CI_DIAGNOSTICS") == "1":
            instrumentation_args.extend(["-e", "traceWindowLookupMiss", "true"])
        instrumentation_args.append("dev.ashnazg.agent/dev.ashnazg.agent.AgentInstrumentation")
        self.instrumentation = subprocess.Popen(
            instrumentation_args,
            stdin=subprocess.DEVNULL, stdout=self.instrument_log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + self.readiness_timeout
        last_error = "connection refused"
        while time.monotonic() < deadline:
            if self.instrumentation.poll() is not None:
                raise HarnessError("instrumentation exited before readiness: " + self.instrumentation_output())
            try:
                with socket.create_connection(("127.0.0.1", self.local_port), timeout=0.6) as probe:
                    probe.settimeout(0.6)
                    probe.sendall(json.dumps({"jsonrpc": "2.0", "id": 1, "token": self.token,
                                             "method": "ping", "params": {}}, separators=(",", ":")).encode() + b"\n")
                    answer = probe.makefile("rb").readline((8 * 1024 * 1024) + 1)
                    response = json.loads(answer)
                    if response.get("id") != 1 or not isinstance(response.get("result", {}).get("apiLevel"), int):
                        raise OSError("agent did not return an authenticated ping response")
                    self.ready_at = time.perf_counter()
                    return
            except (OSError, ValueError, AttributeError) as exc:
                last_error = str(exc)
                time.sleep(0.15)
        raise HarnessError(f"agent did not become ready within {self.readiness_timeout}s: {last_error}")

    def instrumentation_output(self) -> str:
        self.instrument_log.flush()
        self.instrument_log.seek(0)
        return self.instrument_log.read().decode("utf-8", "replace")[-4000:]

    def collect_failure_diagnostics(self, output_dir: Path) -> Path | None:
        """Collect bounded selected-emulator diagnostics only with explicit opt-in."""
        if os.environ.get("ASHNAZG_CI_DIAGNOSTICS") != "1":
            return None

        commands = [
            ("activity_last_anr", ["shell", "dumpsys", "activity", "lastanr"], 24_000),
            ("logcat_tail", ["logcat", "-d", "-t", "2000", "-v", "brief"], 64_000),
            ("android_meminfo", ["shell", "cat", "/proc/meminfo"], 16_000),
            ("android_loadavg", ["shell", "cat", "/proc/loadavg"], 2_000),
            ("process_list", ["shell", "ps", "-A"], 24_000),
            ("agent_meminfo", ["shell", "dumpsys", "meminfo", "dev.ashnazg.agent"], 24_000),
            ("fixture_meminfo", ["shell", "dumpsys", "meminfo", "dev.ashnazg.fixture"], 24_000),
        ]

        def safe_output(value: str | bytes | None, limit: int) -> str:
            if isinstance(value, bytes):
                value = value.decode("utf-8", "replace")
            output = value or ""
            if self.token:
                output = output.replace(self.token, "<redacted>")
            return output[-limit:]

        captured = {}
        for name, argv, max_chars in commands:
            try:
                result = self.adb_cmd(*argv, check=False, timeout=20)
                captured[name] = {"exit_code": result.returncode,
                                  "stdout": safe_output(result.stdout, max_chars),
                                  "stderr": safe_output(result.stderr, 4_000)}
            except subprocess.TimeoutExpired as exc:
                captured[name] = {"error": safe_output(f"{type(exc).__name__}: {exc}", 4_000),
                                  "stdout": safe_output(exc.stdout, max_chars),
                                  "stderr": safe_output(exc.stderr, 4_000)}
            except (OSError, subprocess.SubprocessError) as exc:
                captured[name] = {"error": safe_output(f"{type(exc).__name__}: {exc}", 4_000)}

        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "failure-diagnostics.json"
        path.write_text(json.dumps({"collected_at_utc": datetime.now(timezone.utc).isoformat(),
                                    "device": self.serial, "diagnostics": captured}, indent=2) + "\n")
        return path

    @property
    def cold_setup_ms(self) -> float:
        if self.started_at is None or self.ready_at is None:
            raise HarnessError("device setup did not complete")
        return (self.ready_at - self.started_at) * 1000

    def cleanup(self) -> None:
        failures = []

        def attempt(step, operation):
            try:
                operation()
            except Exception as exc:
                failures.append((step, exc))

        def stop_instrumentation():
            if self.instrumentation is not None and self.instrumentation.poll() is None:
                self.instrumentation.terminate()
                try:
                    self.instrumentation.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.instrumentation.kill()
                    self.instrumentation.wait(timeout=5)

        try:
            attempt("instrumentation process", stop_instrumentation)
            if self.instrumentation is not None:
                attempt("agent force-stop", lambda: self.adb_cmd(
                    "shell", "am", "force-stop", "dev.ashnazg.agent", check=False, timeout=20))
            attempt("owned forward", self.close_forward)
        finally:
            attempt("instrumentation log", self.instrument_log.close)
        if failures:
            error = HarnessError("session cleanup failed: " + "; ".join(step for step, _ in failures))
            error.failures = tuple(failures)  # Preserve causes without printing token-bearing command arguments.
            raise error from None


def artifact_apks(root: Path) -> tuple[Path, Path]:
    base = root / "agent-android"
    return (
        base / "agent/build/outputs/apk/debug/agent-debug.apk",
        base / "fixture/build/outputs/apk/debug/fixture-debug.apk",
    )
