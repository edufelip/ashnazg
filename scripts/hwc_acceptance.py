#!/usr/bin/env python3
"""Qualify an installed source-owned HWC2 module on an explicit Android device."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


MARKER = "ASHNAZG_HWC_TELEMETRY_V1 "
ALLOWED_PATHS = {
    "/vendor/lib64/hw/hwcomposer.redroid.so",
    "/system/lib64/hw/hwcomposer.redroid.so",
}
MAX_EVIDENCE_CHARS = 32_768
MAX_TELEMETRY_EVENTS = 256


class AcceptanceError(RuntimeError):
    pass


def require_serial(serial: str | None) -> str:
    value = serial or os.environ.get("ANDROID_SERIAL")
    if not value or not value.strip():
        raise AcceptanceError("set ANDROID_SERIAL explicitly or pass --serial")
    return value.strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_provenance(path: Path) -> dict[str, Any]:
    try:
        descriptor = json.loads(Path(path).read_text(encoding="utf-8"))
        image = descriptor["image_inspect"]
        labels = image["config"]["labels"]
        digest = labels["org.ashnazg.hwc-sha256"]
        raw_paths = labels["org.ashnazg.hwc-paths"]
        image_id = image["id"]
        paths = json.loads(raw_paths)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AcceptanceError(f"invalid HWC image provenance: {exc}") from exc
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise AcceptanceError("provenance image ID is missing or malformed")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AcceptanceError("provenance HWC SHA-256 label is missing or malformed")
    if (not isinstance(paths, list) or not paths or
            any(not isinstance(item, str) or item not in ALLOWED_PATHS for item in paths) or
            len(paths) != len(set(paths))):
        raise AcceptanceError("provenance contains an unsupported HWC path")
    return {"image_id": image_id, "sha256": digest, "paths": paths}


def bounded_text(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    if len(text) <= limit:
        return text
    suffix = "[truncated]"
    return text[:max(0, limit - len(suffix))] + suffix


def bounded_surfaceflinger(text: str, limit: int = MAX_EVIDENCE_CHARS) -> str:
    if len(text) <= limit:
        return text
    marker_position = text.find(MARKER)
    suffix = "[truncated]"
    if marker_position < 0:
        return bounded_text(text, limit)
    marker_end = text.find("\n", marker_position)
    marker_line = text[marker_position: marker_end if marker_end >= 0 else len(text)]
    if len(marker_line) + len(suffix) + 1 > limit:
        return bounded_text(text, limit)
    head_length = max(0, limit - len(marker_line) - len(suffix) - 1)
    return text[:head_length] + "\n" + marker_line + suffix


def parse_hwc_dump(text: str) -> dict[str, Any]:
    match = re.search(re.escape(MARKER) + r"(\{[^\r\n]*\})", text)
    if not match:
        raise AcceptanceError("loaded project HWC telemetry marker missing from SurfaceFlinger dump")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise AcceptanceError(f"project HWC telemetry JSON is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise AcceptanceError("project HWC telemetry schema is unsupported")
    config = payload.get("selected_config")
    callbacks = payload.get("callbacks")
    enabled = payload.get("callback_enabled")
    if (not isinstance(config, int) or isinstance(config, bool) or config not in (0, 1) or
            not isinstance(enabled, bool) or not isinstance(callbacks, list)):
        raise AcceptanceError("project HWC telemetry fields are incomplete")
    if len(callbacks) > MAX_TELEMETRY_EVENTS:
        raise AcceptanceError("project HWC telemetry exceeds its fixed event bound")
    for counter in ("callback_total", "present_successes", "present_timeouts"):
        value = payload.get(counter)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AcceptanceError(f"project HWC telemetry counter {counter} is malformed")
    for event in callbacks:
        if (not isinstance(event, dict) or not isinstance(event.get("timestamp_ns"), int) or
                isinstance(event.get("timestamp_ns"), bool) or
                not isinstance(event.get("config_id"), int) or isinstance(event.get("config_id"), bool) or
                event.get("config_id") not in (0, 1) or
                not isinstance(event.get("callback_enabled"), bool) or
                not isinstance(event.get("sequence"), int) or isinstance(event.get("sequence"), bool)):
            raise AcceptanceError("project HWC callback telemetry sample is malformed")
    return payload


def callback_intervals(telemetry: dict[str, Any], *, config_id: int | None = None,
                       after_sequence: int = 0) -> dict[str, Any]:
    samples = telemetry.get("callbacks", [])
    if not telemetry.get("callback_enabled", False):
        return {"status": "unavailable_callbacks_disabled", "intervals_ns": []}
    if not isinstance(samples, list):
        return {"status": "unavailable_too_few_samples", "intervals_ns": []}
    if any(not isinstance(event, dict) for event in samples):
        raise AcceptanceError("malformed callback telemetry sample")
    samples = [event for event in samples
               if event.get("sequence", 0) > after_sequence and
               event.get("callback_enabled", False)]
    if not isinstance(samples, list) or len(samples) < 2:
        return {"status": "unavailable_too_few_samples", "intervals_ns": []}
    normalized = []
    for event in samples:
        if not isinstance(event, dict) or not isinstance(event.get("timestamp_ns"), int):
            raise AcceptanceError("malformed callback telemetry sample")
        if event.get("config_id") not in (0, 1):
            raise AcceptanceError("malformed callback config attribution")
        normalized.append((event["timestamp_ns"], event["config_id"]))
    intervals: list[int] = []
    config_ids: list[int] = []
    for (left, left_config), (right, right_config) in zip(normalized, normalized[1:]):
        if right <= left:
            raise AcceptanceError("callback timestamps are not strictly increasing")
        # A mode transition interval straddles two configurations and cannot be
        # attributed to either period, so retain only same-config intervals.
        if left_config == right_config and (config_id is None or right_config == config_id):
            intervals.append(right - left)
            config_ids.append(right_config)
    if not intervals:
        return {"status": "unavailable_no_same_config_intervals", "intervals_ns": [], "config_ids": []}
    return {"status": "available", "intervals_ns": intervals, "config_ids": config_ids}


def run_adb(serial: str, arguments: list[str], *, check: bool = True,
            deadline: float | None = None) -> str:
    adb = os.environ.get("ASHNAZG_ADB", "adb")
    command_timeout = 30.0
    if deadline is not None:
        command_timeout = min(command_timeout, deadline - time.monotonic())
        if command_timeout <= 0:
            raise AcceptanceError("mode transition exceeded its absolute deadline")
    try:
        result = subprocess.run([adb, "-s", serial, *arguments], check=False,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=command_timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceError(f"adb command failed: {exc}") from exc
    raw_output = result.stdout or ""
    output = (bounded_surfaceflinger(raw_output) if arguments == ["shell", "dumpsys", "SurfaceFlinger"]
              else bounded_text(raw_output))
    if check and result.returncode:
        raise AcceptanceError(f"adb command returned {result.returncode}: {output}")
    return output


def read_hwc_telemetry(serial: str, *, deadline: float | None = None) -> dict[str, Any]:
    return parse_hwc_dump(run_adb(serial, ["shell", "dumpsys", "SurfaceFlinger"], deadline=deadline))


def verify_installed_module(serial: str, provenance: dict[str, Any]) -> dict[str, Any]:
    checked: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="ashnazg-hwc-pull-") as temporary:
        for index, remote in enumerate(provenance["paths"]):
            local = Path(temporary) / f"module-{index}.so"
            run_adb(serial, ["pull", remote, str(local)], check=False)
            if not local.is_file():
                checked.append({"path": remote, "result": "not_pullable"})
                continue
            digest = _sha256_file(local)
            checked.append({"path": remote, "sha256": digest,
                            "result": "match" if digest == provenance["sha256"] else "mismatch"})
            if digest == provenance["sha256"]:
                return {"path": remote, "sha256": digest, "checked": checked}
        raise AcceptanceError(f"no installed HWC module matches provenance: {checked}")


def display_evidence(serial: str) -> dict[str, str]:
    # Bounded command output records framework mode policy and SurfaceFlinger state.
    return {
        "surfaceflinger": run_adb(serial, ["shell", "dumpsys", "SurfaceFlinger"]),
        "display": bounded_text(run_adb(serial, ["shell", "dumpsys", "display"])),
    }


def wait_for_config(serial: str, config: int, *, deadline: float,
                    poll_interval: float = 0.5) -> dict[str, Any]:
    while True:
        if time.monotonic() >= deadline:
            raise AcceptanceError(f"timed out before HWC selected config {config} (deadline)")
        telemetry = read_hwc_telemetry(serial, deadline=deadline)
        if telemetry["selected_config"] == config:
            return telemetry
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AcceptanceError(f"timed out before HWC selected config {config} (deadline)")
        time.sleep(min(poll_interval, remaining))


def _request_mode(serial: str, config: int, *, deadline: float | None = None) -> None:
    rate = "1.0" if config == 0 else "60.0"
    run_adb(serial, ["shell", "cmd", "display", "set-user-preferred-display-mode",
                     "720", "1280", rate], deadline=deadline)


def transition_to_config(serial: str, config: int, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    _request_mode(serial, config, deadline=deadline)
    return wait_for_config(serial, config, deadline=deadline)


def run_acceptance(serial: str, provenance_path: Path, *, timeout: float = 30.0) -> dict[str, Any]:
    provenance = validate_provenance(provenance_path)
    installed = verify_installed_module(serial, provenance)
    initial_telemetry = read_hwc_telemetry(serial)
    receipt: dict[str, Any] = {
        "schema": 1,
        "status": "running",
        "serial": serial,
        "image_id": provenance["image_id"],
        "module": installed,
        "evidence": {"before": display_evidence(serial)},
        "transitions": [],
    }
    primary_error: Exception | None = None
    try:
        for config in (0, 1):
            deadline = time.monotonic() + timeout
            baseline = read_hwc_telemetry(serial, deadline=deadline)
            _request_mode(serial, config, deadline=deadline)
            telemetry = wait_for_config(serial, config, deadline=deadline)
            receipt["transitions"].append({
                "requested_config": config,
                "selected_config": telemetry["selected_config"],
                "telemetry": telemetry,
                "callback_intervals": callback_intervals(
                    telemetry, config_id=config, after_sequence=baseline["callback_total"]),
                "evidence": display_evidence(serial),
            })
    except Exception as exc:
        primary_error = exc
    finally:
        restore: dict[str, Any] = {"requested_config": 1}
        try:
            deadline = time.monotonic() + timeout
            _request_mode(serial, 1, deadline=deadline)
            telemetry = wait_for_config(serial, 1, deadline=deadline)
            restore["selected_config"] = telemetry["selected_config"]
            restore["telemetry"] = telemetry
            receipt["evidence"]["after"] = display_evidence(serial)
        except Exception as exc:
            restore["error"] = str(exc)
            if primary_error is None:
                primary_error = exc
        receipt["restore"] = restore
    receipt["initial_telemetry"] = initial_telemetry
    receipt["status"] = "passed" if primary_error is None else "failed"
    if primary_error is not None:
        receipt["error"] = str(primary_error)
    if primary_error is not None:
        error = AcceptanceError(str(primary_error))
        error.receipt = receipt
        raise error
    return receipt


def write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provenance", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--serial", help="explicit device serial (otherwise ANDROID_SERIAL is required)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="absolute deadline budget in seconds for each mode transition")
    args = parser.parse_args(argv)
    receipt: dict[str, Any]
    try:
        serial = require_serial(args.serial)
        if args.timeout <= 0:
            raise AcceptanceError("--timeout must be positive")
        receipt = run_acceptance(serial, args.provenance, timeout=args.timeout)
    except Exception as exc:
        try:
            # A failed precondition still produces a machine-readable failure receipt.
            receipt = getattr(exc, "receipt", {"schema": 1, "status": "failed", "error": str(exc)})
        except Exception:
            receipt = {"schema": 1, "status": "failed", "error": "acceptance failed"}
        try:
            write_receipt(args.output, receipt)
        except OSError as write_error:
            print(f"failed to write acceptance receipt: {write_error}", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    write_receipt(args.output, receipt)
    print(json.dumps({"status": receipt["status"], "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
