#!/usr/bin/env python3
"""Measure real persistent RPC against real adb command plus transfer round trips."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.harness import DeviceHarness, HarnessError, artifact_apks
from scripts.integration import IntegrationFailure, RpcClient


def percentile(values: list[float], p: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one successful sample")
    if not 0 <= p <= 100:
        raise ValueError("percentile must be between 0 and 100")
    ordered = sorted(float(x) for x in values)
    if any(not (x > 0 and x < float("inf")) for x in ordered):
        raise ValueError("samples must be finite positive durations; errors are never encoded as zero")
    # Nearest-rank convention: rank = ceil(p * n), with the minimum rank 1.
    import math
    return ordered[max(0, math.ceil(p * len(ordered) / 100) - 1)]


def require_samples(samples: list[float], errors: list[str]) -> None:
    if errors:
        raise RuntimeError(f"benchmark has {len(errors)} errors: " + "; ".join(errors[:4]))
    if not samples:
        raise RuntimeError("benchmark produced no successful samples")
    if any(not isinstance(x, (int, float)) or x <= 0 for x in samples):
        raise ValueError("benchmark samples must be positive durations")


def summarize(samples: list[float]) -> dict:
    require_samples(samples, [])
    return {"iterations": len(samples), "p50_ms": percentile(samples, 50),
            "p95_ms": percentile(samples, 95), "mean_ms": statistics.fmean(samples),
            "samples_ms": [float(x) for x in samples]}


def host_binary_metadata(path: Path) -> dict:
    if not path.is_file():
        raise HarnessError(f"host binary target/{path.parent.name}/{path.name} not found")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"basename": path.name, "profile": path.parent.name, "sha256": digest}


def source_metadata(root: Path) -> dict:
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.strip()
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
        return {"head": commit, "dirty": bool(status), "github_sha": os.environ.get("GITHUB_SHA")}
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "dirty": None, "github_sha": os.environ.get("GITHUB_SHA")}


def adb_metadata(adb: str | None) -> dict | None:
    if not adb:
        return None
    try:
        result = subprocess.run([adb, "version"], check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, timeout=10)
        lines = result.stdout.splitlines()[:2]
    except (OSError, subprocess.SubprocessError):
        lines = []
    return {"basename": Path(adb).name, "version_lines": lines}


def load_average() -> list[float] | None:
    try:
        return [round(float(value), 3) for value in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def sanitize_message(message: str, root: Path) -> str:
    resolved_root = str(root.resolve())
    replacements = [os.environ.get("ASHNAZG_ADB"), shutil.which("adb"),
                    os.environ.get("ASHNAZG_BIN"), str(root), resolved_root]
    for raw in replacements:
        if raw:
            replacement = "<workspace>" if raw in (str(root), resolved_root) else Path(raw).name
            message = message.replace(raw, replacement)
    return message


def adb_snapshot_roundtrip(harness: DeviceHarness, workdir: Path) -> bytes:
    remote = "/sdcard/ashnazg-uiautomator-benchmark.xml"
    harness.adb_cmd("shell", "uiautomator", "dump", "--compressed", remote, timeout=90)
    local = workdir / "uiautomator.xml"
    harness.adb_cmd("pull", remote, str(local), timeout=90)
    data = local.read_bytes()
    if not data.strip().startswith(b"<?xml") or b"<hierarchy" not in data:
        raise IntegrationFailure("uiautomator dump plus pull did not produce a hierarchy XML file")
    root = ElementTree.fromstring(data)
    if not any(element.attrib.get("resource-id") == "dev.ashnazg.fixture:id/increment" and
               element.attrib.get("text") == "Count: 0" for element in root.iter()):
        raise IntegrationFailure("uiautomator XML did not show the native fixture Count: 0 button")
    return data


def assert_fixture_count(snapshot: dict, expected_count: int, context: str) -> dict:
    nodes = snapshot.get("nodes")
    if not isinstance(nodes, list):
        raise IntegrationFailure(f"{context} returned no semantic nodes list")
    counter = [node for node in nodes if node.get("testId") == "dev.ashnazg.fixture:id/increment"]
    if len(counter) != 1:
        raise IntegrationFailure(f"{context} snapshot is not the native fixture")
    expected_label = f"Count: {expected_count}"
    if counter[0].get("text") != expected_label:
        raise IntegrationFailure(f"{context} increment button did not show fixture {expected_label}")
    return snapshot


def assert_fixture_snapshot(snapshot: dict, context: str) -> dict:
    return assert_fixture_count(snapshot, 0, context)


def assert_action_result(result: dict, expected_count: int, context: str) -> dict:
    if result.get("settled") is not True:
        raise IntegrationFailure(f"{context} action result is not settled")
    snapshot = result.get("snapshot")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("snapshotId"), int):
        raise IntegrationFailure(f"{context} action result omitted its fresh snapshot")
    assert_fixture_count(snapshot, expected_count, context)
    return result


def mcp_snapshot_payload(result: dict) -> dict:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content", []):
        if item.get("type") == "text":
            try:
                value = json.loads(item.get("text", ""))
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
    raise IntegrationFailure("MCP snapshot did not return structured semantic data")


def mcp_action_payload(result: dict) -> dict:
    return mcp_snapshot_payload(result)


def guest_screenshot(client: RpcClient, fmt: str) -> bytes:
    result = client.call("ui_screenshot", {"format": fmt, "somOverlay": False})
    mime = "image/png" if fmt == "png" else "image/webp"
    signature = b"\x89PNG\r\n\x1a\n" if fmt == "png" else b"RIFF"
    data = base64.b64decode(result.get("data", ""), validate=True)
    if result.get("mimeType") != mime or not data.startswith(signature):
        raise IntegrationFailure(f"persistent guest screenshot did not match requested {mime}")
    return data


def adb_screencap_roundtrip(harness: DeviceHarness) -> bytes:
    # Use binary stdout so the measured call includes the real ADB image transfer.
    proc = subprocess.run([harness.adb, "-s", harness.serial, "exec-out", "screencap", "-p"],
                          check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    data = proc.stdout
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise IntegrationFailure("adb screencap did not return PNG bytes")
    return data


class McpProcess:
    """One long-lived stdio MCP host process, initialized once for all timed calls."""
    def __init__(self, binary: str, harness: DeviceHarness):
        env = os.environ.copy()
        env.update({"ASHNAZG_TOKEN": harness.token, "ASHNAZG_AGENT_ADDR": "127.0.0.1:7777",
                    "ANDROID_SERIAL": harness.serial})
        self.process = subprocess.Popen([binary, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
        self.request_id = 1
        self.started_at = time.perf_counter()
        try:
            initialized = self.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                       "clientInfo": {"name": "ashnazg-benchmark", "version": "0.1"}})
            if not initialized.get("result", {}).get("protocolVersion"):
                raise IntegrationFailure(f"MCP initialization failed: {initialized}")
            self.notify("notifications/initialized")
            tools = self.request("tools/list", {})
            names = {item.get("name") for item in tools.get("result", {}).get("tools", [])}
            if not {"ui_snapshot", "ui_screenshot"}.issubset(names):
                raise IntegrationFailure(f"MCP tools missing from benchmark host: {sorted(names)}")
            self.initialization_ms = (time.perf_counter() - self.started_at) * 1000
        except Exception:
            self.close()
            raise

    def read(self) -> dict:
        from scripts.integration import read_mcp_line
        return read_mcp_line(self.process)

    def request(self, method: str, params: dict) -> dict:
        if self.process.stdin is None:
            raise IntegrationFailure("MCP stdin is unavailable")
        request_id = self.request_id
        self.request_id += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                             "method": method, "params": params}, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        response = self.read()
        if response.get("id") != request_id or "error" in response:
            raise IntegrationFailure(f"MCP request {method} failed or lost correlation: {response}")
        return response

    def notify(self, method: str) -> None:
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def call_tool(self, name: str, arguments: dict) -> dict:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        result = response.get("result", {})
        if result.get("isError"):
            raise IntegrationFailure(f"MCP tool {name} returned isError: {result}")
        return result

    def screenshot(self, fmt: str) -> bytes:
        result = self.call_tool("ui_screenshot", {"format": fmt, "somOverlay": False})
        expected = "image/png" if fmt == "png" else "image/webp"
        images = [entry for entry in result.get("content", [])
                  if entry.get("type") == "image" and entry.get("mimeType") == expected]
        if not images:
            raise IntegrationFailure(f"MCP screenshot lacked {expected} image content")
        data = __import__("base64").b64decode(images[0]["data"], validate=True)
        signature = b"\x89PNG\r\n\x1a\n" if fmt == "png" else b"RIFF"
        if not data.startswith(signature):
            raise IntegrationFailure(f"MCP screenshot bytes did not match {expected}")
        return data

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def measure(operation, iterations: int, root: Path) -> tuple[list[float], list[str]]:
    samples: list[float] = []
    errors: list[str] = []
    for index in range(iterations):
        start = time.perf_counter_ns()
        try:
            operation()
            elapsed = (time.perf_counter_ns() - start) / 1_000_000
            if elapsed <= 0:
                raise RuntimeError("timer returned a non-positive duration")
            samples.append(elapsed)
        except Exception as exc:  # Record error evidence, then fail the run after writing artifact.
            errors.append(f"iteration {index + 1}: {type(exc).__name__}: {sanitize_message(str(exc), root)}")
    return samples, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--output", type=Path, default=Path("artifacts/benchmark.json"))
    parser.add_argument("--readiness-timeout", type=float, default=45.0)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")

    harness = None
    binary = os.environ.get("ASHNAZG_BIN") or str(args.root / "target/release/ashnazg")
    binary_metadata = host_binary_metadata(Path(binary)) if Path(binary).is_file() else {
        "basename": Path(binary).name, "profile": Path(binary).parent.name, "sha256": None}
    artifact = {"schema_version": 1, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "iterations_requested": args.iterations,
                "environment": {"platform": platform.platform(), "python": platform.python_version(),
                                "adb": adb_metadata(os.environ.get("ASHNAZG_ADB") or shutil.which("adb")),
                                "host_binary": binary_metadata, "source": source_metadata(args.root),
                                "load_average_before": load_average(),
                                "android_serial_source": "ANDROID_SERIAL" if os.environ.get("ANDROID_SERIAL") else "sole online emulator"},
                "device": {}, "setup": {}, "benchmarks": {}, "errors": []}
    exit_code = 0
    client = None
    mcp = None
    try:
        if not Path(binary).is_file():
            raise HarnessError(f"host binary not found: {binary}; build it or set ASHNAZG_BIN")
        harness = DeviceHarness(args.readiness_timeout)
        artifact["environment"]["android_serial"] = harness.serial
        for prop in ("ro.build.version.sdk", "ro.product.model", "ro.product.cpu.abi"):
            artifact["device"][prop] = harness.adb_cmd("shell", "getprop", prop, timeout=20).stdout.strip()
        artifact["device"]["screen_size"] = harness.adb_cmd("shell", "wm", "size", timeout=20).stdout.strip()
        artifact["device"]["animation_scales"] = {
            key: harness.adb_cmd("shell", "settings", "get", "global", key, timeout=20).stdout.strip()
            for key in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")
        }
        harness.assert_available()
        agent, fixture = artifact_apks(args.root)
        started = time.perf_counter()
        harness.install(agent, fixture)
        harness.adb_cmd("shell", "pm", "clear", "dev.ashnazg.fixture", timeout=30)
        harness.adb_cmd("shell", "am", "start", "-W", "-n", "dev.ashnazg.fixture/.MainActivity", timeout=60)
        artifact["setup"]["apk_install_and_fixture_launch_ms"] = (time.perf_counter() - started) * 1000
        with tempfile.TemporaryDirectory(prefix="ashnazg-bench-") as temporary:
            workdir = Path(temporary)
            # Run one-shot UiAutomator baseline before the persistent instrumentation claims UiAutomation.
            operations = {
                "snapshot_adb_uiautomator_dump_pull": lambda: adb_snapshot_roundtrip(harness, workdir),
                "screenshot_adb_exec_out_screencap_png": lambda: adb_screencap_roundtrip(harness),
            }
            for name, operation in operations.items():
                samples, errors = measure(operation, args.iterations, args.root)
                entry = {"errors": errors, "successes": len(samples), "iterations_requested": args.iterations}
                if samples:
                    entry.update(summarize(samples))
                artifact["benchmarks"][name] = entry
                if errors or len(samples) != args.iterations:
                    artifact["errors"].extend([f"{name}: {error}" for error in errors] or
                                              [f"{name}: incomplete samples ({len(samples)}/{args.iterations})"])

        harness.start(agent, fixture, install=False)
        artifact["setup"]["agent_instrumentation_ready_ms"] = harness.cold_setup_ms
        artifact["setup"]["ready_includes"] = ["adb forward", "instrumentation startup", "authenticated ping readiness"]
        harness.adb_cmd("shell", "am", "start", "-W", "-n", "dev.ashnazg.fixture/.MainActivity", timeout=60)
        client = RpcClient(harness.token)
        client.call("ping")
        assert_fixture_snapshot(client.call("ui_snapshot"), "persistent guest RPC")
        mcp = McpProcess(binary, harness)
        assert_fixture_snapshot(mcp_snapshot_payload(mcp.call_tool("ui_snapshot", {})), "MCP stdio")
        artifact["setup"]["mcp_stdio_initialize_and_tools_list_ms"] = mcp.initialization_ms
        operations = {
            "snapshot_persistent_tcp": lambda: assert_fixture_snapshot(client.call("ui_snapshot"), "persistent guest RPC"),
            "snapshot_mcp_stdio": lambda: assert_fixture_snapshot(mcp_snapshot_payload(mcp.call_tool("ui_snapshot", {})), "MCP stdio"),
            "screenshot_persistent_tcp_png": lambda: guest_screenshot(client, "png"),
            "screenshot_persistent_tcp_webp": lambda: guest_screenshot(client, "webp"),
            "screenshot_mcp_stdio_png": lambda: mcp.screenshot("png"),
            "screenshot_mcp_stdio_webp": lambda: mcp.screenshot("webp"),
        }
        for name, operation in operations.items():
            samples, errors = measure(operation, args.iterations, args.root)
            entry = {"errors": errors, "successes": len(samples), "iterations_requested": args.iterations}
            if samples:
                entry.update(summarize(samples))
            artifact["benchmarks"][name] = entry
            if errors or len(samples) != args.iterations:
                artifact["errors"].extend([f"{name}: {error}" for error in errors] or
                                          [f"{name}: incomplete samples ({len(samples)}/{args.iterations})"])

        def action_series(name: str, operation, starting_count: int) -> bool:
            samples = []
            errors = []
            for offset in range(1, args.iterations + 1):
                expected_count = starting_count + offset
                measured, failures = measure(lambda: assert_action_result(
                    operation(), expected_count, name), 1, args.root)
                samples.extend(measured)
                errors.extend(failures)
                if failures:
                    # The mutation might have reached the guest before any transport failure.
                    # Stop so a later sample cannot silently change that outcome.
                    break
            entry = {"errors": errors, "successes": len(samples), "iterations_requested": args.iterations}
            if samples:
                entry.update(summarize(samples))
            artifact["benchmarks"][name] = entry
            if errors or len(samples) != args.iterations:
                artifact["errors"].extend([f"{name}: {error}" for error in errors] or
                                          [f"{name}: incomplete samples ({len(samples)}/{args.iterations})"])
            return not errors and len(samples) == args.iterations

        # These action series intentionally follow every warm read/image series. Each
        # mutating RPC is sent once, and its settlement wait plus fresh count snapshot
        # remains inside the measured round trip.
        tcp_actions_ok = action_series("action_tap_settled_persistent_tcp", lambda: client.call(
            "ui_act", {"action": "tap", "target": {"testId": "dev.ashnazg.fixture:id/increment"}}), 0)
        if tcp_actions_ok:
            action_series("action_tap_settled_mcp_stdio", lambda: mcp_action_payload(mcp.call_tool(
                "ui_act", {"action": "tap", "target": {"testId": "dev.ashnazg.fixture:id/increment"}})), args.iterations)
        else:
            name = "action_tap_settled_mcp_stdio"
            artifact["benchmarks"][name] = {"errors": ["not run because persistent TCP action series did not complete"],
                                            "successes": 0, "iterations_requested": args.iterations}
            artifact["errors"].append(f"{name}: {artifact['benchmarks'][name]['errors'][0]}")
        if artifact["errors"]:
            exit_code = 1
    except (HarnessError, IntegrationFailure, OSError, subprocess.SubprocessError, RuntimeError, ValueError, KeyError) as exc:
        artifact["errors"].append(f"setup: {type(exc).__name__}: {sanitize_message(str(exc), args.root)}")
        exit_code = 1
    finally:
        artifact["environment"]["load_average_after"] = load_average()
        if mcp is not None:
            mcp.close()
        if client is not None:
            client.close()
        if harness is not None:
            harness.cleanup()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(artifact, indent=2) + "\n")
    print(json.dumps(artifact, indent=2))
    if exit_code:
        print("benchmark failed; see errors above (failed samples are never represented as zero)", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
