#!/usr/bin/env python3
"""Ubuntu x86_64 everyday Android agent development session."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import uuid
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADB_PORT = 5555


class DevSessionError(RuntimeError):
    pass


def package_artifacts(root: Path) -> tuple[Path, Path, Path]:
    names = ("bin/ashnazg", "android/agent-instrumentation.apk", "android/fixture-instrumentation.apk")
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest["name"] != "ashnazg" or manifest["platform"] != "linux-x86_64":
            raise DevSessionError("expected an Ashnazg Linux x86_64 release package")
        for name in names:
            entry = manifest["files"][name]
            with (root / name).open("rb") as handle:
                if os.fstat(handle.fileno()).st_size != entry["size"] or (
                    hashlib.sha256(handle.read()).hexdigest() != entry["sha256"]
                ):
                    raise DevSessionError(f"release package integrity check failed for {name}")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise DevSessionError("cannot verify release package manifest and files") from exc
    binary, agent, fixture = (root / name for name in names)
    if not os.access(binary, os.X_OK):
        raise DevSessionError("release binary is not executable")
    return binary, agent, fixture


def validate_prerequisites(*, packaged: bool = False, guest_binder: bool = False) -> dict[str, str]:
    if platform.system() != "Linux":
        raise DevSessionError("development session requires a Linux host")
    if platform.machine() not in ("x86_64", "amd64"):
        raise DevSessionError("development session requires Ubuntu x86_64")
    found = {}
    for name in (("docker", "adb") if packaged else ("docker", "adb", "java")):
        path = shutil.which(name)
        if not path:
            raise DevSessionError(f"{name} is required; install it yourself")
        found[name] = path
    if not guest_binder and not Path("/dev/binder").exists():
        raise DevSessionError("/dev/binder is unavailable; configure the host manually")
    if not packaged and not (ROOT / "agent-android/gradlew").is_file():
        raise DevSessionError("agent-android Gradle wrapper is missing")
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not packaged and (not sdk or not Path(sdk).is_dir()):
        raise DevSessionError("set ANDROID_HOME to the installed Android SDK directory")
    try:
        for arguments in ((("info",),) if packaged else (("info",), ("buildx", "version"))):
            subprocess.run([found["docker"], *arguments], check=True, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        dependency = "Docker daemon" if packaged else "Docker daemon and Docker Buildx"
        raise DevSessionError(f"{dependency} must be available") from exc
    return {"architecture": "amd64", **({"android_sdk": sdk} if not packaged else {}), **found}


def redroid_command(args, output_dir: str) -> list[str]:
    tail = args.command[1:] if args.command[:1] == ["--"] else args.command
    return [sys.executable, str(ROOT / "scripts/redroid.py"), "run", "--output-dir", output_dir,
            "--adb-port", str(args.adb_port), "--timeout", str(args.timeout),
            *(["--state-dir", str(args.state_dir)] if args.state_dir else []),
            *(["--binder-bootstrap", str(args.binder_bootstrap)] if args.binder_bootstrap else []),
            "--startup-attempts", str(args.startup_attempts), "--fps", "60", "--command-timeout", "0", "--image-profile", "android15-64only", "--", sys.executable, str(Path(__file__).resolve()),
            "_inside", "--adb-port", str(args.adb_port),
            *(["--release-dir", str(args.release_dir)] if args.release_dir else []),
            "--", *([args.host_command] if args.host_command else []), *(tail or (["mcp"] if args.release_dir else []))]


def _inside(port: int, host_command: list[str], release_dir: Path | None = None) -> int:
    if __package__:
        from .harness import DeviceHarness
    else:
        from harness import DeviceHarness
    env = os.environ.copy()
    env["ANDROID_SERIAL"] = f"127.0.0.1:{port}"
    os.environ.update(env)
    if release_dir:
        binary, agent, fixture = package_artifacts(release_dir)
        host_command = [str(binary), *host_command]
    else:
        build = subprocess.run([str(ROOT / "agent-android/gradlew"), ":agent:assembleDebug", ":fixture:assembleDebug"],
                               cwd=ROOT / "agent-android", check=False, stdout=sys.stderr, stdin=subprocess.DEVNULL)
        if build.returncode:
            return build.returncode
        agent = ROOT / "agent-android/agent/build/outputs/apk/debug/agent-debug.apk"
        fixture = ROOT / "agent-android/fixture/build/outputs/apk/debug/fixture-debug.apk"
    harness = DeviceHarness(local_port=0)
    child = None
    previous_handlers = {}
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, stop)
    try:
        harness.start(agent, fixture)
        child_env = os.environ.copy()
        child_env.update(ANDROID_SERIAL=harness.serial, ASHNAZG_TOKEN=harness.token,
                         ASHNAZG_AGENT_ADDR=harness.agent_addr)
        child = subprocess.Popen(host_command, env=child_env)
        return child.wait()
    finally:
        # The outer supervisor gives this process group a short grace period.
        # Remove the owned host forward first; container removal stops guest work.
        if harness.forward_created:
            try:
                harness.close_forward(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        harness.cleanup()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("check", help="validate installed host prerequisites")
    start = sub.add_parser("start", help="start ReDroid and run host command until stopped")
    start.add_argument("--startup-attempts", type=int, choices=(1, 2, 3), default=2)
    start.add_argument("--state-dir", type=Path, help="private directory retaining app data across invocations")
    start.add_argument("--adb-port", type=int, default=ADB_PORT)
    start.add_argument("--timeout", type=int, default=180)
    launch = start.add_mutually_exclusive_group(required=True)
    launch.add_argument("--host-command")
    launch.add_argument("--release-dir", type=Path)
    start.add_argument("--binder-bootstrap", type=Path)
    start.add_argument("command", nargs=argparse.REMAINDER)
    inside = sub.add_parser("_inside")
    inside.add_argument("--adb-port", type=int, required=True)
    inside.add_argument("--release-dir", type=Path)
    inside.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def run_supervisor(command):
    child = subprocess.Popen(command, start_new_session=True)
    previous = {}
    def stop(signum, frame):
        if child.poll() is None:
            child.send_signal(signum)
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
        return child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "check":
            for key, value in validate_prerequisites().items():
                print(f"{key}={value}")
            return 0
        if args.action == "_inside":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            if not command:
                raise DevSessionError("host command is required")
            return _inside(args.adb_port, command, args.release_dir)
        if not 1 <= args.adb_port <= 65535 or not 1 <= args.timeout <= 3600:
            parser.error("port must be 1..65535 and timeout 1..3600")
        if args.host_command is not None and not args.host_command.strip():
            parser.error("--host-command cannot be empty")
        args.command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if args.release_dir:
            args.release_dir = args.release_dir.resolve()
            package_artifacts(args.release_dir)
        if args.binder_bootstrap:
            args.binder_bootstrap = args.binder_bootstrap.resolve()
        validate_prerequisites(packaged=bool(args.release_dir), guest_binder=bool(args.binder_bootstrap))
        return run_supervisor(redroid_command(args, str(ROOT / "artifacts/development" / str(uuid.uuid4()))))
    except (DevSessionError, OSError, subprocess.SubprocessError) as exc:
        print(f"dev-session: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
