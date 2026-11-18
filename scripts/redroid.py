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
    from .session_data import SessionData
    from .ci_emulator import focused_application_window, last_anr_status, readiness_status, terminate_process_group
else:
    from session_data import SessionData
    from ci_emulator import focused_application_window, last_anr_status, readiness_status, terminate_process_group


IMAGE_INDEX = "sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad"
IMAGE = f"redroid/redroid@{IMAGE_INDEX}"
PLATFORM_DIGESTS = {
    "amd64": "sha256:25879821f13ffd38d5d6047d832379b96cf72ec520508b45036dca25cdc3737f",
    "arm64": "sha256:b21c57533662275928832602f7c09b83cb198aa9b271135ae49d978857d6795f",
}
IMAGE_PROFILES = {
    "default": {
        "index": IMAGE_INDEX,
        "platform_digests": PLATFORM_DIGESTS,
    },
    "android15-64only": {
        "index": "sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee",
        "platform_digests": {
            "amd64": "sha256:58e548d78c4854f032912757dc0782d7a86b9d602ffc10b2f09fb9e86731adf3",
            "arm64": "sha256:dc2024a999dd0acb1112a23cddc9537ec8cb60a0dbe5a87ce9fbd75a2f5e94d2",
        },
    },
}
DISPLAY = {"width": 720, "height": 1280, "dpi": 320, "fps": 30, "renderer": "software", "ashmem": "memfd"}
MAX_LOG_BYTES = 16_384
SERIAL_PREFIX = "127.0.0.1:"


class RedroidError(RuntimeError):
    pass


def validate_image_provenance(path: Path, *, architecture: str, checkout_sha: str,
                              checkout_dirty: bool, inspect_raw: str) -> str:
    """Validate builder output and a fresh inspect of the local image ID."""
    try:
        # Import only for the opt-in derived-image path; the pinned default is independent.
        if __package__:
            from . import build_android_image as builder
        else:
            import build_android_image as builder
        if Path(path).is_symlink() or not Path(path).is_file():
            raise ValueError("provenance must be a regular file")
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        required = {"schema", "recipe_version", "source_sha", "source_dirty", "upstream_base",
                    "platform", "apk_inputs", "image_inspect", "notices", "qualification"}
        if not isinstance(doc, dict) or set(doc) != required:
            raise ValueError("descriptor schema is incomplete")
        if doc["schema"] != 1 or doc["recipe_version"] != builder.VERSION:
            raise ValueError("unsupported descriptor schema or recipe version")
        source_sha = doc.get("source_sha")
        if (not isinstance(source_sha, str) or not builder.SHA_RE.fullmatch(source_sha) or
                doc["source_dirty"] is not False or checkout_dirty or source_sha != checkout_sha):
            raise ValueError("source provenance does not match a clean checkout HEAD")
        platform_name = f"linux/{architecture}"
        if doc["platform"] != platform_name:
            raise ValueError("image platform does not match this host")
        base = doc["upstream_base"]
        if (base.get("image") != builder.BASE_IMAGE or base.get("index_digest") != builder.BASE_INDEX or
                base.get("platform_digest") != builder.BASE_PLATFORM_DIGESTS[platform_name]):
            raise ValueError("unknown upstream base provenance")
        inspect = doc["image_inspect"]
        if not isinstance(inspect, dict) or not {"id", "repo_tags", "os", "architecture", "variant",
                                                  "config", "rootfs_layers"}.issubset(inspect):
            raise ValueError("descriptor image inspect schema is incomplete")
        image_id = inspect.get("id")
        if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("descriptor image ID is invalid")
        apks = doc["apk_inputs"]
        labels = inspect.get("config", {}).get("labels")
        if not isinstance(labels, dict) or not isinstance(apks, dict) or set(apks) != {"agent", "fixture"}:
            raise ValueError("descriptor APK or label provenance is incomplete")
        expected = {
            "org.ashnazg.base-image": builder.BASE_IMAGE,
            "org.ashnazg.version": builder.VERSION,
            "org.ashnazg.source-sha": checkout_sha,
            "org.ashnazg.platform": platform_name,
        }
        for name in ("agent", "fixture"):
            digest = apks[name]["sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"invalid {name} APK digest")
            expected[f"org.ashnazg.{name}-sha256"] = digest
        if any(labels.get(key) != value for key, value in expected.items()):
            raise ValueError("descriptor provenance labels do not match expected values")
        actual = builder._inspect_metadata(
            inspect_raw, platform=platform_name, tag=f"ashnazg-redroid:{builder.VERSION}",
            source_sha=checkout_sha, version=builder.VERSION,
            apk_sha256={name: apks[name]["sha256"] for name in ("agent", "fixture")},
        )
        # Tags are mutable aliases; the immutable image and its runtime configuration
        # must match, while extra local aliases are irrelevant to the launched ID.
        if any(actual[key] != inspect[key] for key in (
                "id", "os", "architecture", "variant", "config", "rootfs_layers")):
            raise ValueError("local image inspect does not match provenance descriptor")
        return image_id
    except RedroidError:
        raise
    except Exception as exc:
        raise RedroidError(f"invalid local image provenance: {exc}") from exc


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


def verify_image_index(raw: str, index_digest: str, architecture: str, *, profile: str = "default") -> str:
    selected = IMAGE_PROFILES.get(profile)
    if selected is None:
        raise RedroidError(f"unknown image profile: {profile}")
    if index_digest != selected["index"]:
        raise RedroidError("pinned image index digest does not match the contract")
    try:
        document = json.loads(raw)
        manifests = document["manifests"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RedroidError("Docker returned an unrecognized image index") from exc
    matches = [m.get("digest") for m in manifests
               if m.get("platform", {}).get("os") == "linux"
               and m.get("platform", {}).get("architecture") == architecture]
    expected = selected["platform_digests"].get(architecture)
    if len(matches) != 1 or matches[0] != expected:
        raise RedroidError(f"pinned image has an unexpected linux/{architecture} platform digest")
    return matches[0]


def check_port_available(port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # Docker's published listener uses address reuse too. A reclaimed
            # endpoint may still have TIME_WAIT connections after guest removal.
            # This does not permit sharing an active listener (no SO_REUSEPORT).
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
    except OSError as exc:
        raise RedroidError(f"ADB loopback port {port} is unavailable: {exc}") from exc


def _bounded(value: str, limit: int = MAX_LOG_BYTES) -> str:
    value = re.sub(
        r"(?i)(--?[\w.-]*(?:token|password|secret)|[\"']?[\w.-]*(?:token|password|secret)[\"']?)"
        r"(\s*(?:[:=]\s*|\s+))(?:(?:\"(?:\\.|[^\"])*\")|(?:'(?:\\.|[^'])*')|[^\s,;}]+)",
        r"\1\2[REDACTED]", value,
    )
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
                 binder_available: bool | None = None, image_provenance: Path | None = None,
                 image_profile: str = "default", state_dir: Path | None = None, fps: int | None = None, startup_attempts: int = 1,
                 binder_bootstrap: Path | None = None):
        if image_profile not in IMAGE_PROFILES:
            raise RedroidError(f"unknown image profile: {image_profile}")
        if image_profile != "default" and image_provenance:
            raise RedroidError("image profiles cannot be combined with image provenance until that contract exists")
        if fps not in (None, 30, 60):
            raise RedroidError("fps must be 30 or 60")
        if type(startup_attempts) is not int or not 1 <= startup_attempts <= 3:
            raise RedroidError("startup attempts must be 1..3")
        self.binder_bootstrap = binder_bootstrap.resolve() if binder_bootstrap else None
        if self.binder_bootstrap:
            if image_profile != "android15-64only" or image_provenance:
                raise RedroidError("private Binder bootstrap requires the pinned android15-64only profile")
            if (not self.binder_bootstrap.is_file() or not os.access(self.binder_bootstrap, os.X_OK)
                    or any(c in str(self.binder_bootstrap) for c in ",\n\r")):
                raise RedroidError("private Binder bootstrap must be a local executable")
        self.startup_attempts = startup_attempts
        self.state_dir = state_dir
        self.storage = None
        self.fps = fps
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
        self.image_provenance = Path(image_provenance) if image_provenance else None
        self.image_profile = image_profile
        self.image_index = IMAGE_PROFILES[image_profile]["index"]
        self.platform_digests = IMAGE_PROFILES[image_profile]["platform_digests"]
        self.image = f"redroid/redroid@{self.image_index}"
        self.display = {**DISPLAY, "fps": fps or DISPLAY["fps"]}
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
            completed = self.runner(list(args), capture_output=True, text=True, stdin=subprocess.DEVNULL,
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
        self.architecture = validate_host(self.host_system, self.host_arch, binder_available or self.binder_bootstrap is not None)
        if self.binder_bootstrap and self.architecture != "amd64":
            raise RedroidError("private Binder bootstrap is qualified only for Linux x86_64")
        if self.binder_bootstrap:
            self.receipt["binder_mode"] = "private guest Binderfs"
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
        if self.state_dir is None:
            self._check_endpoint()
        if self.image_provenance:
            repository = str(Path(__file__).resolve().parent.parent)
            try:
                source_sha = subprocess.run(["git", "-C", repository, "rev-parse", "HEAD"],
                                            capture_output=True, text=True, timeout=5, check=True).stdout.strip()
                dirty = bool(subprocess.run(["git", "-C", repository, "status", "--porcelain",
                                             "--untracked-files=all"], capture_output=True, text=True,
                                            timeout=5, check=True).stdout.strip())
            except (OSError, subprocess.SubprocessError) as exc:
                raise RedroidError("cannot verify local image source checkout") from exc
            try:
                descriptor = json.loads(self.image_provenance.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RedroidError(f"invalid local image provenance: {exc}") from exc
            image_id = descriptor.get("image_inspect", {}).get("id") if isinstance(descriptor, dict) else None
            if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise RedroidError("invalid local image provenance: descriptor image ID is invalid")
            actual = self._command([self.docker_path, "image", "inspect", image_id], timeout=15).stdout
            self.image = validate_image_provenance(self.image_provenance, architecture=self.architecture,
                                                   checkout_sha=source_sha, checkout_dirty=dirty,
                                                   inspect_raw=actual)
            if self.fps not in (None, 60):
                raise RedroidError("derived image requires 60 fps")
            # The recipe's known runtime profile is fixed at 60 fps; no governor claim is made.
            self.display = {**DISPLAY, "fps": 60, "renderer": "guest"}
            self.receipt["image_provenance"] = str(self.image_provenance)
            self.receipt["image_profile"] = self.display
            self.receipt["image"] = self.image
            self.receipt["requested_display"] = self.display
        else:
            raw_index = self._command([self.docker_path, "buildx", "imagetools", "inspect", "--raw", self.image],
                                      timeout=30).stdout
            platform_digest = verify_image_index(raw_index, self.image_index, self.architecture,
                                                 profile=self.image_profile)
            self.receipt["image_index"] = self.image_index
            self.receipt["image"] = self.image
            self.receipt["image_profile"] = self.image_profile
            self.receipt["platform_digest"] = platform_digest

    def _check_endpoint(self) -> None:
        check_port_available(self.adb_port)
        devices = self._command([self.adb_path, "devices", "-l"], check=True, timeout=10).stdout
        if re.search(rf"(?m)^{re.escape(self.serial)}\s+", devices):
            raise RedroidError(f"ADB serial {self.serial} is already in use")

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
            "image_index": self.image_index if not self.image_provenance else None, "image": self.image,
            "image_profile": self.image_profile, "host": {
                "system": self.host_system, "kernel": platform.release(),
                "architecture": self.host_arch, "runner_image": os.environ.get("ImageOS") or os.environ.get("RUNNER_OS_IMAGE"),
            }, "requested_display": self.display, "adb_port": self.adb_port, "serial": self.serial,
            "started_at_unix": time.time(), "status": "running", "setup_timings_seconds": {},
        })

    def _prepare_storage(self) -> None:
        if self.state_dir is None:
            return
        self.storage = SessionData(self.state_dir, {
            "image": self.image, "architecture": self.architecture, "display": self.display,
        })
        self.storage.__enter__()
        self.receipt["storage"] = {"identity": self.storage.identity,
                                   "directory": str(self.storage.path), "persistent": True}
        # A killed supervisor releases flock but can leave a privileged container
        # alive. Fence it before attaching /data to a new Android instance.
        ids = self._command([self.docker_path, "ps", "--all", "--quiet", "--no-trunc",
                             "--filter", f"volume={self.storage.data}"], timeout=10).stdout.split()
        for container_id in ids:
            if not re.fullmatch(r"[0-9a-f]{64}", container_id):
                raise RedroidError("invalid container identity during storage recovery")
            document = json.loads(self._command([self.docker_path, "inspect", container_id], timeout=10).stdout)
            entry = document[0] if isinstance(document, list) and len(document) == 1 else {}
            labels = entry.get("Config", {}).get("Labels") or {}
            mounts = entry.get("Mounts") or []
            if (entry.get("Id") != container_id or
                    labels.get("org.ashnazg.redroid.data-id") != self.storage.identity or
                    not labels.get("org.ashnazg.redroid.run-id") or
                    not any(m.get("Source") == str(self.storage.data) and m.get("Destination") == "/data"
                            and m.get("Type") == "bind" for m in mounts)):
                raise RedroidError("storage container ownership could not be verified; refusing recovery")
            self._command([self.docker_path, "rm", "--force", container_id], timeout=15)
            self.receipt.setdefault("recovered_containers", []).append(container_id)
            # Remove stale ADB registrations only for this verified old guest's
            # loopback-published endpoint, never disconnect all devices.
            ports = (entry.get("NetworkSettings", {}).get("Ports") or {}).get("5555/tcp")
            # Docker clears NetworkSettings.Ports after a guest crash/stop.
            bindings = ports or (entry.get("HostConfig", {}).get("PortBindings") or {}).get("5555/tcp") or []
            for binding in bindings:
                port = binding.get("HostPort", "")
                if binding.get("HostIp") == "127.0.0.1" and port.isdecimal() and 1 <= int(port) <= 65535:
                    self._command([self.adb_path or "adb", "disconnect", f"127.0.0.1:{port}"],
                                  timeout=5)

    def _launch_container(self) -> None:
        args = [self.docker_path, "run", "--detach", "--privileged", "--name", self.container_name,
                "--label", f"org.ashnazg.redroid.run-id={self.run_id}",
                "--platform", f"linux/{self.architecture}",
                "--publish", f"127.0.0.1:{self.adb_port}:5555"]
        if self.binder_bootstrap:
            args.extend(["--mount", f"type=bind,source={self.binder_bootstrap},target=/ashnazg-binder-bootstrap,readonly",
                         "--entrypoint", "/ashnazg-binder-bootstrap"])
        else:
            args.extend(["--device", "/dev/binder:/dev/binder",
                         "--env", "ANDROID_BINDER_DEVICES=binder,hwbinder,vndbinder"])
        args.append(self.image)
        storage_args = (["--label", f"org.ashnazg.redroid.data-id={self.storage.identity}",
                         "--mount", f"type=bind,source={self.storage.data},target=/data"]
                        if self.storage else ["--volume", "/data"])
        args[-1:-1] = storage_args
        if self.binder_bootstrap:
            args.extend(["/init", "qemu=1", "androidboot.hardware=redroid"])
        if not self.image_provenance:
            args.extend(["androidboot.redroid_width=720", "androidboot.redroid_height=1280",
                         "androidboot.redroid_dpi=320", f"androidboot.redroid_fps={self.display['fps']}",
                         "androidboot.redroid_gpu_mode=guest", "androidboot.use_memfd=true"])
        # A timed-out Docker client may already have created the container. Cleanup
        # inspects its unique label before acting, including this ambiguous path.
        self.container_started = True
        result = self._command(args, timeout=self._remaining())
        self.receipt["container_id"] = result.stdout.strip()[:128]

    def _wait_ready(self) -> None:
        self.adb_connected = True
        connect_pending = True
        previous_window = None
        last_reason = "ADB device has not booted"
        while True:
            self.receipt["last_readiness"] = last_reason
            self._remaining()
            try:
                if connect_pending:
                    self._command([self.adb_path, "connect", self.serial], timeout=10, check=False)
                    connect_pending = False
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
            except RedroidError as exc:
                # A slow boot can time out one ADB probe. Retry only that case;
                # every probe and sleep still uses the original boot deadline.
                if not isinstance(exc.__cause__, subprocess.TimeoutExpired):
                    raise
                previous_window = None
                last_reason = "ADB readiness probe timed out; waiting within the boot deadline"
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

    def _owned_container(self, timeout: float = 5.0) -> bool:
        inspect = self._command([self.docker_path or "docker", "inspect", "--format",
                                 "{{ index .Config.Labels \"org.ashnazg.redroid.run-id\" }}", self.container_name],
                                timeout=timeout, check=False, use_deadline=False)
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
                removed = self._command([self.docker_path or "docker", "rm", "--force",
                                         *([] if self.storage else ["--volumes"]), self.container_name],
                                        timeout=10, check=False, use_deadline=False)
                if removed.returncode:
                    self.receipt["cleanup_error"] = _bounded(removed.stderr or removed.stdout, 1200)
            except Exception as exc:
                self.receipt["cleanup_error"] = _bounded(str(exc), 1200)

    def _collect_diagnostics(self, budget_seconds: float = 45.0) -> None:
        if not self.container_started or os.environ.get("ASHNAZG_CI_DIAGNOSTICS") != "1":
            return
        budget_seconds = max(0.0, min(45.0, budget_seconds))
        if not budget_seconds:
            self.receipt["diagnostics_skipped"] = "startup recovery reserved remaining time"
            return
        container_probes = (
            ("container_logcat", ("/system/bin/logcat", "-b", "all", "-d", "-t", "250")),
            ("container_boot_completed", ("/system/bin/getprop", "sys.boot_completed")),
            ("lmk_use_psi", ("/system/bin/getprop", "ro.lmk.use_psi")),
            ("lmk_use_minfree_levels", ("/system/bin/getprop", "ro.lmk.use_minfree_levels")),
            ("low_ram", ("/system/bin/getprop", "ro.config.low_ram")),
            ("processes", ("/system/bin/pidof", "lmkd", "system_server", "surfaceflinger")),
            ("memory_pressure", ("/system/bin/cat", "/proc/pressure/memory")),
            ("kernel_cgroups", ("/system/bin/cat", "/proc/cgroups")),
            ("cgroup_controllers", ("/system/bin/cat", "/sys/fs/cgroup/cgroup.controllers")),
            ("mountinfo", ("/system/bin/cat", "/proc/self/mountinfo")),
            ("system_cgroup_config", ("/system/bin/cat", "/system/etc/cgroups.json")),
            ("vendor_cgroup_config", ("/system/bin/cat", "/vendor/etc/cgroups.json")),
        )
        started = time.monotonic()
        try:
            if not self._owned_container(timeout=min(5.0, budget_seconds)):
                self.receipt["diagnostics_skipped"] = "container ownership could not be verified"
                return
        except Exception:
            self.receipt["diagnostics_skipped"] = "container ownership could not be verified"
            return
        # Keep the established ADB evidence as well as the container fallback.
        adb_probes = (
            ("boot_completed", ("shell", "getprop", "sys.boot_completed")),
            ("last_anr", ("shell", "dumpsys", "activity", "lastanr")),
            ("input", ("shell", "dumpsys", "input")),
            ("windows", ("shell", "dumpsys", "window", "-a", "windows")),
            ("logcat", ("shell", "logcat", "-d", "-t", "300")),
        )
        deadline = started + budget_seconds
        # Retain container fallback evidence even with a shortened recovery
        # budget, while reserving up to half the remaining time for ADB.
        available = max(0.0, deadline - time.monotonic())
        reserved_adb = min(5.0 * len(adb_probes), available / 2)
        container_deadline = deadline - reserved_adb
        probes = [(name, [self.docker_path or "docker", "exec", self.container_name, *arguments],
                   container_deadline)
                  for name, arguments in container_probes]
        probes.extend((name, [self.adb_path or "adb", "-s", self.serial, *arguments], deadline)
                      for name, arguments in adb_probes)
        diagnostics = {}
        for name, command, phase_deadline in probes:
            remaining = min(deadline, phase_deadline) - time.monotonic()
            if remaining <= 0:
                diagnostics[name] = {"unavailable": "diagnostic collection budget exhausted"}
                continue
            try:
                result = self._command(command,
                                       timeout=min(5.0, remaining), check=False, use_deadline=False)
                diagnostics[name] = {"exit_code": result.returncode,
                                     "output": _bounded(result.stdout + result.stderr)}
            except Exception as exc:
                diagnostics[name] = {"unavailable": _bounded(str(exc), 1000)}
        self.receipt["diagnostics"] = diagnostics

    def _save_receipt(self, status: str, failure: str | None = None, exit_code: int | None = None) -> None:
        self.receipt.update({"status": status, "elapsed_seconds": round(time.monotonic() - self.started, 3),
                             "finished_at_unix": time.time()})
        if failure:
            self.receipt["failure"] = _bounded(failure, 2000)
        if exit_code is not None:
            self.receipt["exit_code"] = exit_code
        _atomic_json(self.output_dir / "receipt.json", self.receipt)

    def _boot_with_recovery(self) -> None:
        overall_deadline = self.deadline
        history = self.receipt.setdefault("startup_attempts", [])
        try:
            for number in range(1, self.startup_attempts + 1):
                # Reserve time for remaining attempts without extending the
                # original startup deadline. Diagnostics/cleanup have own bounds.
                remaining = overall_deadline - time.monotonic()
                self.deadline = time.monotonic() + max(0, remaining) / (self.startup_attempts - number + 1)
                attempt = {"attempt": number, "run_id": self.run_id, "status": "starting"}
                history.append(attempt)
                started = time.monotonic()
                try:
                    self.receipt["startup_phase"] = "container_start"
                    self._launch_container()
                    self.receipt["setup_timings_seconds"]["container_start"] = round(time.monotonic() - started, 3)
                    self.receipt["startup_phase"] = "android_readiness"
                    ready_started = time.monotonic()
                    self._wait_ready()
                    self.receipt["setup_timings_seconds"]["readiness"] = round(time.monotonic() - ready_started, 3)
                    attempt["status"] = "ready"
                    return
                except RedroidError as exc:
                    transient = (isinstance(exc.__cause__, subprocess.TimeoutExpired) or
                                 str(exc) == f"operation exceeded {self.timeout}s timeout")
                    attempt.update(status="retryable_failure" if transient else "permanent_failure",
                                   phase=self.receipt["startup_phase"], failure=_bounded(str(exc), 2000))
                    if not transient or number == self.startup_attempts:
                        raise
                    # Cleanup can consume 25 seconds (ownership, disconnect,
                    # logs and removal). Bound intermediate evidence collection
                    # so it cannot spend the remaining attempts' share as well.
                    remaining = max(0.0, overall_deadline - time.monotonic())
                    recovery_share = remaining / (self.startup_attempts - number + 1)
                    diagnostic_budget = min(45.0, max(0.0, recovery_share - 25.0))
                    attempt["diagnostic_budget_seconds"] = round(diagnostic_budget, 3)
                    self._collect_diagnostics(budget_seconds=diagnostic_budget)
                    for key in ("diagnostics", "diagnostics_skipped"):
                        if key in self.receipt:
                            attempt[key] = self.receipt.pop(key)
                    self._cleanup()
                    errors = ("cleanup_error", "cleanup_refused", "adb_cleanup_error")
                    if any(key in self.receipt for key in errors):
                        raise RedroidError("startup recovery refused: cleanup was not verified") from exc
                    if self.docker_log.exists():
                        self.docker_log.replace(self.output_dir / f"container-attempt-{number}.log")
                    self.container_started = self.adb_connected = False
                    self.run_id = str(uuid.uuid4())
                    self.container_name = f"ashnazg-redroid-{self.run_id}"
                    self.receipt["run_id"] = self.run_id
                    self.receipt.pop("container_id", None)
                    self.receipt.pop("last_readiness", None)
                    self.deadline = overall_deadline
                    self._remaining()
                    time.sleep(min(0.25 * number, self._remaining()))
                    self.receipt["startup_phase"] = "endpoint"
                    self._check_endpoint()
                finally:
                    attempt["elapsed_seconds"] = round(time.monotonic() - started, 3)
                    try:
                        _atomic_json(self.output_dir / f"startup-attempt-{number}.json", attempt)
                    except OSError as diagnostic_error:
                        self.receipt["attempt_record_error"] = _bounded(str(diagnostic_error), 1200)
        finally:
            self.deadline = overall_deadline

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
            self.receipt["startup_phase"] = "preflight"
            self._preflight()
            self.receipt["setup_timings_seconds"]["preflight"] = round(time.monotonic() - started, 3)
            started = time.monotonic()
            self.receipt["startup_phase"] = "storage"
            self._prepare_storage()
            if self.storage:
                self.receipt["startup_phase"] = "endpoint"
                self._check_endpoint()
            self._boot_with_recovery()
            self._sample_resources("before_command")
            self.receipt["startup_phase"] = "host_command"
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
            self.receipt["failed_phase"] = self.receipt.get("startup_phase")
            status = "timeout" if "timeout" in failure.lower() or "exceeded" in failure.lower() else "failure"
            code = 1
        finally:
            try:
                if status != "success":
                    self._collect_diagnostics()
            finally:
                try:
                    self._cleanup()
                finally:
                    if self.storage:
                        self.storage.close()
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
    run.add_argument("--startup-attempts", type=int, choices=(1, 2, 3), default=1,
                     help="bounded attempts before host command; share the original startup deadline")
    run.add_argument("--binder-bootstrap", type=Path, help="trusted static private Binderfs launcher")
    run.add_argument("--state-dir", type=Path, help="retain Android data; recover exclusively owned stale containers")
    run.add_argument("--fps", type=int, choices=(30, 60), help="explicit upstream display cadence")
    run.add_argument("--adb-port", type=int, default=5555)
    run.add_argument("--timeout", type=int, default=180)
    run.add_argument("--command-timeout", type=int, default=2400,
                     help="command deadline in seconds; 0 explicitly allows a persistent service")
    run.add_argument("--image-provenance", type=Path,
                     help="opt in to a locally built image with a verified provenance descriptor")
    run.add_argument("--image-profile", choices=tuple(IMAGE_PROFILES), default="default",
                     help="select a known digest-pinned upstream image profile")
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
                                 timeout=args.timeout, command_timeout=args.command_timeout,
                                 image_provenance=args.image_provenance,
                                 image_profile=args.image_profile, state_dir=args.state_dir, fps=args.fps,
                                 startup_attempts=args.startup_attempts, binder_bootstrap=args.binder_bootstrap).run(command)
    except RedroidError as exc:
        print(f"redroid: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
