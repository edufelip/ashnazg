#!/usr/bin/env python3
"""Bounded, ownership-safe supervisor for a local Linux ReDroid container."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

if __package__:
    from .ci_emulator import focused_application_window, last_anr_status, readiness_status, terminate_process_group
else:
    from ci_emulator import focused_application_window, last_anr_status, readiness_status, terminate_process_group


IMAGE_INDEX = "sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad"
IMAGE = f"redroid/redroid@{IMAGE_INDEX}"
PLATFORM_DIGESTS = {
    "amd64": "sha256:25879821f13ffd38d5d6047d832379b96cf72ec520508b45036dca25cdc3737f",
    "arm64": "sha256:b21c57533662275928832602f7c09b83cb198aa9b271135ae49d978857d6795f",
}
DISPLAY = {"width": 720, "height": 1280, "dpi": 320, "fps": 30, "renderer": "software", "ashmem": "memfd"}
MAX_LOG_BYTES = 16_384
SERIAL_PREFIX = "127.0.0.1:"


class RedroidError(RuntimeError):
    pass


class RunSignal(BaseException):
    def __init__(self, signum: int):
        self.signum = signum


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def validate_host(system: str, architecture: str, binder_available: bool) -> str:
    if system != "Linux":
        raise RedroidError("ReDroid requires a Linux host")
    normalized = {"x86_64": "amd64", "aarch64": "arm64"}.get(architecture, architecture)
    if normalized not in PLATFORM_DIGESTS:
        raise RedroidError(f"unsupported host architecture: {architecture}")
    if not binder_available:
        raise RedroidError("Binder device /dev/binder is unavailable")
    return normalized


def verify_image_index(raw: str, index_digest: str, architecture: str) -> str:
    if index_digest != IMAGE_INDEX:
        raise RedroidError("pinned image index digest does not match the contract")
    try:
        document = json.loads(raw)
        manifests = document["manifests"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RedroidError("Docker returned an unrecognized image index") from exc
    matches = [m.get("digest") for m in manifests
               if m.get("platform", {}).get("os") == "linux"
               and m.get("platform", {}).get("architecture") == architecture]
    expected = PLATFORM_DIGESTS.get(architecture)
    if len(matches) != 1 or matches[0] != expected:
        raise RedroidError(f"pinned image has an unexpected linux/{architecture} platform digest")
    return matches[0]


def check_port_available(port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
    except OSError as exc:
        raise RedroidError(f"ADB loopback port {port} is unavailable: {exc}") from exc


def _bounded(value: str, limit: int = MAX_LOG_BYTES) -> str:
    value = re.sub(r"(?i)(token|password|secret)(\s*[=:]\s*)\S+", r"\1\2[REDACTED]", value)
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) > limit:
        encoded = b"[truncated]\n" + encoded[-(limit - 12):]
    return encoded.decode("utf-8", errors="replace")


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


class RedroidSupervisor:
    def __init__(self, *, output_dir: Path, adb_port: int = 5555, timeout: int = 180,
                 command_timeout: int = 2400,
                 runner: Callable = subprocess.run, host_system: str | None = None,
                 host_arch: str | None = None, binder_path: Path = Path("/dev/binder"),
                 docker_path: str | None = None, adb_path: str | None = None,
                 binder_available: bool | None = None):
        self.output_dir = Path(output_dir)
        self.adb_port = adb_port
        self.timeout = timeout
        self.command_timeout = command_timeout
        self.runner = runner
        self.host_system = host_system or platform.system()
        self.host_arch = host_arch or platform.machine()
        self.binder_path = Path(binder_path)
        self.binder_available = binder_available
        self.docker_path = docker_path
        self.adb_path = adb_path
        self.architecture = ""
        self.run_id = str(uuid.uuid4())
        self.container_name = f"ashnazg-redroid-{self.run_id}"
        self.serial = f"{SERIAL_PREFIX}{self.adb_port}"
        self.deadline = 0.0
        self.container_started = False
        self.adb_connected = False
        self.child: subprocess.Popen | None = None
        self.receipt: dict = {}
        self.docker_log = self.output_dir / "container.log"
        self.started = time.monotonic()

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RedroidError(f"operation exceeded {self.timeout}s timeout")
        return remaining

    def _command(self, args: Sequence[str], *, timeout: float | None = None, check: bool = True,
                 use_deadline: bool = True) -> CommandResult:
        try:
            bounded_timeout = timeout or 30
            if use_deadline:
                bounded_timeout = min(bounded_timeout, self._remaining())
            completed = self.runner(list(args), capture_output=True, text=True,
                                    timeout=bounded_timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RedroidError(f"command timed out: {args[0]}") from exc
        stdout = completed.stdout or ""
        if len(stdout.encode("utf-8", errors="replace")) > 1_048_576:
            raise RedroidError(f"structured command output exceeds 1 MiB: {args[0]}")
        result = CommandResult(completed.returncode, stdout,
                               _bounded(completed.stderr or ""))
        if check and result.returncode:
            details = result.stderr.strip() or result.stdout.strip()
            raise RedroidError(f"{args[0]} command failed (exit {result.returncode}): {details[:1200]}")
        return result

    def _adb(self, *args: str, check: bool = True, timeout: float = 10) -> CommandResult:
        return self._command([self.adb_path or "adb", "-s", self.serial, *args],
                             timeout=timeout, check=check)

    def _preflight(self) -> None:
        binder_available = self.binder_available
        if binder_available is None:
            try:
                binder_available = stat.S_ISCHR(self.binder_path.stat().st_mode) and os.access(
                    self.binder_path, os.R_OK | os.W_OK)
            except OSError:
                binder_available = False
        self.architecture = validate_host(self.host_system, self.host_arch, binder_available)
        self.docker_path = self.docker_path or shutil.which("docker")
        self.adb_path = self.adb_path or shutil.which("adb")
        if not self.docker_path:
            raise RedroidError("Docker CLI is unavailable")
        if not self.adb_path:
            raise RedroidError("ADB CLI is unavailable")
        endpoint = self._command([self.docker_path, "context", "inspect", "--format",
                                  "{{.Endpoints.docker.Host}}"], timeout=10).stdout.strip()
        endpoint = os.environ.get("DOCKER_HOST") or endpoint
        if not endpoint.startswith("unix://"):
            raise RedroidError("ReDroid requires a local Unix-socket Docker endpoint")
        check_port_available(self.adb_port)
        devices = self._command([self.adb_path, "devices", "-l"], check=True, timeout=10).stdout
        if re.search(rf"(?m)^{re.escape(self.serial)}\s+", devices):
            raise RedroidError(f"ADB serial {self.serial} is already in use")
        raw_index = self._command([self.docker_path, "buildx", "imagetools", "inspect", "--raw", IMAGE],
                                  timeout=30).stdout
        platform_digest = verify_image_index(raw_index, IMAGE_INDEX, self.architecture)
        self.receipt["platform_digest"] = platform_digest

    def _write_metadata(self) -> None:
        source_dirty = None
        try:
            repository = str(Path(__file__).resolve().parent.parent)
            source_sha = subprocess.run(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=3, check=False
            ).stdout.strip() or None
            status = subprocess.run(["git", "-C", repository, "status", "--porcelain"],
                                    capture_output=True, text=True, timeout=3, check=False)
            if status.returncode == 0:
                source_dirty = bool(status.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            source_sha = None
        self.receipt.update({
            "schema": 1, "run_id": self.run_id, "source_sha": source_sha,
            "source_dirty": source_dirty,
            "github_sha": os.environ.get("GITHUB_SHA"),
            "image_index": IMAGE_INDEX, "image": IMAGE, "host": {
                "system": self.host_system, "kernel": platform.release(),
                "architecture": self.host_arch, "runner_image": os.environ.get("ImageOS") or os.environ.get("RUNNER_OS_IMAGE"),
            }, "requested_display": DISPLAY, "adb_port": self.adb_port, "serial": self.serial,
            "started_at_unix": time.time(), "status": "running", "setup_timings_seconds": {},
        })

    def _launch_container(self) -> None:
        args = [self.docker_path, "run", "--detach", "--privileged", "--name", self.container_name,
                "--label", f"org.ashnazg.redroid.run-id={self.run_id}",
                "--platform", f"linux/{self.architecture}", "--volume", "/data",
                "--publish", f"127.0.0.1:{self.adb_port}:5555", "--device", "/dev/binder:/dev/binder",
                "--env", "ANDROID_BINDER_DEVICES=binder,hwbinder,vndbinder", IMAGE,
                "androidboot.redroid_width=720", "androidboot.redroid_height=1280",
                "androidboot.redroid_dpi=320", "androidboot.redroid_fps=30",
                "androidboot.redroid_gpu_mode=guest", "androidboot.use_memfd=true"]
        # A timed-out Docker client may already have created the container. Cleanup
        # inspects its unique label before acting, including this ambiguous path.
        self.container_started = True
        result = self._command(args, timeout=self._remaining())
        self.receipt["container_id"] = result.stdout.strip()[:128]

    def _wait_ready(self) -> None:
        self.adb_connected = True
        self._command([self.adb_path, "connect", self.serial], timeout=10, check=False)
        previous_window = None
        last_reason = "ADB device has not booted"
        while True:
            self.receipt["last_readiness"] = last_reason
            self._remaining()
            state = self._adb("get-state", check=False, timeout=5)
            if state.returncode != 0 or state.stdout.strip() != "device":
                self._command([self.adb_path, "connect", self.serial], timeout=5, check=False)
            if state.returncode == 0 and state.stdout.strip() == "device":
                boot = self._adb("shell", "getprop", "sys.boot_completed", check=False, timeout=5)
                if boot.returncode == 0 and boot.stdout.strip() == "1":
                    anr = self._adb("shell", "dumpsys", "activity", "lastanr", check=False, timeout=10)
                    if last_anr_status(anr.stdout) is True:
                        raise RedroidError("ReDroid recorded an ANR during boot")
                    input_state = self._adb("shell", "dumpsys", "input", check=False, timeout=10)
                    windows = self._adb("shell", "dumpsys", "window", "-a", "windows", check=False, timeout=10)
                    status, reason = readiness_status(input_state.stdout, windows.stdout, anr.stdout)
                    if status and input_state.returncode == windows.returncode == anr.returncode == 0:
                        window = focused_application_window(input_state.stdout)
                        if window == previous_window:
                            self.receipt["readiness"] = reason
                            return
                        previous_window = window
                        last_reason = "focused, drawn app window has not persisted across readiness checks"
                    else:
                        previous_window = None
                        last_reason = reason
            time.sleep(min(1.0, self._remaining()))

    def _run_child(self, command: Sequence[str]) -> int:
        env = os.environ.copy()
        env["ANDROID_SERIAL"] = self.serial
        self.child = subprocess.Popen(list(command), stdin=None, env=env, start_new_session=True)
        try:
            try:
                code = self.child.wait(timeout=self.command_timeout or None)
            except subprocess.TimeoutExpired:
                raise RedroidError(f"child command exceeded {self.command_timeout}s timeout")
        finally:
            # The command owns a new process group. Reap descendants even when its
            # shell exits first; never signal another automation session.
            terminate_process_group(self.child, grace_seconds=3)
            self.child = None
        return code if code >= 0 else 128 - code

    def _sample_resources(self, phase: str) -> None:
        try:
            sample = self._command([self.docker_path or "docker", "stats", "--no-stream", "--format",
                                    "{{json .}}", self.container_name], timeout=5, check=False,
                                   use_deadline=False)
            if sample.returncode:
                raise RedroidError(sample.stderr or "Docker stats unavailable")
            value = json.loads(sample.stdout)
            if not isinstance(value, dict):
                raise ValueError("unexpected Docker stats response")
            self.receipt.setdefault("resource_samples", {})[phase] = value
        except Exception as exc:
            self.receipt.setdefault("resource_samples", {})[phase] = {"unavailable": _bounded(str(exc), 1000)}

    def _owned_container(self) -> bool:
        inspect = self._command([self.docker_path or "docker", "inspect", "--format",
                                 "{{ index .Config.Labels \"org.ashnazg.redroid.run-id\" }}", self.container_name],
                                timeout=5, check=False, use_deadline=False)
        return inspect.returncode == 0 and inspect.stdout.strip() == self.run_id

    def _cleanup(self) -> None:
        if not self.container_started:
            return
        try:
            if not self._owned_container():
                self.receipt["cleanup_refused"] = "container ownership label did not match this run"
                return
        except Exception as exc:
            self.receipt["cleanup_refused"] = "could not verify container ownership: " + _bounded(str(exc), 1200)
            return
        if self.adb_connected:
            try:
                self._command([self.adb_path or "adb", "disconnect", self.serial],
                              timeout=5, check=False, use_deadline=False)
            except Exception as exc:
                self.receipt["adb_cleanup_error"] = _bounded(str(exc), 1200)
        try:
            if os.environ.get("ASHNAZG_CI_DIAGNOSTICS") != "1":
                return
            logs = self._command([self.docker_path or "docker", "logs", "--tail", "200", self.container_name],
                                 timeout=5, check=False, use_deadline=False)
            self.docker_log.parent.mkdir(parents=True, exist_ok=True)
            self.docker_log.write_text(_bounded(logs.stdout + logs.stderr), encoding="utf-8")
        except Exception as exc:
            self.receipt["cleanup_diagnostics_error"] = _bounded(str(exc), 1200)
        finally:
            try:
                removed = self._command([self.docker_path or "docker", "rm", "--force", "--volumes", self.container_name],
                                        timeout=10, check=False, use_deadline=False)
                if removed.returncode:
                    self.receipt["cleanup_error"] = _bounded(removed.stderr or removed.stdout, 1200)
            except Exception as exc:
                self.receipt["cleanup_error"] = _bounded(str(exc), 1200)

    def _collect_diagnostics(self) -> None:
        if not self.container_started or os.environ.get("ASHNAZG_CI_DIAGNOSTICS") != "1":
            return
        probes = {
            "boot_completed": ("shell", "getprop", "sys.boot_completed"),
            "last_anr": ("shell", "dumpsys", "activity", "lastanr"),
            "input": ("shell", "dumpsys", "input"),
            "windows": ("shell", "dumpsys", "window", "-a", "windows"),
            "logcat": ("shell", "logcat", "-d", "-t", "300"),
        }
        diagnostics = {}
        for name, arguments in probes.items():
            try:
                result = self._command([self.adb_path or "adb", "-s", self.serial, *arguments],
                                       timeout=3, check=False, use_deadline=False)
                diagnostics[name] = {"exit_code": result.returncode,
                                     "output": _bounded(result.stdout + result.stderr, 4096)}
            except Exception as exc:
                diagnostics[name] = {"error": _bounded(str(exc), 1000)}
        self.receipt["diagnostics"] = diagnostics

    def _save_receipt(self, status: str, failure: str | None = None, exit_code: int | None = None) -> None:
        self.receipt.update({"status": status, "elapsed_seconds": round(time.monotonic() - self.started, 3),
                             "finished_at_unix": time.time()})
        if failure:
            self.receipt["failure"] = _bounded(failure, 2000)
        if exit_code is not None:
            self.receipt["exit_code"] = exit_code
        _atomic_json(self.output_dir / "receipt.json", self.receipt)

    def run(self, command: Sequence[str]) -> int:
        if not command:
            raise RedroidError("a command after -- is required")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.deadline = time.monotonic() + self.timeout
        self._write_metadata()
        prior_handlers = {}

        def on_signal(signum, _frame):
            raise RunSignal(signum)

        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            try:
                prior_handlers[signum] = signal.signal(signum, on_signal)
            except ValueError:  # not running on the main thread
                pass
        failure = None
        code = 1
        status = "failure"
        try:
            started = time.monotonic()
            self._preflight()
            self.receipt["setup_timings_seconds"]["preflight"] = round(time.monotonic() - started, 3)
            started = time.monotonic()
            self._launch_container()
            self.receipt["setup_timings_seconds"]["container_start"] = round(time.monotonic() - started, 3)
            started = time.monotonic()
            self._wait_ready()
            self.receipt["setup_timings_seconds"]["readiness"] = round(time.monotonic() - started, 3)
            self._sample_resources("before_command")
            code = self._run_child(command)
            self.receipt["command_exit_code"] = code
            self._sample_resources("after_command")
            status = "success" if code == 0 else "command_failure"
        except RunSignal as exc:
            failure = f"interrupted by signal {exc.signum}"
            code = 128 + exc.signum
            status = "interrupted"
        except Exception as exc:
            failure = str(exc)
            status = "timeout" if "timeout" in failure.lower() or "exceeded" in failure.lower() else "failure"
            code = 1
        finally:
            if status != "success":
                self._collect_diagnostics()
            self._cleanup()
            if code == 0 and any(key in self.receipt for key in ("cleanup_error", "cleanup_refused", "adb_cleanup_error")):
                status = "cleanup_failure"
                code = 1
            self._save_receipt(status, failure, code)
            for signum, handler in prior_handlers.items():
                signal.signal(signum, handler)
        return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    run = subparsers.add_parser("run", help="run a command against an owned ReDroid instance")
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--adb-port", type=int, default=5555)
    run.add_argument("--timeout", type=int, default=180)
    run.add_argument("--command-timeout", type=int, default=2400,
                     help="command deadline in seconds; 0 explicitly allows a persistent service")
    run.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not 1 <= args.adb_port <= 65535:
        parser.error("--adb-port must be between 1 and 65535")
    if not 1 <= args.timeout <= 3600:
        parser.error("--timeout must be between 1 and 3600 seconds")
    if not 0 <= args.command_timeout <= 86400:
        parser.error("--command-timeout must be between 0 and 86400 seconds")
    try:
        return RedroidSupervisor(output_dir=args.output_dir, adb_port=args.adb_port,
                                 timeout=args.timeout, command_timeout=args.command_timeout).run(command)
    except RedroidError as exc:
        print(f"redroid: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
