"""Measure the iOS automation comparison table on one owned simulator.

macOS execution, Linux-importable pure logic. Implements the run contract in
docs/research/ios-mcp-comparison.md: cold observations start with a newly
created simulator, warm observations shut down and restart the same simulator
with its installed app and retained data, and both conditions are reported
separately. Each observation times the runtime operation to the first usable
hierarchy (expected app identifier, enabled target, valid bounds) and one
observed-element action through the expected semantic change plus a newly
captured PNG. Per-process memory samples the app, simulator and automation
helper separately at the same lifecycle stages in every arm through
proc_pid_rusage with RUSAGE_INFO_V1, verifying ri_proc_start_abstime identity
on each read; lost or changed processes are recorded as missing samples, never
zeros, and shared CoreSimulator services are reported separately with their
scope. A failed operation keeps its elapsed time and failure record and has
no success timing. Tool discovery and arm bring-up are recorded separately
from the table rows. Receipts are JSON files with explicit scope fields. The prebuilt app's
identity is the pinned hashApp tree digest the Rust ios-app staging and the
Ashnazg bridge descriptor both report, so every arm compares the same bytes.
"""

import argparse
import base64
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from scripts.native_cli_acceptance import AcceptanceError, StdioClient, sanitize
from scripts.xcode_mcp_inventory import InventoryError, list_tools

RUSAGE_INFO_V1 = 1
# proc_pid_rusage writes the flavor struct the kernel defines; only the fixed
# header offsets (identical across v0-v4) are parsed, and the buffer is sized
# well past any published struct to stay robust to trailing-field widths.
RUSAGE_BUFFER_BYTES = 512
RUSAGE_MEMORY_OFFSET = 16 + 5 * 8  # UUID and CPU/wakeup/paging counters
RUSAGE_MIN_BYTES = RUSAGE_MEMORY_OFFSET + 4 * 8
ROLE_APP = "app"
ROLE_SIMULATOR = "simulator"
ROLE_HELPER = "helper"
MEMORY_STAGES = ("post_arm_start", "first_snapshot_boundary", "tap_boundary", "post_shutdown")
DEFAULT_FIRST_SNAPSHOT_TIMEOUT = 240.0
CAPTURE_LIMIT = 16 * 1024 * 1024
# Shared CoreSimulator host services are matched by executable name so that
# app and launchd_sim paths inside the CoreSimulator device tree never match.
SHARED_SERVICE_PATTERN = re.compile(
    r"(?:^|[\s/])(CoreSimulatorService|SimStreamProcessor\w*)(?=$|[\s/])")
DISABLED_TOKEN = re.compile(r"\b(?:enabled|isEnabled|hittable)\s*[:=]\s*(?:false|no|0)\b", re.I)
FRAME_PATTERN = re.compile(r"\{\{([0-9.]+),\s*([0-9.]+)\},\s*\{([0-9.]+),\s*([0-9.]+)\}\}")
POINT_PATTERN = re.compile(r"\b(?:hitPoint|center)\s*:\s*\{([0-9.]+),\s*([0-9.]+)\}")
TAP_BOUNDARIES = (
    "submit one action targeting the observed element to the expected semantic change "
    "plus a newly captured PNG showing the change (Apple: through its final validated "
    "combining frame; Ashnazg: tap plus its subsequent screenshot through the "
    "equivalent frame; direct XCTest: native tap through the observed count change "
    "and new app PNG, measured in the fixture runner)"
)
# The Apple bridge must advertise the whole DeviceInteraction surface before any
# session; a bridge whose enablement is invalid never starts an observation.
APPLE_REQUIRED_TOOLS = (
    "DeviceInteractionStartSession",
    "DeviceInteractionSynthesize",
    "DeviceInteractionEndSession",
)
# The pinned ios-app bridge advertises exactly this tool set (verified in
# platform/ios/verify_mcp_app.py); anything else is an unknown bridge.
ASHNAZG_REQUIRED_TOOLS = frozenset(("ui_snapshot", "ui_act", "ui_screenshot"))
DIRECT_XCTEST_TAP_QUALIFICATION = (
    "a runner-reported boundary-scoped duration for the analogous change plus its own "
    "fresh capture (a post-action PNG distinct from its pre-action capture), reported "
    "by the runner itself; whole-test-case success and loosely declared PNG "
    "directories never qualify"
)


class BenchmarkError(RuntimeError):
    pass


class ProcessGone(RuntimeError):
    """The sampled PID no longer exists (ESRCH from proc_pid_rusage)."""


MEER_LOGIN_EMAIL = "login-email-input"
MEER_LOGIN_PASSWORD = "login-password-input"
MEER_LOGIN_SUBMIT = "login-submit-button"
MEER_HOME_SEARCH = "home-search-button"
MEER_SEARCH_INPUT = "search-input"
MEER_SEARCH_BACK = "search-back-button"
MEER_MASK_PATTERN = re.compile(r"^[•●*]+$")
MEER_SYSTEM_PROMPT_LABELS = (
    "Not Now",
    "Allow While Using App",
    "Don’t Allow",
    "Don't Allow",
)
MAX_CREDENTIAL_CHARS = 256


def load_mock_credentials(path):
    """Read synthetic mock credentials in memory only; never log or retain them."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BenchmarkError(f"failed to read mock credentials: {error}") from error
    if not isinstance(payload, dict):
        raise BenchmarkError("mock credentials must be a JSON object")
    email = payload.get("email")
    password = payload.get("password")
    if (not isinstance(email, str) or not isinstance(password, str)
            or not email or not password
            or len(email) > MAX_CREDENTIAL_CHARS
            or len(password) > MAX_CREDENTIAL_CHARS):
        raise BenchmarkError("mock credentials must carry nonempty bounded email and password")
    return email, password


def validate_loopback_http_url(raw):
    """Allow only explicit http(s) URLs bound to loopback hosts."""
    if not isinstance(raw, str) or not raw:
        raise BenchmarkError("--initial-url must be a nonempty loopback http(s) URL")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in ("http", "https") or host not in ("127.0.0.1", "localhost", "::1"):
        raise BenchmarkError("--initial-url must use http(s) on a loopback host")
    return raw


def scrub_secrets(value, secrets=()):
    """Recursively sanitize dicts/lists/strings and redact synthetic credentials."""
    clean = sanitize(value)
    active = tuple(secret for secret in secrets if isinstance(secret, str) and secret)
    if not active:
        return clean

    def _scrub(item):
        if isinstance(item, dict):
            return {
                _scrub(str(key)) if any(s in str(key) for s in active) else str(key): _scrub(val)
                for key, val in item.items()
            }
        if isinstance(item, list):
            return [_scrub(elem) for elem in item]
        if isinstance(item, str):
            text = item
            for secret in active:
                if secret in text:
                    text = text.replace(secret, "[redacted]")
            return text
        return item

    return _scrub(clean)


def assert_no_secrets(value, secrets=(), label="artifact"):
    """Fail closed if a retained payload contains synthetic credential literals."""
    active = tuple(secret for secret in secrets if isinstance(secret, str) and secret)
    if not active:
        return
    serialized = json.dumps(value, sort_keys=True) if not isinstance(value, str) else value
    for secret in active:
        if secret in serialized:
            raise BenchmarkError(f"synthetic mock credential would leak into {label}; discarded")


# ---------------------------------------------------------------------------
# Pure logic: rusage parsing and memory-record normalization.
# ---------------------------------------------------------------------------


def parse_rusage_buffer(data):
    """Parse the fixed RUSAGE_INFO_V1 header fields from a raw buffer."""
    if len(data) < RUSAGE_MIN_BYTES:
        raise ValueError("rusage buffer is too short for the V1 header fields")
    wired, resident, footprint, start_abstime = struct.unpack_from("<4Q", data, RUSAGE_MEMORY_OFFSET)
    return {"ri_wired_size": wired, "ri_resident_size": resident,
            "ri_phys_footprint": footprint, "ri_proc_start_abstime": start_abstime}


def normalize_memory_sample(role, pid, expected_abstime, reading, error_kind=None):
    """Normalize one per-process memory read against its recorded identity.

    Identity is the ri_proc_start_abstime observed when the PID was first
    sampled; every read re-checks it. Missing samples (lost, changed or
    unreadable processes) keep the PID identity and are never zero-filled.
    """
    entry = {"role": role, "pid": pid, "expected_start_abstime": expected_abstime}
    if reading is None:
        entry["status"] = "lost" if error_kind == "gone" else "unreadable"
        if error_kind and error_kind != "gone":
            entry["error"] = error_kind
        return entry
    abstime = reading["ri_proc_start_abstime"]
    entry["observed_start_abstime"] = abstime
    if expected_abstime is None:
        entry.update(status="sampled", identity_established=True,
                     ri_phys_footprint_bytes=reading["ri_phys_footprint"],
                     ri_resident_size_bytes=reading["ri_resident_size"],
                     ri_wired_size_bytes=reading["ri_wired_size"])
    elif abstime == expected_abstime:
        entry.update(status="sampled", ri_phys_footprint_bytes=reading["ri_phys_footprint"],
                     ri_resident_size_bytes=reading["ri_resident_size"],
                     ri_wired_size_bytes=reading["ri_wired_size"])
    else:
        # A different process now owns this PID: the sample is missing even
        # though the read succeeded.
        entry["status"] = "changed"
    return entry


def parse_device_states(list_output):
    """Map UDID to state from `xcrun simctl list -j devices` stdout."""
    listing = json.loads(list_output)
    states = {}
    for group in listing.get("devices", {}).values():
        for device in group:
            udid, state = device.get("udid"), device.get("state")
            if isinstance(udid, str) and isinstance(state, str):
                states[udid] = state
    return states


def parse_role_pids(ps_output, udid, app_bundle_name):
    """Resolve the owned simulator's launchd_sim and app PIDs from `ps` text."""
    simulator, app = None, None
    for line in ps_output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        command = fields[1]
        if udid in command and "launchd_sim" in command:
            if simulator is not None:
                raise BenchmarkError("multiple launchd_sim processes matched the owned simulator")
            simulator = pid
        elif (udid in command and app_bundle_name and
              re.search(re.escape(app_bundle_name) + r"\.app/", command) and
              "launchd_sim" not in command):
            if app is not None:
                raise BenchmarkError("multiple app processes matched the owned simulator")
            app = pid
    return {ROLE_APP: app, ROLE_SIMULATOR: simulator}


def parse_shared_services(ps_output):
    """Shared CoreSimulator host services, reported separately from per-arm totals."""
    services = []
    for line in ps_output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if SHARED_SERVICE_PATTERN.search(fields[1]):
            services.append({"pid": pid, "command": Path(fields[1]).name[:128],
                            "scope": "shared-coresimulator-host-service"})
    return services


def parse_test_case_durations(log_text):
    """Runner-reported whole-test durations from xcodebuild output."""
    durations, failures = {}, []
    for name, verdict, seconds in re.findall(
            r"Test Case '([^']+)' (passed|failed) \(([0-9.]+) seconds\)", log_text):
        entry = {"name": name, "verdict": verdict, "seconds": float(seconds)}
        if verdict == "failed":
            failures.append(entry)
        else:
            durations[name] = entry
    return durations, failures


# ---------------------------------------------------------------------------
# Pure logic: usable-target predicates and operation records.
# ---------------------------------------------------------------------------


def _number(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def usable_apple_target(hierarchy, bundle_id, identifier, label, expected_before=None):
    """First-usable check for an Apple DeviceInteraction hierarchy text."""
    result = {"usable": False, "app_identifier_seen": False, "selector_basis": None,
              "point": None, "bounds": None, "enabled_evidence": None}
    if not isinstance(hierarchy, str) or not hierarchy.strip():
        result["reason"] = "the hierarchy was empty"
        return result
    if bundle_id and bundle_id not in hierarchy:
        result["reason"] = "the hierarchy did not show the expected app identifier"
        return result
    result["app_identifier_seen"] = bool(bundle_id)
    lines = hierarchy.splitlines()
    escaped = re.escape(identifier)
    id_field = re.compile(
        r"\b(?:identifier|accessibilityIdentifier)\s*[:=]\s*(['\"])" + escaped + r"\1"
    )
    matches = [line for line in lines if id_field.search(line)]
    basis = "identifier"
    if not matches:
        matches = [line for line in lines
                   if f'"{label}"' in line or f"'{label}'" in line] if label else []
        basis = "label"
    if len(matches) != 1:
        result["reason"] = f"the target {identifier} was not uniquely present"
        return result
    line = matches[0]
    if DISABLED_TOKEN.search(line):
        result["reason"] = "the target line reported the element as disabled"
        return result
    frame = FRAME_PATTERN.search(line)
    if frame is not None:
        x, y, width, height = (_number(value) for value in frame.groups())
        if any(value is None for value in (x, y, width, height)):
            result["reason"] = "the target bounds were not finite numbers"
            return result
        if not (width > 0 and height > 0):
            result["reason"] = "the target bounds were not positive"
            return result
        result["bounds"] = {"x": x, "y": y, "width": width, "height": height}
    point = POINT_PATTERN.search(line)
    if point is None or any(_number(value) is None for value in point.groups()):
        result["reason"] = "the hierarchy did not provide a usable point for the target"
        return result
    result.update(usable=True, selector_basis=basis, point=" ".join(point.groups()))
    if "nabled" in line or "hittable" in line:
        result["enabled_evidence"] = True
    if expected_before is not None and expected_before not in hierarchy:
        result.update(usable=False,
                      reason=f"the expected pre-action state {expected_before!r} was absent")
    return result


def usable_snapshot_target(snapshot, test_id, role=None):
    """First-usable check for an Ashnazg ui_snapshot payload."""
    result = {"usable": False, "selector_basis": None, "rect": None, "enabled_evidence": True}
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("nodes"), list):
        result["reason"] = "the snapshot omitted its node list"
        return result
    matches = [node for node in snapshot["nodes"]
               if isinstance(node, dict) and node.get("testId") == test_id]
    if len(matches) != 1:
        result["reason"] = f"the target {test_id} was not uniquely present"
        return result
    node = matches[0]
    if role is not None and node.get("role") != role:
        result["reason"] = f"the target {test_id} did not have role {role}"
        return result
    if node.get("enabled") is not True:
        result["reason"] = "the target was not enabled"
        return result
    rect = node.get("rect")
    if (not isinstance(rect, list) or len(rect) != 4 or
            any(_number(value) is None for value in rect) or
            not (rect[2] > rect[0] and rect[3] > rect[1])):
        result["reason"] = "the target bounds were not a valid rect"
        return result
    result.update(usable=True, selector_basis="testId", rect=rect, role=node.get("role"))
    return result


def usable_meer_login_snapshot(snapshot):
    """Verify both Meer login fields resolve uniquely with no leaked secure value."""
    email_res = usable_snapshot_target(snapshot, MEER_LOGIN_EMAIL, role="textfield")
    if not email_res["usable"]:
        return email_res
    pw_res = usable_snapshot_target(snapshot, MEER_LOGIN_PASSWORD, role="securetextfield")
    if not pw_res["usable"]:
        return pw_res
    pw_node = next(node for node in snapshot["nodes"]
                   if isinstance(node, dict) and node.get("testId") == MEER_LOGIN_PASSWORD)
    if "value" in pw_node:
        return {"usable": False, "selector_basis": None, "rect": None, "enabled_evidence": True,
                "reason": "secure field serialized a value before typing"}
    return {"usable": True, "selector_basis": "testId",
            "rect": email_res["rect"], "password_rect": pw_res["rect"],
            "enabled_evidence": True}


def finish_operation(timings, name, started_ns, ended_ns, error=None):
    """Record one table row: a failed operation keeps its elapsed time and
    failure record and has no success timing."""
    elapsed = ended_ns - started_ns
    entry = {"status": "failed" if error else "passed", "elapsed_ns": elapsed}
    if error:
        entry["failure"] = sanitize(str(error))[:500]
    else:
        entry["success_timing_ns"] = elapsed
    timings[name] = entry


def skip_operation(timings, name, reason):
    timings[name] = {"status": "skipped", "elapsed_ns": None, "reason": reason}


# ---------------------------------------------------------------------------
# Pure logic: receipt schema.
# ---------------------------------------------------------------------------

RECEIPT_SCHEMA_VERSION = 1
ARM_NAMES = ("apple-mcp", "ashnazg-mcp", "direct-xctest")


def build_receipt(scope, timings, memory, shutdown, artifacts, status, error=None, elapsed_seconds=None):
    receipt = {"schema_version": RECEIPT_SCHEMA_VERSION, "status": status, "scope": scope,
               "timings": timings, "memory": memory, "shutdown": shutdown, "artifacts": artifacts}
    if error is not None:
        receipt["error"] = sanitize(str(error))[:500]
    if elapsed_seconds is not None:
        receipt["elapsed_seconds"] = elapsed_seconds
    return receipt


def validate_receipt(receipt):
    """Return every missing or invalid receipt field; empty means valid."""
    problems = []
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        problems.append("schema_version")
    if receipt.get("status") not in ("passed", "failed"):
        problems.append("status")
    scope = receipt.get("scope")
    if not isinstance(scope, dict):
        return problems + ["scope"]
    if scope.get("arm") not in ARM_NAMES:
        problems.append("scope.arm")
    if scope.get("condition") not in ("cold", "warm"):
        problems.append("scope.condition")
    simulator = scope.get("simulator")
    if not isinstance(simulator, dict) or not isinstance(simulator.get("udid"), str) or not simulator.get("udid"):
        problems.append("scope.simulator.udid")
    app = scope.get("app")
    if not isinstance(app, dict) or not app.get("bundle_id") or not app.get("sha256"):
        problems.append("scope.app")
    if not isinstance(scope.get("source_revisions"), dict) or not scope["source_revisions"]:
        problems.append("scope.source_revisions")
    timings = receipt.get("timings")
    if not isinstance(timings, dict):
        return problems + ["timings"]
    for name in ("first_usable_snapshot", "tap_to_fresh_frame"):
        entry = timings.get(name)
        if not isinstance(entry, dict):
            problems.append(f"timings.{name}")
            continue
        if entry.get("status") not in ("passed", "failed", "skipped"):
            problems.append(f"timings.{name}.status")
        if entry["status"] in ("passed", "failed") and not isinstance(entry.get("elapsed_ns"), int):
            problems.append(f"timings.{name}.elapsed_ns")
        if entry["status"] == "passed" and not isinstance(entry.get("success_timing_ns"), int):
            problems.append(f"timings.{name}.success_timing_ns")
        if entry["status"] == "failed" and not entry.get("failure"):
            problems.append(f"timings.{name}.failure")
        if entry["status"] == "skipped" and not entry.get("reason"):
            problems.append(f"timings.{name}.reason")
    memory = receipt.get("memory")
    if not isinstance(memory, dict) or not isinstance(memory.get("stages"), dict):
        return problems + ["memory"]
    for stage in MEMORY_STAGES:
        samples = memory["stages"].get(stage)
        if not isinstance(samples, list):
            problems.append(f"memory.stages.{stage}")
            continue
        for sample in samples:
            if not isinstance(sample, dict) or not sample.get("role"):
                problems.append(f"memory.stages.{stage}.role")
            elif sample.get("status") == "not-running":
                continue
            elif not isinstance(sample.get("pid"), int):
                problems.append(f"memory.stages.{stage}.pid")
            elif sample.get("status") == "sampled" and not isinstance(
                    sample.get("ri_phys_footprint_bytes"), int):
                problems.append(f"memory.stages.{stage}.footprint")
    shutdown = receipt.get("shutdown")
    if not isinstance(shutdown, dict):
        return problems + ["shutdown"]
    if not isinstance(shutdown.get("simulator_shutdown"), bool):
        problems.append("shutdown.simulator_shutdown")
    if not isinstance(shutdown.get("owned_helpers_gone"), bool):
        problems.append("shutdown.owned_helpers_gone")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        problems.append("artifacts")
    else:
        for name, meta in artifacts.items():
            if not isinstance(meta, dict) or not isinstance(meta.get("sha256"), str):
                problems.append(f"artifacts.{name}")
    return problems


# ---------------------------------------------------------------------------
# Pure logic: pinned app-tree identity and tool-contract fail-closed checks.
# ---------------------------------------------------------------------------


def _hash_app_dir(directory, root, hasher):
    """One directory level of the pinned hashApp walk (see app_tree_sha256)."""
    try:
        entries = list(os.scandir(directory))
    except OSError as error:
        raise BenchmarkError(f"failed to read {directory}: {error}") from error

    def utf16_key(entry):
        # Node's readdirSync().sort() compares UTF-16 code units; encode the
        # same way so entry order matches hashApp exactly, and reject
        # undecodable names instead of silently lossy-hashing them.
        try:
            return entry.name.encode("utf-16-be")
        except UnicodeEncodeError:
            raise BenchmarkError(
                f"app tree contains a non-UTF-8 name: {entry.path}") from None

    files = 0
    for entry in sorted(entries, key=utf16_key):
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise BenchmarkError(f"failed to stat {entry.path}: {error}") from error
        mode = stat.S_IMODE(metadata.st_mode)
        relative = Path(entry.path).relative_to(root).as_posix()
        is_dir = entry.is_dir(follow_symlinks=False)
        size = 0 if is_dir else metadata.st_size
        # serde_json and JSON.stringify emit identical compact bytes here.
        hasher.update(json.dumps([relative, mode, size], ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8"))
        if is_dir:
            files += _hash_app_dir(Path(entry.path), root, hasher)
        elif entry.is_symlink():
            target = os.readlink(entry.path)
            if os.path.isabs(target):
                raise BenchmarkError(
                    f"app tree contains an absolute symlink {relative}; the staged "
                    "copy must stay relocatable")
            try:
                canonical = Path(entry.path).resolve(strict=True)
            except OSError:
                canonical = None
            if canonical is None or canonical == root or not canonical.is_relative_to(root):
                raise BenchmarkError(
                    f"app tree contains a dangling or escaping symlink: {relative}")
            hasher.update(os.fsencode(target))
        elif entry.is_file(follow_symlinks=False):
            files += 1
            with open(entry.path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
        else:
            raise BenchmarkError(f"app tree contains a non-regular entry: {relative}")
    return files


def app_tree_sha256(root):
    """Deterministic .app bundle identity under the pinned hashApp convention.

    Byte-for-byte mirror of src/ios_app.rs stage_tree_pinned (whose recorded
    reference digests pin this exact walk): depth-first over entries sorted
    by name, one SHA-256 frame per entry for
    ["<relative path>",<mode & 0o777>,<directory ? 0 : lstat size>] (compact
    JSON, UTF-8), file bytes streamed after their frame, and a symlink
    contributing its read target text after its frame. Absolute, dangling or
    escaping symlinks, non-regular entries, non-UTF-8 names and empty trees
    are rejected — matching the Rust staging so re-hashing the staged tree
    reproduces this digest.
    """
    root = Path(root).resolve()
    hasher = hashlib.sha256()
    files = _hash_app_dir(root, root, hasher)
    if files == 0:
        raise BenchmarkError(f"{root} contains no files to pin")
    return hasher.hexdigest()


def preflight_app(app_argument):
    """Validate and digest the prebuilt .app bundle before anything runs.

    Called before any simulator exists; a directory that is not a real .app
    bundle, or content the pinned walk rejects, fails closed here instead
    of crashing mid-run the way opening the directory as a file did.
    """
    path = Path(app_argument)
    if not path.is_dir() or path.suffix != ".app":
        raise BenchmarkError("--app must be an existing .app bundle directory")
    return {"path": str(path.resolve()), "sha256": app_tree_sha256(path),
            "sha256_basis": "pinned hashApp tree digest: sorted depth-first walk "
                            "hashing relative path, mode and lstat size per "
                            "entry, then file bytes or symlink targets"}


def require_apple_tools(advertised_names):
    """Fail closed before any session when the bridge's enablement does not
    expose the DeviceInteraction surface this comparison measures."""
    missing = [name for name in APPLE_REQUIRED_TOOLS if name not in advertised_names]
    if missing:
        raise BenchmarkError(
            "the Apple MCP bridge did not advertise " + ", ".join(missing) +
            "; the Xcode MCP enablement prerequisite is invalid (enable the "
            "bridge and Xcode's external-agent access before observing)")
    return True


def require_owned_session_descriptor(descriptor, udid, bundle_id, app_sha256):
    """Fail closed unless the bridge's ios-app-session descriptor pins exactly
    this run's owned simulator and prebuilt app; an implicitly selected or
    misbound session is never observed."""
    if not isinstance(descriptor, dict):
        raise BenchmarkError("the bridge never reported its ios-app-session descriptor")
    simulator = descriptor.get("simulator")
    if not isinstance(simulator, dict):
        raise BenchmarkError("the bridge descriptor omitted the selected simulator")
    selected = simulator.get("udid")
    record = {"arm_selected_simulator": selected,
              "descriptor_bundle_id": descriptor.get("bundleId"),
              "descriptor_app_sha256": descriptor.get("appSha256"),
              "app_identity_basis": "session descriptor bundle and app digest"}
    if not isinstance(selected, str) or selected.lower() != udid.lower():
        raise BenchmarkError(
            "the bridge selected a simulator other than the owned one; "
            "explicit simulator selection is unqualified for this arm")
    if descriptor.get("bundleId") != bundle_id:
        raise BenchmarkError("the bridge pinned a bundle other than the benchmark app")
    if descriptor.get("appSha256") != app_sha256:
        raise BenchmarkError("the bridge pinned app bytes other than the benchmark app tree")
    return record


def require_advancing_snapshot_id(previous, current, what):
    """The pinned bridge contract: every observed state must advance the
    sequence; a stale, missing or non-integer snapshotId is an unconfirmed
    boundary, never a measured one."""
    if isinstance(current, bool) or not isinstance(current, int) or current <= previous:
        raise BenchmarkError(f"{what} did not advance the snapshot sequence past {previous}")
    return current


def parse_ashnazg_screenshot(result):
    """The bridge's pinned ui_screenshot result shape: exactly one image
    block and one text block carrying JSON metadata; there is no
    structuredContent on this tool (src/mcp.rs returns content blocks)."""
    if not isinstance(result, dict):
        raise BenchmarkError("ui_screenshot did not return a tool result")
    blocks = result.get("content") or []
    images = [block for block in blocks
             if isinstance(block, dict) and block.get("type") == "image"]
    texts = [block for block in blocks
             if isinstance(block, dict) and block.get("type") == "text"]
    if len(images) != 1 or len(texts) != 1:
        raise BenchmarkError("ui_screenshot did not return exactly one image and one metadata block")
    try:
        image = base64.b64decode(images[0].get("data", ""), validate=True)
        metadata = json.loads(texts[0].get("text", ""))
    except ValueError as error:
        raise BenchmarkError(f"ui_screenshot content was not valid base64 or JSON: {error}") from error
    if not image or not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BenchmarkError("the fresh frame was not a PNG")
    if not isinstance(metadata, dict):
        raise BenchmarkError("ui_screenshot metadata was not a JSON object")
    return image, metadata


# ---------------------------------------------------------------------------
# macOS-only execution helpers.
# ---------------------------------------------------------------------------


def require_macos_host():
    if sys.platform != "darwin":
        raise BenchmarkError("the iOS session benchmark requires macOS")


def _libproc():
    import ctypes

    try:
        return ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError:
        return ctypes.CDLL(None, use_errno=True)


def read_proc_rusage(pid):
    """proc_pid_rusage(pid, RUSAGE_INFO_V1) through ctypes; macOS only."""
    import ctypes

    buffer = ctypes.create_string_buffer(RUSAGE_BUFFER_BYTES)
    library = _libproc()
    library.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    library.proc_pid_rusage.restype = ctypes.c_int
    if library.proc_pid_rusage(pid, RUSAGE_INFO_V1, buffer) != 0:
        code = ctypes.get_errno()
        if code == errno.ESRCH:
            raise ProcessGone(f"process {pid} was gone")
        raise BenchmarkError(f"proc_pid_rusage failed for {pid} with errno {code}")
    sample = parse_rusage_buffer(bytes(buffer))
    if sample["ri_proc_start_abstime"] == 0:
        raise BenchmarkError(f"proc_pid_rusage returned no process start identity for {pid}")
    return sample


def list_process_commands():
    """`ps -axo pid=,command=` output; works on macOS and Linux."""
    return subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                          text=True, check=True, timeout=30).stdout


class MemoryTracker:
    """Samples the app, simulator and helper identities at shared stages."""

    def __init__(self, sampler=read_proc_rusage, ps_lister=list_process_commands):
        self._sampler = sampler
        self._ps_lister = ps_lister
        self.identities = {}
        self.stages = {}
        self.shared_stages = {}

    def _listing(self):
        try:
            return self._ps_lister(), None
        except (OSError, subprocess.SubprocessError) as error:
            return "", str(error)

    def sample_stage(self, stage, udid, app_bundle_name, helper_pids=()):
        """One lifecycle stage: per-role samples plus shared services."""
        listing, listing_error = self._listing()
        try:
            dynamic = parse_role_pids(listing, udid, app_bundle_name)
            dynamic_error = None
        except BenchmarkError as error:
            dynamic, dynamic_error = {ROLE_APP: None, ROLE_SIMULATOR: None}, str(error)
        samples = []
        for role in (ROLE_APP, ROLE_SIMULATOR):
            pid = dynamic.get(role)
            if pid is None:
                samples.append({"role": role, "pid": None, "status": "not-running",
                                "note": dynamic_error or listing_error
                                or "no process matched the owned simulator"})
            else:
                samples.append(self._sample(role, pid))
        for pid in sorted(set(helper_pids)):
            samples.append(self._sample(ROLE_HELPER, pid))
        self.stages[stage] = samples
        shared = [dict(self._sample("shared", service["pid"]),
                       scope=service["scope"], command=service["command"])
                  for service in parse_shared_services(listing)]
        self.shared_stages[stage] = shared

    def _sample(self, role, pid):
        key = (role, pid)
        expected = self.identities.get(key)
        try:
            reading = self._sampler(pid)
        except ProcessGone:
            entry = normalize_memory_sample(role, pid, expected, None, error_kind="gone")
            self.identities.pop(key, None)
            return entry
        except (BenchmarkError, OSError, ValueError) as error:
            return normalize_memory_sample(role, pid, expected, None, error_kind=str(error)[:200])
        entry = normalize_memory_sample(role, pid, expected, reading)
        if entry["status"] == "sampled" and expected is None:
            self.identities[key] = reading["ri_proc_start_abstime"]
        elif entry["status"] == "changed":
            # Re-anchor on the new process so later stages can sample again.
            self.identities[key] = reading["ri_proc_start_abstime"]
        return entry


MEER_MILESTONES = ("first_valid_login", "submit_to_home", "home_to_search", "search_to_home")


class MeerCheckpointServer:
    """One ordered loopback handoff from an owned direct XCTest invocation."""

    def __init__(self, tracker, udid, app_bundle_name, helper_pids):
        self.tracker = tracker
        self.udid = udid
        self.app_bundle_name = app_bundle_name
        self.helper_pids = helper_pids
        self.records = {}
        self.initial_processes = None

    def __enter__(self):
        checkpoint = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(2)
                try:
                    line = self.rfile.readline(513)
                    if len(line) > 512 or not line.endswith(b"\n"):
                        return
                    if checkpoint.record(json.loads(line)):
                        self.wfile.write(b'{"ok":true}\n')
                except (OSError, ValueError, RecursionError):
                    return

        self.server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.worker.start()
        return self

    def __exit__(self, *errors):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()

    def record(self, row):
        index = len(self.records)
        if index >= len(MEER_MILESTONES) or not isinstance(row, dict):
            return False
        name = MEER_MILESTONES[index]
        uptime = row.get("uptimeNs")
        last = self.records[MEER_MILESTONES[index - 1]]["uptime_ns"] if index else 0
        if (row.get("op") != "checkpoint" or row.get("milestone") != name
                or type(row.get("sequence")) is not int or row["sequence"] != index + 1
                or type(uptime) is not int or not last < uptime <= (1 << 64) - 1):
            return False
        started = time.monotonic_ns()
        self.tracker.sample_stage(name, self.udid, self.app_bundle_name, self.helper_pids)
        samples = self.tracker.stages[name]
        complete = ({sample["role"] for sample in samples} == {ROLE_APP, ROLE_SIMULATOR, ROLE_HELPER}
                    and all(sample.get("status") == "sampled" for sample in samples))
        if complete:
            identities = {(sample["role"], sample["pid"], sample["observed_start_abstime"])
                          for sample in samples}
            if index == 0:
                self.initial_processes = identities
            complete = identities == self.initial_processes
        self.records[name] = {"sequence": index + 1, "uptime_ns": uptime,
                              "sample_started_ns": started, "sample_ended_ns": time.monotonic_ns(),
                              "complete": complete}
        return complete


def qualify_meer_xctest(attachment, checkpoints, bundle_id):
    """Match native timing evidence to the sampler at the same UI boundary."""
    expected = {
        "schema": "meer-journey-boundaries", "version": 1,
        "clock": "dispatch-uptime-ns", "navigation": "visible-controls",
        "bundleId": bundle_id, "initialUrlScope": "loopback-only",
        "emailReadback": "exact", "passwordMaskEvidence": "native-bullets-and-length",
        "typedStagesOnce": True, "mutationReplays": 0,
    }
    if not isinstance(attachment, dict) or any(
            type(attachment.get(key)) is not type(value) or attachment[key] != value
            for key, value in expected.items()):
        raise BenchmarkError("XCTest evidence did not qualify the pinned Meer journey")

    handshakes = attachment.get("checkpoints")
    if isinstance(handshakes, dict) and handshakes.get("requested") is True:
        native_statuses = handshakes.get("milestones")
    else:
        native_statuses = None
    expected_prefixes = ("firstValidLogin", "submitToHome", "search", "homeReturn")
    last_end = 0
    durations, missing = {}, []
    for index, (milestone, prefix) in enumerate(zip(MEER_MILESTONES, expected_prefixes)):
        start = attachment.get(prefix + "StartUptimeNs")
        end = attachment.get(prefix + "EndUptimeNs")
        if type(start) is not int or type(end) is not int or not last_end <= start < end:
            raise BenchmarkError("XCTest journey clocks were absent or out of order")
        last_end = end
        durations[milestone] = (end - start) / 1_000_000
        native = {"milestone": milestone, "sequence": index + 1, "status": "acked"}
        sample = checkpoints.get(milestone, {})
        if (not isinstance(native_statuses, list) or len(native_statuses) != len(MEER_MILESTONES)
                or native_statuses[index] != native
                or sample.get("complete") is not True or sample.get("sequence") != index + 1
                or sample.get("uptime_ns") != end):
            missing.append(milestone)
    if "credentialEntryStartUptimeNs" in attachment:
        credentials_start = attachment["credentialEntryStartUptimeNs"]
        ready = attachment["firstValidLoginEndUptimeNs"]
        submit = attachment["submitToHomeStartUptimeNs"]
        if type(credentials_start) is not int or not ready <= credentials_start < submit:
            raise BenchmarkError("XCTest credential-entry clock was out of order")
        durations["login_to_home"] = (
            attachment["submitToHomeEndUptimeNs"] - credentials_start
        ) / 1_000_000
    capture_keys = (
        "homeCapture", "finalHomeCapture", "captureExtent", "capturePixels",
        "homeScreenshotStartUptimeNs", "homeScreenshotEndUptimeNs",
        "finalHomeScreenshotStartUptimeNs", "finalHomeScreenshotEndUptimeNs",
    )
    if any(key in attachment for key in capture_keys):
        if (attachment.get("captureExtent") != "app"
                or attachment.get("homeCapture") != "home-screenshot"
                or attachment.get("finalHomeCapture") != "final-home-screenshot"):
            raise BenchmarkError("XCTest Meer capture attachment metadata did not match")
        pixels = attachment.get("capturePixels")
        if (not isinstance(pixels, list) or len(pixels) != 2
                or not all(type(dim) is int and dim > 0 for dim in pixels)):
            raise BenchmarkError("XCTest Meer capture dimensions were invalid")
        h_start = attachment.get("homeScreenshotStartUptimeNs")
        h_end = attachment.get("homeScreenshotEndUptimeNs")
        f_start = attachment.get("finalHomeScreenshotStartUptimeNs")
        f_end = attachment.get("finalHomeScreenshotEndUptimeNs")
        if (not all(type(v) is int and v >= 0 for v in (h_start, h_end, f_start, f_end))
                or not (attachment["submitToHomeEndUptimeNs"] <= h_start < h_end
                        <= attachment["searchStartUptimeNs"])
                or not (attachment["homeReturnEndUptimeNs"] <= f_start < f_end)):
            raise BenchmarkError("XCTest Meer capture clocks were absent or out of order")
        durations["home_screenshot"] = (h_end - h_start) / 1_000_000
        durations["final_home_screenshot"] = (f_end - attachment["homeReturnEndUptimeNs"]) / 1_000_000
        durations["search_to_home_with_screenshot"] = (
            f_end - attachment["homeReturnStartUptimeNs"]
        ) / 1_000_000
    return {"milliseconds_by_milestone": durations, "memory_complete": not missing,
            "missing_memory_milestones": missing}


def simctl(*args, timeout=180):
    return subprocess.run(["xcrun", "simctl", *args], capture_output=True, text=True,
                          timeout=timeout, check=True).stdout


def device_state(udid):
    states = parse_device_states(simctl("list", "-j", "devices"))
    return states.get(udid)


def create_simulator(name, device_type, runtime):
    return simctl("create", name, device_type, runtime).strip()


def wait_for_boot(udid, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if device_state(udid) == "Booted":
            return
        time.sleep(1.0)
    raise BenchmarkError(f"the owned simulator {udid} did not reach Booted")


def read_text_bounded(path):
    with path.open("rb") as stream:
        data = stream.read(CAPTURE_LIMIT + 1)
    if len(data) > CAPTURE_LIMIT:
        raise BenchmarkError(f"{path.name} exceeded the capture bounds")
    return data.decode("utf-8", "replace")


def artifact_record(data, path):
    path.write_bytes(data)
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def retain_file(result, key, output, name):
    value = result.get(key)
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise BenchmarkError(f"{key} did not identify a local file")
    path = Path(value)
    if not path.is_file():
        raise BenchmarkError(f"{key} was unavailable on this host")
    with path.open("rb") as stream:
        data = stream.read(CAPTURE_LIMIT + 1)
    if not data or len(data) > CAPTURE_LIMIT:
        raise BenchmarkError(f"{key} exceeded the capture bounds")
    if name.endswith(".png") and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise BenchmarkError(f"{key} did not contain a PNG")
    return artifact_record(data, output / name)


# ---------------------------------------------------------------------------
# Observation driver: one arm against one owned simulator condition.
# ---------------------------------------------------------------------------


class AppleMcpArm:
    """Apple's Xcode MCP bridge through DeviceInteraction tools."""

    name = "apple-mcp"
    first_snapshot_boundaries = (
        "DeviceInteractionStartSession request on the owned simulator to the first "
        "hierarchy with the expected app identifier, enabled target and valid bounds")

    def __init__(self, context):
        self.context = context
        self.process = None
        self.client = None
        self.call = None
        self.session_key = None

    def start(self):
        context = self.context
        began = time.monotonic_ns()
        self.process = subprocess.Popen(
            ["xcrun", "mcpbridge"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        context.register_helpers(self.process.pid)
        self.client = StdioClient(self.process)
        try:
            advertised = list_tools(self.client)
        except AcceptanceError as error:
            raise BenchmarkError(
                f"the Apple bridge failed before advertising tools: {error}") from error
        # Invalid enablement (a closed Xcode or a disabled bridge) fails
        # closed here, before any session or observation is attempted.
        require_apple_tools([tool.get("name") for tool in advertised.get("tools", [])
                             if isinstance(tool, dict)])
        self.call = apple_tool_caller(self.client)
        context.timings["arm_start"] = {
            "status": "passed", "elapsed_ns": time.monotonic_ns() - began,
            "boundaries": "mcpbridge spawn through initialize and tools/list "
                          "with the DeviceInteraction surface verified; "
                          "recorded separately from the first-usable-snapshot row",
        }

    def first_usable_snapshot(self):
        context = self.context
        session = self.call("DeviceInteractionStartSession", {
            "deviceIdentifier": context.udid,
            "sessionIdentifier": f"Ashnazg Benchmark {context.condition}",
        })
        self.session_key = session.get("interactionSessionKey")
        if not isinstance(self.session_key, str) or not self.session_key:
            self.session_key = None
            raise BenchmarkError("start session omitted the interaction key")
        device = session.get("deviceUUID")
        if (session.get("deviceIsSimulator") is not True or not isinstance(device, str)
                or device.lower() != context.udid.lower()):
            raise BenchmarkError("Apple selected a device other than the owned simulator")
        arguments = {"interactSessionKey": self.session_key,
                     "activationBundleId": context.bundle_id}
        attempts, last = 0, None
        deadline = time.monotonic() + context.first_snapshot_timeout
        while time.monotonic() < deadline:
            attempts += 1
            try:
                state = self.call("DeviceInteractionSynthesize", dict(arguments))
                hierarchy = read_text_bounded(Path(state["hierarchyPath"]))
                usable = usable_apple_target(
                    hierarchy, context.bundle_id, context.tap_identifier,
                    context.tap_label, context.expected_before,
                )
                if usable["usable"]:
                    return {
                        "usable": usable,
                        "attempts": attempts,
                        "app_identity_basis": "hierarchy application bundle identifier",
                        "hierarchy": retain_file(state, "hierarchyPath", context.output,
                                                 "first-hierarchy.txt"),
                        "screenshot": retain_file(state, "screenshotPath", context.output,
                                                 "first-screenshot.png"),
                    }, None
                last = usable.get("reason", "the hierarchy was not usable yet")
            except (BenchmarkError, InventoryError, OSError, KeyError, ValueError) as error:
                last = str(error)
            time.sleep(0.5)
        return {"attempts": attempts, "last_reason": sanitize(last)[:500]}, BenchmarkError(
            f"no usable hierarchy before the timeout; last reason: {last}")

    def tap_to_fresh_frame(self, first_record):
        context = self.context
        point = first_record["usable"]["point"]
        arguments = {"interactSessionKey": self.session_key,
                     "activationBundleId": context.bundle_id}
        acted = self.call("DeviceInteractionSynthesize", {
            **arguments, "interactionCommand": f"t {point}",
        })
        hierarchy = retain_file(acted, "hierarchyPath", context.output, "after-tap-hierarchy.txt")
        screenshot = retain_file(acted, "screenshotPath", context.output, "after-tap.png")
        text = read_text_bounded(context.output / "after-tap-hierarchy.txt")
        if context.expected_after not in text:
            raise BenchmarkError("the tap did not produce the expected semantic change")
        if context.expected_before and context.expected_before in text:
            raise BenchmarkError("the pre-action state was still present after the tap")
        if screenshot["sha256"] == first_record["screenshot"]["sha256"]:
            raise BenchmarkError("the post-tap PNG repeated the pre-tap PNG")
        return {"hierarchy": hierarchy, "screenshot": screenshot,
                "semantic_change": [context.expected_before, context.expected_after]}, None

    def shutdown(self):
        ended = "not-started"
        if self.session_key and self.call:
            try:
                self.call("DeviceInteractionEndSession",
                          {"interactionSessionKey": self.session_key})
                ended = "passed"
            except (BenchmarkError, InventoryError, OSError):
                ended = "failed"
        self.session_key = None
        exit_code = _eof_teardown(self.process)
        return {"end_session": ended, "bridge_exit_code": exit_code}


def write_ashnazg_arm_manifest(context, template):
    scope = context.simulator_scope
    if scope.get("created_by_run") is not True or scope.get("udid") != context.udid:
        raise BenchmarkError("refusing to hand off a simulator outside this run")
    name = scope.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"AshnazgIosBenchmark-[0-9a-f]{12}", name):
        raise BenchmarkError("the simulator must use the disposable benchmark name")
    data = {
        **template,
        "appPath": str(context.app_path.resolve()),
        "appSha256": context.app_sha256,
        "bundleId": context.bundle_id,
        "runtime": scope["runtime"],
        "deviceType": scope["device_type"],
        "benchmarkSimulator": {"udid": context.udid, "name": name},
    }
    if getattr(context, "initial_url", None):
        data["initialUrl"] = context.initial_url
    path = context.output.resolve() / "ashnazg-arm-manifest.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        json.dump(data, target, indent=2, sort_keys=True)
        target.write("\n")
    return path


class AshnazgMcpArm:
    """Ashnazg's native XCTest bridge driven over stdio MCP.

    The harness can write the manifest for its own booted simulator. The
    bridge's session descriptor must confirm the device and artifact pins.
    """

    name = "ashnazg-mcp"
    first_snapshot_boundaries = (
        "first ui_snapshot request after the session-descriptor identity check to the "
        "first snapshot with an enabled target and valid bounds")

    def __init__(self, context):
        self.context = context
        self.process = None
        self.client = None
        # The full Meer journey (typed login, prompt-clearing polls, captures,
        # and search round-trip) typically takes ~25-30 calls; 64 keeps headroom.
        self.ids = iter(range(64))
        self.descriptor = None
        self.last_snapshot_id = 0

    def _request(self, message, timeout):
        try:
            return self.client.request(message, timeout=timeout)
        except AcceptanceError as error:
            raise BenchmarkError(str(error)) from error

    def _call_result(self, name, arguments, timeout=240.0):
        message = {"jsonrpc": "2.0", "id": next(self.ids), "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        response = self._request(message, timeout)
        result = response.get("result", {})
        if "error" in response or result.get("isError"):
            raise BenchmarkError(f"{name} rejected: {response.get('error', result.get('content'))}")
        return result

    def _call(self, name, arguments, timeout=240.0):
        payload = self._call_result(name, arguments, timeout).get("structuredContent")
        if not isinstance(payload, dict):
            raise BenchmarkError(f"{name} did not return structured content")
        return payload

    def start(self):
        context = self.context
        began = time.monotonic_ns()
        if context.ashnazg_manifest is not None:
            manifest = write_ashnazg_arm_manifest(context, context.ashnazg_manifest)
            command = [part.format(udid=context.udid, manifest=manifest)
                       for part in context.arm_command]
        elif "{udid}" in " ".join(context.arm_command):
            command = [part.format(udid=context.udid) for part in context.arm_command]
        else:
            raise BenchmarkError(
                "the arm command did not select the owned simulator; pass {udid} "
                "or --ios-app {manifest} with a benchmark manifest template")
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env={**os.environ, "RUST_LOG": "info"})
        context.register_helpers(self.process.pid)
        self.client = StdioClient(self.process)
        initialize = self._request({
            "jsonrpc": "2.0", "id": next(self.ids), "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "ios-session-benchmark", "version": "1"}},
        }, timeout=900.0)
        if "error" in initialize:
            raise BenchmarkError("initialize was rejected before any observation")
        negotiated = initialize.get("result", {}).get("protocolVersion")
        if negotiated != "2024-11-05":
            raise BenchmarkError(
                f"the bridge negotiated {negotiated!r} instead of the requested protocol version")
        self.client.notification({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = self._request({"jsonrpc": "2.0", "id": next(self.ids),
                                "method": "tools/list", "params": {}}, timeout=60.0)
        tools = listed.get("result", {}).get("tools", [])
        names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
        if names != set(ASHNAZG_REQUIRED_TOOLS):
            raise BenchmarkError(
                f"the bridge advertised {sorted(map(str, names))} instead of the "
                "pinned ios-app tool set")
        self._require_owned_simulator()
        context.timings["arm_start"] = {
            "status": "passed", "elapsed_ns": time.monotonic_ns() - began,
            "boundaries": "bridge spawn through initialize and tools/list; the bridge "
                          "opens its session before answering initialize",
        }

    def _session_descriptor(self):
        # The bridge reports an ios-app-session descriptor line on stderr.
        marker = "ios-app-session "
        text = bytes(self.client.stderr).decode("utf-8", "replace")
        for raw in text.splitlines():
            if marker in raw:
                return json.loads(raw.split(marker, 1)[1].strip())
        return None

    def _require_owned_simulator(self):
        descriptor = self._session_descriptor()
        context = self.context
        context.receipt_extras.update(require_owned_session_descriptor(
            descriptor, context.udid, context.bundle_id, context.app_sha256))
        self.descriptor = descriptor
        pid = descriptor.get("pid")
        if type(pid) is int and pid > 0:
            context.register_helpers(pid)

    def _advance_snapshot_id(self, snapshot, what):
        self.last_snapshot_id = require_advancing_snapshot_id(
            self.last_snapshot_id, snapshot.get("snapshotId"), what)
        return self.last_snapshot_id

    def _next_snapshot(self, what, timeout=240.0):
        snap = self._call("ui_snapshot", {"includeInvisible": False}, timeout=timeout)
        self._advance_snapshot_id(snap, what)
        return snap

    def _select_meer_node(self, snapshot, test_id, role, what):
        usable = usable_snapshot_target(snapshot, test_id, role=role)
        if not usable["usable"]:
            raise BenchmarkError(f"{what}: {usable.get('reason')}")
        return next(node for node in snapshot["nodes"]
                    if isinstance(node, dict) and node.get("testId") == test_id)

    def _poll_meer_target(self, test_id, role, timeout, what, validator=None):
        attempts, last = 0, None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            attempts += 1
            try:
                snap = self._next_snapshot(what)
                res = (validator(snap) if validator is not None
                       else usable_snapshot_target(snap, test_id, role=role))
                if res["usable"]:
                    return snap, attempts, res
                last = res.get("reason", "target not usable yet")
            except (BenchmarkError, OSError) as error:
                last = str(error)
            time.sleep(0.5)
        raise BenchmarkError(f"{what} timed out after {attempts} polls: {last}")

    def _type_meer_once(self, test_id, role, value, is_secret, what):
        acted = self._call("ui_act", {
            "action": "type", "target": {"testId": test_id}, "text": value,
        })
        if acted.get("settled") is not True:
            raise BenchmarkError(f"{what}: type action did not settle")
        snap = acted.get("snapshot") or {}
        self._advance_snapshot_id(snap, what)
        node = self._select_meer_node(snap, test_id, role, what)
        if is_secret:
            if "value" in node:
                raise BenchmarkError(f"{what}: secure field serialized a value")
            # Email readback intentionally remains visible in its own field, so
            # assert only the password secret against the post-typing snapshot.
            assert_no_secrets(acted, (value,), label=what)
            return
        if node.get("value") != value:
            observed_chars = len(str(node.get("value") or ""))
            raise BenchmarkError(
                f"{what}: readback did not match typed input "
                f"(expected_chars={len(value)}, observed_chars={observed_chars})")

    def _scroll_to_meer_once(self, test_id, what):
        acted = self._call("ui_act", {
            "action": "scroll_to", "target": {"testId": test_id}, "direction": "down",
        })
        if acted.get("settled") is not True:
            raise BenchmarkError(f"{what}: scroll_to did not settle")
        self._advance_snapshot_id(acted.get("snapshot") or {}, what)

    def _tap_meer_once(self, target, what):
        acted = self._call("ui_act", {"action": "tap", "target": target})
        if acted.get("settled") is not True:
            raise BenchmarkError(f"{what}: tap did not settle")
        snap = acted.get("snapshot") or {}
        self._advance_snapshot_id(snap, what)
        return snap

    def _capture_meer_screenshot(self, what):
        image, metadata = parse_ashnazg_screenshot(
            self._call_result("ui_screenshot", {"format": "png"}))
        if len(image) < 24 or image[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
            raise BenchmarkError(f"{what}: ui_screenshot did not return a PNG with IHDR")
        dims = list(struct.unpack_from(">II", image, 16))
        if min(dims) <= 0:
            raise BenchmarkError(f"{what}: PNG dimensions were non-positive")
        shot_id = require_advancing_snapshot_id(
            self.last_snapshot_id, metadata.get("snapshotId"), what)
        self.last_snapshot_id = shot_id
        return {"bytes": len(image), "sha256": hashlib.sha256(image).hexdigest(),
                "dimensions": dims, "snapshot_id": shot_id, "published": False}

    def _poll_home_clearing_prompts(self, timeout, quiet_streak=2.5):
        """Poll for Home while dismissing blocking system prompts in the same loop.

        Single-capture `app.snapshot()` hides underlying app controls behind
        system sheets or alerts (AutoFill 'Save Password?', notifications or
        location alerts), so prompts must be dismissed during the Home wait.
        """
        dismissed = []
        prompt_dismiss_ns = 0
        clean_since = None
        first_home_ns = None
        attempts = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            attempts += 1
            snap = self._next_snapshot("home/after-submit")
            nodes = snap.get("nodes") or []
            matched_label = next(
                (
                    label
                    for label in MEER_SYSTEM_PROMPT_LABELS
                    if any(
                        isinstance(node, dict)
                        and node.get("role") == "button"
                        and node.get("enabled") is True
                        and node.get("text") == label
                        for node in nodes
                    )
                ),
                None,
            )
            if matched_label is not None:
                tap_started = time.monotonic_ns()
                self._tap_meer_once({"text": matched_label}, f"home/dismiss-{matched_label}")
                prompt_dismiss_ns += time.monotonic_ns() - tap_started
                dismissed.append(matched_label)
                clean_since = None
                continue
            if usable_snapshot_target(snap, MEER_HOME_SEARCH, role="button")["usable"]:
                if first_home_ns is None:
                    first_home_ns = time.monotonic_ns()
                if clean_since is None:
                    clean_since = time.monotonic()
                elif time.monotonic() - clean_since >= quiet_streak:
                    return snap, attempts, first_home_ns, time.monotonic_ns(), dismissed, prompt_dismiss_ns
            else:
                clean_since = None
            time.sleep(0.5)
        raise BenchmarkError("home/after-submit timed out before Home settled prompt-free")

    def _first_meer_login_snapshot(self):
        context = self.context
        snap, attempts, usable = self._poll_meer_target(
            MEER_LOGIN_EMAIL, "textfield", context.first_snapshot_timeout,
            "login/first-state", validator=usable_meer_login_snapshot)
        return {
            "usable": usable,
            "attempts": attempts,
            "snapshot_id": snap.get("snapshotId"),
        }, None

    def _run_meer_journey(self, first_record):
        context = self.context
        email, password = context.load_credentials()
        credential_entry_start_ns = time.monotonic_ns()
        self._type_meer_once(MEER_LOGIN_EMAIL, "textfield", email, False, "login/email")
        self._scroll_to_meer_once(MEER_LOGIN_PASSWORD, "login/email-reveal")
        self._type_meer_once(MEER_LOGIN_PASSWORD, "securetextfield", password, True, "login/password")
        self._scroll_to_meer_once(MEER_LOGIN_SUBMIT, "login/password-reveal")
        submit_snap = self._next_snapshot("login/submit-selection")
        self._select_meer_node(submit_snap, MEER_LOGIN_SUBMIT, "button", "login/submit-selection")
        submit_start_ns = time.monotonic_ns()
        self._tap_meer_once({"testId": MEER_LOGIN_SUBMIT}, "login/submit")
        (
            settled_home_snap,
            home_attempts,
            home_end_ns,
            settled_home_ns,
            dismissed_prompts,
            prompt_dismiss_ns,
        ) = self._poll_home_clearing_prompts(context.first_snapshot_timeout)
        context.sample_live_stage("submit_to_home")

        home_ss_start_ns = time.monotonic_ns()
        home_screenshot = self._capture_meer_screenshot("home/screenshot-milestone")
        home_ss_end_ns = time.monotonic_ns()

        prompt_start_ns = home_end_ns
        prompt_end_ns = settled_home_ns
        prompt_elapsed_ns = (prompt_end_ns - prompt_start_ns) + prompt_dismiss_ns

        self._select_meer_node(settled_home_snap, MEER_HOME_SEARCH, "button", "search/open-tap")
        search_start_ns = time.monotonic_ns()
        self._tap_meer_once({"testId": MEER_HOME_SEARCH}, "search/open-tap")
        search_snap, search_attempts, _ = self._poll_meer_target(
            MEER_SEARCH_INPUT, "textfield", context.first_snapshot_timeout, "search/after-tap")
        search_end_ns = time.monotonic_ns()
        context.sample_live_stage("home_to_search")

        self._select_meer_node(search_snap, MEER_SEARCH_BACK, "button", "home-return/back-tap")
        return_start_ns = time.monotonic_ns()
        self._tap_meer_once({"testId": MEER_SEARCH_BACK}, "home-return/back-tap")
        _, return_attempts, _ = self._poll_meer_target(
            MEER_HOME_SEARCH, "button", context.first_snapshot_timeout, "home-return/after-tap")
        return_end_ns = time.monotonic_ns()
        final_screenshot = self._capture_meer_screenshot("home-return/screenshot-milestone")
        final_ss_end_ns = time.monotonic_ns()

        milestones = {
            "login_to_home": {
                "elapsed_ns": home_end_ns - credential_entry_start_ns,
                "ms": round((home_end_ns - credential_entry_start_ns) / 1_000_000, 3),
                "boundaries": "email entry start through first Home snapshot with usable home-search-button",
            },
            "submit_to_home": {
                "elapsed_ns": home_end_ns - submit_start_ns,
                "ms": round((home_end_ns - submit_start_ns) / 1_000_000, 3),
                "boundaries": "login-submit-button tap request through first Home snapshot with usable home-search-button (includes clearing any blocking system overlays before app.snapshot() can read Home)",
                "attempts": home_attempts,
                "prompt_dismiss_ms": round(prompt_dismiss_ns / 1_000_000, 3),
            },
            "home_screenshot": {
                "elapsed_ns": home_ss_end_ns - home_ss_start_ns,
                "ms": round((home_ss_end_ns - home_ss_start_ns) / 1_000_000, 3),
                "boundaries": "settled Home confirmed -> Home ui_screenshot PNG validated in memory",
            },
            "prompt_settle": {
                "elapsed_ns": prompt_elapsed_ns,
                "ms": round(prompt_elapsed_ns / 1_000_000, 3),
                "dismissed": dismissed_prompts,
                "boundaries": "system-prompt dismissal taps plus 2.5s prompt-free Home quiet check",
            },
            "home_to_search": {
                "elapsed_ns": search_end_ns - search_start_ns,
                "ms": round((search_end_ns - search_start_ns) / 1_000_000, 3),
                "boundaries": "home-search-button tap request through usable search-input snapshot",
                "attempts": search_attempts,
            },
            "search_to_home": {
                "elapsed_ns": return_end_ns - return_start_ns,
                "ms": round((return_end_ns - return_start_ns) / 1_000_000, 3),
                "boundaries": "search-back-button tap request through usable home-search-button snapshot",
                "attempts": return_attempts,
            },
            "final_home_screenshot": {
                "elapsed_ns": final_ss_end_ns - return_end_ns,
                "ms": round((final_ss_end_ns - return_end_ns) / 1_000_000, 3),
                "boundaries": "returned Home confirmed -> final Home ui_screenshot PNG validated in memory",
            },
            "search_to_home_with_screenshot": {
                "elapsed_ns": final_ss_end_ns - return_start_ns,
                "ms": round((final_ss_end_ns - return_start_ns) / 1_000_000, 3),
                "boundaries": "search-back-button tap request through usable home-search-button plus fresh Home PNG",
            },
        }
        context.timings["meer_milestones"] = milestones
        context.receipt_extras["meer"] = {
            "navigation": "visible-controls",
            "system_prompts_dismissed": dismissed_prompts,
            "home_screenshot": home_screenshot,
            "final_home_screenshot": final_screenshot,
            "checkpoints": [
                {"milestone": name, "sequence": index, "status": "sampled"}
                for index, name in enumerate(MEER_MILESTONES, 1)
            ],
        }
        return {
            "kind": "meer",
            "native_elapsed_ns": final_ss_end_ns - return_start_ns,
            "search_to_home_ns": return_end_ns - return_start_ns,
            "boundaries": {
                "first_snapshot_id": first_record.get("snapshot_id") or 0,
                "final_snapshot_id": self.last_snapshot_id,
            },
            "home_screenshot": home_screenshot,
            "final_home_screenshot": final_screenshot,
        }, None

    def first_usable_snapshot(self):
        context = self.context
        if getattr(context, "workload", "fixture") == "meer":
            return self._first_meer_login_snapshot()
        attempts, last = 0, None
        deadline = time.monotonic() + context.first_snapshot_timeout
        while time.monotonic() < deadline:
            attempts += 1
            try:
                snapshot = self._call("ui_snapshot", {"includeInvisible": False})
                usable = usable_snapshot_target(snapshot, context.tap_identifier)
                if usable["usable"]:
                    require_advancing_snapshot_id(
                        0, snapshot.get("snapshotId"), "the first usable snapshot")
                    if context.expected_before and not any(
                            context.expected_before in str(node.get("text") or "")
                            for node in snapshot["nodes"]):
                        last = "the expected pre-action state was not in the snapshot yet"
                    else:
                        (context.output / "first-snapshot.json").write_text(
                            json.dumps(snapshot, indent=2, sort_keys=True))
                        targets = [node for node in snapshot["nodes"]
                                   if node.get("testId") == context.tap_identifier]
                        return {
                            "usable": usable, "attempts": attempts,
                            "snapshot_id": snapshot.get("snapshotId"),
                            "target_value": targets[0].get("text"),
                        }, None
                else:
                    last = usable.get("reason", "the snapshot was not usable yet")
            except (BenchmarkError, OSError) as error:
                last = str(error)
            time.sleep(0.5)
        return {"attempts": attempts, "last_reason": sanitize(last)[:500]}, BenchmarkError(
            f"no usable snapshot before the timeout; last reason: {last}")

    def tap_to_fresh_frame(self, first_record):
        context = self.context
        if getattr(context, "workload", "fixture") == "meer":
            return self._run_meer_journey(first_record)
        first_id = first_record.get("snapshot_id") or 0
        acted = self._call("ui_act", {"action": "tap",
                                      "target": {"testId": context.tap_identifier}})
        if acted.get("settled") is not True:
            raise BenchmarkError("the tap did not settle")
        state = acted.get("snapshot") or {}
        tap_id = require_advancing_snapshot_id(
            first_id, state.get("snapshotId"), "the post-tap snapshot")
        texts = " ".join(str(node.get("text") or "")
                         for node in state.get("nodes") or [])
        if context.expected_after not in texts:
            raise BenchmarkError("the tap did not produce the expected semantic change")
        if context.expected_before and context.expected_before in texts:
            raise BenchmarkError("the pre-action state was still present after the tap")
        # The pinned ui_screenshot result carries its image and metadata in
        # content blocks, not structuredContent (src/mcp.rs).
        image, metadata = parse_ashnazg_screenshot(
            self._call_result("ui_screenshot", {"format": "png"}))
        frame_id = require_advancing_snapshot_id(
            tap_id, metadata.get("snapshotId"), "the fresh frame screenshot")
        return {"action": "tap", "target": {"testId": context.tap_identifier},
                "boundaries": {"first_snapshot_id": first_id,
                               "post_tap_snapshot_id": tap_id,
                               "fresh_frame_snapshot_id": frame_id},
                "screenshot": artifact_record(image, context.output / "after-tap.png")}, None

    def shutdown(self):
        return {"mode": "stdio-eof", "bridge_exit_code": _eof_teardown(self.process)}


DIRECT_PROBE_CASE = "SimulatorProbeTests/testObserveTapObserveAndCapture()"
DIRECT_ATTACHMENTS = {
    "fixture-observation-boundaries": "xctest-boundaries.json",
    "before-increment": "first-screenshot.png",
    "after-increment": "after-tap.png",
}


def export_xctest_attachments(result_bundle, output, udid, test_id, names, optional_names=()):
    """Return one exact test's matching, bounded, successful simulator attachments."""
    if not result_bundle.is_dir():
        raise BenchmarkError("the direct XCTest runner did not create its result bundle")
    all_names = list(names) + [n for n in optional_names if n not in names]
    with tempfile.TemporaryDirectory(dir=output) as directory:
        exported = Path(directory)
        subprocess.run(
            ["xcrun", "xcresulttool", "export", "attachments",
             "--path", str(result_bundle), "--output-path", directory,
             "--test-id", test_id],
            check=True, capture_output=True, timeout=60,
        )
        try:
            manifest = json.loads(read_text_bounded(exported / "manifest.json"))
        except ValueError as error:
            raise BenchmarkError("xcresult attachment manifest is invalid JSON") from error
        if not isinstance(manifest, list):
            raise BenchmarkError("xcresult attachment manifest must contain test entries")
        files = {}
        for test in manifest:
            if not isinstance(test, dict) or test.get("testIdentifier") != test_id:
                continue
            attachments = test.get("attachments")
            if not isinstance(attachments, list):
                raise BenchmarkError("xcresult test did not report attachments")
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                suggested = attachment.get("suggestedHumanReadableName")
                if not isinstance(suggested, str):
                    continue
                for name in all_names:
                    # Xcode 27.2 xcresulttool exports attachments as <name>_<index>_<UUID>.<ext>.
                    if not re.fullmatch(
                        re.escape(name)
                        + r"(?:_\d+)?(?:_[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})?(?:\.[A-Za-z0-9]+)?",
                        suggested,
                    ):
                        continue
                    filename = attachment.get("exportedFileName")
                    device = attachment.get("deviceId")
                    if (name in files or not isinstance(filename, str)
                            or not filename or Path(filename).name != filename
                            or filename in (".", "..") or not isinstance(device, str)
                            or device.lower() != udid.lower()
                            or attachment.get("isAssociatedWithFailure") is not False):
                        raise BenchmarkError("xcresult attachment identity or device did not match")
                    path = exported / filename
                    if path.is_symlink() or not path.is_file():
                        raise BenchmarkError("xcresult attachment was not a regular exported file")
                    with path.open("rb") as stream:
                        data = stream.read(CAPTURE_LIMIT + 1)
                    if not data or len(data) > CAPTURE_LIMIT:
                        raise BenchmarkError("xcresult attachment exceeded its size bounds")
                    files[name] = data
        if not set(names).issubset(files):
            raise BenchmarkError("xcresult did not include one set of native boundary attachments")
        return files


MEER_DIRECT_CASE = "MeerJourneyBenchmarkTests/testPinnedJourneyThroughVisibleControls()"
MEER_DIRECT_ATTACHMENT = "meer-journey-boundaries"
MEER_DIRECT_CAPTURES = ("home-screenshot", "final-home-screenshot")


def read_meer_xctest_attachment(result_bundle, output, udid):
    files = export_xctest_attachments(
        result_bundle, output, udid, MEER_DIRECT_CASE, (MEER_DIRECT_ATTACHMENT,),
    )
    try:
        return json.loads(files[MEER_DIRECT_ATTACHMENT])
    except ValueError as error:
        raise BenchmarkError("Meer XCTest boundary attachment is invalid JSON") from error


def validate_meer_png_bytes(data, expected_pixels, what):
    """Validate an in-memory PNG screenshot without writing Meer captures to disk."""
    if len(data) < 24 or data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
        raise BenchmarkError(f"{what}: XCTest attachment was not a PNG with IHDR")
    dims = list(struct.unpack_from(">II", data, 16))
    if dims != expected_pixels or min(dims) <= 0:
        raise BenchmarkError(f"{what}: XCTest PNG extent did not match boundary pixels")
    return {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "dimensions": dims,
        "published": False,
    }


def read_meer_xctest_captures(result_bundle, output, udid, names=MEER_DIRECT_CAPTURES):
    return export_xctest_attachments(result_bundle, output, udid, MEER_DIRECT_CASE, tuple(names))


def read_direct_xctest_evidence(result_bundle, context):
    """Qualify the fixture test's own clock and frames from this invocation."""
    files = export_xctest_attachments(
        result_bundle, context.output, context.udid, DIRECT_PROBE_CASE, DIRECT_ATTACHMENTS,
    )
    try:
        bounds = json.loads(files["fixture-observation-boundaries"])
    except ValueError as error:
        raise BenchmarkError("XCTest boundary attachment is invalid JSON") from error
    if (not isinstance(bounds, dict) or bounds.get("schema") != "simulator-probe-boundaries"
            or type(bounds.get("version")) is not int or bounds["version"] != 1
            or bounds.get("clock") != "dispatch-uptime-ns"
            or bounds.get("captureExtent") != "app"
            or bounds.get("preCapture") != "before-increment"
            or bounds.get("postCapture") != "after-increment"):
        raise BenchmarkError("XCTest boundary attachment did not match the fixture contract")
    clocks = [bounds.get(key) for key in (
        "firstUsableStartUptimeNs", "firstUsableEndUptimeNs",
        "tapStartUptimeNs", "tapEndUptimeNs",
    )]
    if not all(type(value) is int and value >= 0 for value in clocks):
        raise BenchmarkError("XCTest boundary clock fields are invalid")
    first_start, first_end, tap_start, tap_end = clocks
    if not first_start < first_end <= tap_start < tap_end:
        raise BenchmarkError("XCTest boundary clocks are out of order")
    before, after = files["before-increment"], files["after-increment"]
    if before == after:
        raise BenchmarkError("XCTest repeated the pre-tap PNG")
    pixels = bounds.get("capturePixels")
    for png in (before, after):
        if (len(png) < 24 or png[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                or list(struct.unpack_from(">II", png, 16)) != pixels
                or not all(pixels)):
            raise BenchmarkError("XCTest PNG extent did not match the boundary attachment")
    for name, output_name in DIRECT_ATTACHMENTS.items():
        artifact_record(files[name], context.output / output_name)
    return {
        "first_ns": first_end - first_start, "tap_ns": tap_end - tap_start,
        "capture_pixels": pixels, "capture_extent": "app",
    }


class DirectXctestArm:
    """Direct XCTest through a configured runner command.

    The native fixture test supplies semantic assertions and its in-test
    clock. The parent measures runner wait and attachment export separately.
    """

    name = "direct-xctest"
    first_snapshot_boundaries = (
        "native app.launch dispatch to the fixture's asserted enabled, "
        "hittable target with finite visible bounds")

    def __init__(self, context):
        self.context = context
        self.process = None
        self.result_bundle = None
        self.evidence = None
        self.checkpoint_server = None

    def _close_checkpoint_server(self):
        server = self.checkpoint_server
        if server is None:
            return {}
        self.checkpoint_server = None
        server.__exit__(None, None, None)
        return dict(server.records)

    def start(self):
        context = self.context
        began = time.monotonic_ns()
        result_bundle = (context.output / "trial.xcresult").resolve()
        if "{xcresult}" in context.arm_command:
            if result_bundle.exists() or result_bundle.is_symlink():
                raise BenchmarkError("refusing to reuse an XCTest result bundle")
            self.result_bundle = result_bundle
        command = [part.format(udid=context.udid, xcresult=result_bundle)
                   for part in context.arm_command]
        env = dict(os.environ)
        if getattr(context, "workload", "fixture") == "meer":
            email, password = context.load_credentials()
            initial_url = validate_loopback_http_url(context.initial_url)
            self.checkpoint_server = MeerCheckpointServer(
                context.memory, context.udid, context.app_bundle_name, context.helper_pids)
            self.checkpoint_server.__enter__()
            env.update({
                "TEST_RUNNER_ASHNAZG_MEER_JOURNEY": "1",
                "TEST_RUNNER_ASHNAZG_MEER_BUNDLE_ID": context.bundle_id,
                "TEST_RUNNER_ASHNAZG_MEER_INITIAL_URL": initial_url,
                "TEST_RUNNER_ASHNAZG_MEER_MOCK_EMAIL": email,
                "TEST_RUNNER_ASHNAZG_MEER_MOCK_PASSWORD": password,
                "TEST_RUNNER_ASHNAZG_MEER_CHECKPOINT_PORT": str(self.checkpoint_server.port),
            })
        try:
            with (context.output / "xctest.log").open("wb") as log:
                self.process = subprocess.Popen(
                    command, stdout=log, stderr=subprocess.STDOUT, env=env)
        except Exception:
            self._close_checkpoint_server()
            raise
        context.timings["arm_start"] = {
            "status": "passed", "elapsed_ns": time.monotonic_ns() - began,
            "boundaries": "runner spawn; the runner's own session is inside the "
                          "first-usable-snapshot interval for this arm",
        }
        context.register_helpers(self.process.pid)

    def _read_meer_evidence(self, checkpoint_records):
        context = self.context
        if self.result_bundle is None:
            raise BenchmarkError("direct-xctest Meer workload requires -resultBundlePath {xcresult}")
        captures = {}
        try:
            attachment = read_meer_xctest_attachment(
                self.result_bundle, context.output, context.udid)
            assert_no_secrets(attachment, context.secrets, label="meer-journey-boundaries")
            artifact_record(
                json.dumps(attachment, indent=2, sort_keys=True).encode("utf-8") + b"\n",
                context.output / "meer-journey-boundaries.json",
            )
            qualified = qualify_meer_xctest(attachment, checkpoint_records, context.bundle_id)
            if "homeCapture" in attachment and "finalHomeCapture" in attachment:
                raw_captures = read_meer_xctest_captures(
                    self.result_bundle,
                    context.output,
                    context.udid,
                    (attachment["homeCapture"], attachment["finalHomeCapture"]),
                )
                pixels = attachment["capturePixels"]
                captures["home_screenshot"] = validate_meer_png_bytes(
                    raw_captures[attachment["homeCapture"]], pixels, "home-screenshot")
                captures["final_home_screenshot"] = validate_meer_png_bytes(
                    raw_captures[attachment["finalHomeCapture"]], pixels, "final-home-screenshot")
        finally:
            shutil.rmtree(self.result_bundle, ignore_errors=True)
        first_ns = attachment["firstValidLoginEndUptimeNs"] - attachment["firstValidLoginStartUptimeNs"]
        submit_ns = attachment["submitToHomeEndUptimeNs"] - attachment["submitToHomeStartUptimeNs"]
        search_ns = attachment["searchEndUptimeNs"] - attachment["searchStartUptimeNs"]
        home_return_ns = attachment["homeReturnEndUptimeNs"] - attachment["homeReturnStartUptimeNs"]
        milestones = {
            "first_valid_login": {
                "elapsed_ns": first_ns,
                "ms": round(first_ns / 1_000_000, 3),
                "boundaries": "native app.launch() through verified unique login-email-input and empty login-password-input",
            },
            "submit_to_home": {
                "elapsed_ns": submit_ns,
                "ms": round(submit_ns / 1_000_000, 3),
                "boundaries": "native login-submit-button tap through verified home-search-button",
            },
            "home_to_search": {
                "elapsed_ns": search_ns,
                "ms": round(search_ns / 1_000_000, 3),
                "boundaries": "native home-search-button tap through verified search-input",
            },
            "search_to_home": {
                "elapsed_ns": home_return_ns,
                "ms": round(home_return_ns / 1_000_000, 3),
                "boundaries": "native search-back-button tap through verified home-search-button (no screenshot capture)",
            },
        }
        if "credentialEntryStartUptimeNs" in attachment:
            login_ns = attachment["submitToHomeEndUptimeNs"] - attachment["credentialEntryStartUptimeNs"]
            milestones["login_to_home"] = {
                "elapsed_ns": login_ns,
                "ms": round(login_ns / 1_000_000, 3),
                "boundaries": "email entry start through verified home-search-button",
            }
        tap_ns = home_return_ns
        if captures:
            home_ss_ns = (
                attachment["homeScreenshotEndUptimeNs"] - attachment["homeScreenshotStartUptimeNs"])
            final_ss_ns = (
                attachment["finalHomeScreenshotEndUptimeNs"] - attachment["homeReturnEndUptimeNs"])
            search_with_ss_ns = (
                attachment["finalHomeScreenshotEndUptimeNs"] - attachment["homeReturnStartUptimeNs"])
            milestones["home_screenshot"] = {
                "elapsed_ns": home_ss_ns,
                "ms": round(home_ss_ns / 1_000_000, 3),
                "boundaries": "settled Home confirmed -> Home app.screenshot() PNG validated in memory",
            }
            milestones["final_home_screenshot"] = {
                "elapsed_ns": final_ss_ns,
                "ms": round(final_ss_ns / 1_000_000, 3),
                "boundaries": "returned Home confirmed -> final Home app.screenshot() PNG validated in memory",
            }
            milestones["search_to_home_with_screenshot"] = {
                "elapsed_ns": search_with_ss_ns,
                "ms": round(search_with_ss_ns / 1_000_000, 3),
                "boundaries": "native search-back-button tap through verified home-search-button plus fresh Home PNG",
            }
            tap_ns = search_with_ss_ns
        context.timings["meer_milestones"] = milestones
        meer_extra = {
            "navigation": "visible-controls",
            "qualified": qualified,
            "checkpoints": attachment.get("checkpoints"),
        }
        if captures:
            meer_extra.update(captures)
        context.receipt_extras["meer"] = meer_extra
        self.evidence = {
            "kind": "meer",
            "first_ns": first_ns,
            "tap_ns": tap_ns,
            "search_to_home_ns": home_return_ns,
            "milestones": milestones,
            "qualified": qualified,
            **captures,
        }
        if captures:
            self.evidence["capture_extent"] = attachment["captureExtent"]
            self.evidence["capture_pixels"] = attachment["capturePixels"]
        return self.evidence

    def first_usable_snapshot(self):
        context = self.context
        is_meer = getattr(context, "workload", "fixture") == "meer"
        deadline = time.monotonic() + context.first_snapshot_timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.5)
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=30)
            self._close_checkpoint_server()
            if is_meer and self.result_bundle is not None:
                shutil.rmtree(self.result_bundle, ignore_errors=True)
            return {}, BenchmarkError("the runner exceeded the first-snapshot timeout")
        checkpoint_records = self._close_checkpoint_server()
        exit_code = self.process.poll()
        log = scrub_secrets(
            (context.output / "xctest.log").read_text(errors="replace"), context.secrets)
        durations, failures = parse_test_case_durations(log)
        context.receipt_extras["runner_durations"] = durations
        context.receipt_extras["runner_failures"] = failures
        context.receipt_extras["runner_exit_code"] = exit_code
        if exit_code != 0:
            if is_meer and self.result_bundle is not None:
                shutil.rmtree(self.result_bundle, ignore_errors=True)
            return {"durations": durations, "exit_code": exit_code,
                    "log_tail": scrub_secrets(log[-2000:], context.secrets)}, BenchmarkError(
                f"the runner exited with {exit_code}; the failing test log is "
                "retained and the observation is unqualified")
        if failures:
            if is_meer and self.result_bundle is not None:
                shutil.rmtree(self.result_bundle, ignore_errors=True)
            return {"durations": durations}, BenchmarkError(
                f"the runner reported failing test cases: {[row['name'] for row in failures]}")
        if not durations:
            if is_meer and self.result_bundle is not None:
                shutil.rmtree(self.result_bundle, ignore_errors=True)
            return {}, BenchmarkError("the runner reported no passing test-case durations")
        context.receipt_extras["app_identity_basis"] = (
            "the runner's own target-app assertions")
        if is_meer:
            evidence = self._read_meer_evidence(checkpoint_records)
            return {
                "durations": durations,
                "native_elapsed_ns": evidence["first_ns"],
                "meer_qualified": evidence["qualified"],
            }, None
        if self.result_bundle is not None:
            workload = (context.bundle_id, context.tap_identifier,
                        context.expected_before, context.expected_after)
            if workload != ("dev.ashnazg.ios.fixture", "fixture-increment", "Count: 0", "Count: 1"):
                raise BenchmarkError("the direct XCTest attachment qualifies only the counter fixture")
            self.evidence = read_direct_xctest_evidence(self.result_bundle, context)
            return {"durations": durations, "native_elapsed_ns": self.evidence["first_ns"]}, None
        return {"durations": durations}, None

    def _declared_captures(self):
        declared = self.context.xctest_artifacts
        if declared is None or not Path(declared).is_dir():
            return {"present": False}
        captures, invalid = {}, []
        for png in sorted(Path(declared).glob("*.png"))[:64]:
            data = png.read_bytes()
            entry = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                entry["invalid"] = "not a PNG"
                invalid.append(png.name)
            captures[png.name] = entry
        return {"present": True, "pngs": captures, "invalid_pngs": invalid}

    def tap_to_fresh_frame(self, first_record):
        if self.evidence is not None:
            if self.evidence.get("kind") == "meer":
                row = {
                    "kind": "meer",
                    "native_elapsed_ns": self.evidence["tap_ns"],
                    "search_to_home_ns": self.evidence["search_to_home_ns"],
                    "milestones": self.evidence["milestones"],
                    "memory_complete": self.evidence["qualified"]["memory_complete"],
                    "missing_memory_milestones": self.evidence["qualified"]["missing_memory_milestones"],
                }
                for key in ("home_screenshot", "final_home_screenshot", "capture_extent", "capture_pixels"):
                    if key in self.evidence:
                        row[key] = self.evidence[key]
                return row, None
            return {
                "native_elapsed_ns": self.evidence["tap_ns"],
                "capture_pixels": self.evidence["capture_pixels"],
                "capture_extent": self.evidence["capture_extent"],
                "semantic_change": [self.context.expected_before, self.context.expected_after],
            }, None
        context = self.context
        qualification = {"comparable": False,
                          "required_for_comparable": DIRECT_XCTEST_TAP_QUALIFICATION,
                          "declared_captures": self._declared_captures()}
        context.receipt_extras["tap_qualification"] = qualification
        return {"equivalent_frame": "unqualified", **qualification}, BenchmarkError(
            "no real fresh capture and equivalent measurement boundary was "
            "observed; whole-test success never qualifies the fresh-frame "
            "row, so this sample is retained as unqualified")

    def shutdown(self):
        self._close_checkpoint_server()
        return {"mode": "runner-exit",
                "exit_code": self.process.poll() if self.process else None}


ARM_CLASSES = {AppleMcpArm.name: AppleMcpArm,
               AshnazgMcpArm.name: AshnazgMcpArm,
               DirectXctestArm.name: DirectXctestArm}


def apple_tool_caller(client):
    import itertools

    ids = itertools.count(20)

    def call(name, arguments):
        try:
            response = client.request({
                "jsonrpc": "2.0", "id": next(ids), "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }, timeout=180)
        except AcceptanceError as error:
            raise InventoryError(f"{name} failed before a reply: {error}") from error
        result = response.get("result", {})
        if "error" in response or result.get("isError"):
            raise InventoryError(f"{name} rejected: {response.get('error', result.get('content'))}")
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            raise InventoryError(f"{name} did not return structured output")
        return payload

    return call


class ObservationContext:
    """Everything one arm observation needs; shared across the driver."""

    def __init__(self, args, udid, condition, output, arm_name):
        self.udid = udid
        self.condition = condition
        self.output = output
        self.arm_name = arm_name
        self.bundle_id = args.bundle_id
        self.app_path = Path(args.app)
        self.app_bundle_name = Path(args.app).stem
        self.tap_identifier = args.tap_identifier
        self.tap_label = args.tap_label
        self.expected_before = args.expected_before
        self.expected_after = args.expected_after
        self.first_snapshot_timeout = args.first_snapshot_timeout
        self.arm_command = shlex.split(args.arm_command) if args.arm_command else []
        self.xctest_artifacts = args.xctest_artifacts
        self.ashnazg_manifest = getattr(args, "ashnazg_manifest_template", None)
        self.workload = getattr(args, "workload", "fixture")
        self.credentials_path = getattr(args, "credentials", None)
        self.initial_url = getattr(args, "initial_url", None)
        self._loaded_credentials = None
        self.simulator_scope = {}
        self.app_sha256 = None
        self.app_sha256_basis = None
        self.arm_command_scope = list(self.arm_command)
        self.source_revisions = {}
        self.timings = {}
        self.receipt_extras = {}
        self.helper_pids = []
        self.memory = None

    @property
    def secrets(self):
        return self._loaded_credentials or ()

    def load_credentials(self):
        if self._loaded_credentials is None:
            if not self.credentials_path:
                raise BenchmarkError("Meer workload requires --credentials")
            self._loaded_credentials = load_mock_credentials(self.credentials_path)
        return self._loaded_credentials

    def sample_live_stage(self, stage):
        if self.memory is not None:
            self.memory.sample_stage(stage, self.udid, self.app_bundle_name, self.helper_pids)

    def register_helpers(self, *pids):
        for pid in pids:
            if pid not in self.helper_pids:
                self.helper_pids.append(pid)


def _eof_teardown(process, grace=30.0):
    """Close the owned helper's stdin and join it; terminate on expiry."""
    if process is None or process.poll() is not None:
        return process.poll() if process is not None else None
    try:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
    except OSError:
        pass
    try:
        return process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait(timeout=5)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _wait_helpers_gone(pids, timeout=60.0):
    deadline = time.monotonic() + timeout
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if _pid_alive(pid)]
        if remaining:
            time.sleep(0.5)
    return not remaining


def run_observation(arm_name, context):
    """Drive one cold-or-warm observation of one arm against the owned sim."""
    context.memory = MemoryTracker()
    is_meer = getattr(context, "workload", "fixture") == "meer"
    if is_meer and getattr(context, "credentials_path", None) is not None:
        context.load_credentials()
    arm = ARM_CLASSES[arm_name](context)
    arm_shutdown = {"requested": False, "simulator_shutdown": False,
                    "owned_helpers_gone": False, "owned_helper_pids": []}
    status = "failed"
    error = None
    started = time.monotonic()
    scope = context.simulator_scope
    if scope.get("created_by_run") is not True or scope.get("udid") != context.udid:
        return build_observation_receipt(
            context, arm_shutdown, status,
            BenchmarkError("refusing to act on a simulator outside this run"),
            time.monotonic() - started,
        )
    try:
        arm.start()
        context.memory.sample_stage(MEMORY_STAGES[0], context.udid,
                                    context.app_bundle_name, context.helper_pids)
        began = time.monotonic_ns()
        first_record, first_error = {}, None
        try:
            first_record, first_error = arm.first_usable_snapshot()
        except (BenchmarkError, InventoryError, AcceptanceError, OSError,
                subprocess.SubprocessError) as caught:
            first_error = caught
        finish_operation(context.timings, "first_usable_snapshot", began,
                         time.monotonic_ns(), first_error)
        first_timing = context.timings["first_usable_snapshot"]
        first_timing["boundaries"] = arm.first_snapshot_boundaries
        if arm_name == "direct-xctest" and first_error is None:
            native = first_record.get("native_elapsed_ns")
            if native is None:
                skip_operation(context.timings, "first_usable_snapshot",
                               "no in-test XCTest first-usable boundary")
            else:
                first_timing.update(
                    parent_elapsed_ns=first_timing["elapsed_ns"], elapsed_ns=native,
                    success_timing_ns=native, success_clock="dispatch-uptime-ns",
                )
        elif arm_name == "ashnazg-mcp" and is_meer and first_error is None:
            context.timings.setdefault("meer_milestones", {})["first_valid_login"] = {
                "elapsed_ns": first_timing["elapsed_ns"],
                "ms": round(first_timing["elapsed_ns"] / 1_000_000, 3),
                "boundaries": (
                    "first ui_snapshot request after ios-app-session ready -> first snapshot "
                    "with usable login-email-input and empty login-password-input "
                    "(launch and readySelector wait sit inside arm_start)"
                ),
            }
        if first_record:
            context.receipt_extras["first_snapshot"] = {
                key: value for key, value in first_record.items()
                if key in ("usable", "attempts", "last_reason", "app_identity_basis",
                           "target_value", "durations", "snapshot_id",
                           "exit_code", "log_tail", "meer_qualified")
            }
        if arm_name == "direct-xctest" and is_meer:
            live_first = list(context.memory.stages.get("first_valid_login") or [])
            context.memory.stages[MEMORY_STAGES[1]] = live_first
            if not live_first:
                context.receipt_extras.setdefault("missing_memory_stages", {})[MEMORY_STAGES[1]] = (
                    "the Meer XCTest checkpoint did not record a first_valid_login memory sample")
        elif arm_name == "direct-xctest":
            context.memory.stages[MEMORY_STAGES[1]] = []
            context.receipt_extras.setdefault("missing_memory_stages", {})[MEMORY_STAGES[1]] = (
                "the runner did not expose a live memory sampling checkpoint")
        else:
            context.memory.sample_stage(MEMORY_STAGES[1], context.udid,
                                        context.app_bundle_name, context.helper_pids)
            if is_meer:
                context.memory.stages["first_valid_login"] = list(
                    context.memory.stages[MEMORY_STAGES[1]])
        if first_error is None:
            began = time.monotonic_ns()
            tap_record, tap_error = {}, None
            try:
                tap_record, tap_error = arm.tap_to_fresh_frame(first_record)
            except (BenchmarkError, InventoryError, AcceptanceError, OSError,
                    subprocess.SubprocessError) as caught:
                tap_error = caught
            finish_operation(context.timings, "tap_to_fresh_frame", began,
                             time.monotonic_ns(), tap_error)
            tap_timing = context.timings["tap_to_fresh_frame"]
            tap_timing["boundaries"] = TAP_BOUNDARIES
            if arm_name == "direct-xctest" and tap_error is None:
                native = tap_record["native_elapsed_ns"]
                tap_timing.update(
                    parent_elapsed_ns=tap_timing["elapsed_ns"], elapsed_ns=native,
                    success_timing_ns=native, success_clock="dispatch-uptime-ns",
                )
                if is_meer:
                    tap_timing["boundaries"] = (
                        "native search-back-button tap through verified home-search-button "
                        "(no screenshot capture; per-milestone clocks in timings.meer_milestones)"
                    )
            elif arm_name == "ashnazg-mcp" and is_meer and tap_error is None:
                native = tap_record["native_elapsed_ns"]
                tap_timing.update(
                    parent_elapsed_ns=tap_timing["elapsed_ns"], elapsed_ns=native,
                    success_timing_ns=native, success_clock="monotonic-ns",
                    boundaries=(
                        "search-back-button tap through verified home-search-button plus fresh "
                        "Home ui_screenshot PNG (full milestone clocks in timings.meer_milestones)"
                    ),
                )
            if tap_record:
                context.receipt_extras["tap"] = tap_record
        else:
            skip_operation(context.timings, "tap_to_fresh_frame",
                           "the first usable snapshot did not qualify an element")
        if arm_name == "direct-xctest" and is_meer:
            live_tap = list(context.memory.stages.get("search_to_home") or [])
            context.memory.stages[MEMORY_STAGES[2]] = live_tap
            if not live_tap:
                context.receipt_extras.setdefault("missing_memory_stages", {})[MEMORY_STAGES[2]] = (
                    "the Meer XCTest checkpoint did not record a search_to_home memory sample")
        elif arm_name == "direct-xctest":
            context.memory.stages[MEMORY_STAGES[2]] = []
            context.receipt_extras.setdefault("missing_memory_stages", {})[MEMORY_STAGES[2]] = (
                "the runner did not expose a live memory sampling checkpoint")
        else:
            context.memory.sample_stage(MEMORY_STAGES[2], context.udid,
                                        context.app_bundle_name, context.helper_pids)
            if is_meer:
                context.memory.stages["search_to_home"] = list(
                    context.memory.stages[MEMORY_STAGES[2]])
        shutdown_started_ns = time.monotonic_ns()
        arm_shutdown.update(arm.shutdown(), requested=True,
                            owned_helper_pids=list(context.helper_pids))
        if device_state(context.udid) != "Shutdown":
            simctl("shutdown", context.udid)
        arm_shutdown["owned_helpers_gone"] = _wait_helpers_gone(context.helper_pids)
        sim_state = device_state(context.udid)
        arm_shutdown["simulator_state_after"] = sim_state
        arm_shutdown["simulator_shutdown"] = sim_state == "Shutdown"
        arm_shutdown["elapsed_ns"] = time.monotonic_ns() - shutdown_started_ns
        context.memory.sample_stage(MEMORY_STAGES[3], context.udid,
                                    context.app_bundle_name, context.helper_pids)
        if not arm_shutdown["owned_helpers_gone"]:
            raise BenchmarkError("owned helpers were still alive after the requested teardown")
        if not arm_shutdown["simulator_shutdown"]:
            raise BenchmarkError(f"the owned simulator was not shut down (state {sim_state})")
        if arm_shutdown.get("end_session") == "failed":
            raise BenchmarkError("Apple MCP did not acknowledge session cleanup")
        if arm_name == "ashnazg-mcp" and arm_shutdown.get("bridge_exit_code") != 0:
            raise BenchmarkError("the Ashnazg bridge exited without verifying clean teardown")
        if context.timings["first_usable_snapshot"]["status"] != "passed":
            raise BenchmarkError("the first-usable-snapshot operation did not pass")
        if context.timings["tap_to_fresh_frame"]["status"] != "passed":
            raise BenchmarkError("the tap-to-fresh-frame operation did not pass")
        status = "passed"
    except (BenchmarkError, InventoryError, AcceptanceError, OSError,
            subprocess.SubprocessError) as caught:
        error = caught
    finally:
        if "simulator_state_after" not in arm_shutdown:
            try:
                state = device_state(context.udid)
                arm_shutdown["simulator_state_after"] = state
                arm_shutdown["simulator_shutdown"] = state == "Shutdown"
            except (OSError, subprocess.SubprocessError):
                pass
        if not arm_shutdown.get("requested"):
            try:
                arm.shutdown()
            except Exception:  # noqa: BLE001 - teardown must never mask the real error
                pass
            _wait_helpers_gone(context.helper_pids)
    return build_observation_receipt(context, arm_shutdown, status, error,
                                     time.monotonic() - started)


def build_observation_receipt(context, shutdown, status, error, elapsed):
    artifacts = {}
    for path in sorted(context.output.glob("*")):
        if path.is_file() and path.suffix in (".png", ".txt", ".json", ".log"):
            data = path.read_bytes()
            artifacts[path.name] = {"bytes": len(data),
                                    "sha256": hashlib.sha256(data).hexdigest()}
    receipt = build_receipt(
        scope={
            "arm": context.arm_name,
            "condition": context.condition,
            "workload_kind": getattr(context, "workload", "fixture"),
            "simulator": context.simulator_scope,
            "app": {"path": str(context.app_path),
                    "bundle_id": context.bundle_id,
                    "sha256": context.app_sha256,
                    "sha256_basis": context.app_sha256_basis},
            "arm_command": context.arm_command_scope,
            "source_revisions": context.source_revisions,
            "workload": {"tap_identifier": context.tap_identifier,
                         "tap_label": context.tap_label,
                         "expected_before": context.expected_before,
                         "expected_after": context.expected_after},
            **context.receipt_extras,
        },
        timings=context.timings,
        memory={"stages": context.memory.stages if context.memory else {},
                "shared_services": {
                    "scope": "shared-coresimulator-host-services",
                    "stages": context.memory.shared_stages if context.memory else {},
                }},
        shutdown=shutdown,
        artifacts=artifacts,
        status=status,
        error=error,
        elapsed_seconds=round(elapsed, 3),
    )
    return scrub_secrets(receipt, getattr(context, "secrets", ()))


# ---------------------------------------------------------------------------
# Cold/warm lifecycle driver.
# ---------------------------------------------------------------------------


def restore_installed_exec_modes(udid, bundle_id, app_dir):
    """Restore executable bits stripped by `simctl install` to match `hashApp`."""
    if not app_dir.is_dir():
        return
    resolved = simctl("get_app_container", udid, bundle_id, "app", timeout=120).strip()
    if not resolved:
        raise BenchmarkError("failed to resolve the installed app container")
    installed = Path(resolved)
    for source in sorted(p for p in app_dir.rglob("*")
                         if p.is_file() and not p.is_symlink()):
        mode = source.stat().st_mode
        if not mode & 0o111:
            continue
        target = installed / source.relative_to(app_dir)
        if target.is_file():
            os.chmod(target, mode & 0o7777)


def prepare_cold_simulator(args, udid_slot):
    name = f"{args.simulator_name_prefix}-{uuid.uuid4().hex[:12]}"
    udid = create_simulator(name, args.device_type, args.runtime)
    udid_slot.update(udid=udid, name=name, created_by_run=True)
    simctl("boot", udid)
    wait_for_boot(udid, args.boot_timeout)
    simctl("install", udid, str(Path(args.app).resolve()), timeout=600)
    restore_installed_exec_modes(udid, args.bundle_id, Path(args.app).resolve())
    if getattr(args, "workload", "fixture") == "meer":
        simctl("privacy", udid, "grant", "location", args.bundle_id)
    return udid


def prepare_warm_simulator(args, udid_slot):
    udid = udid_slot.get("udid")
    if not udid or udid_slot.get("created_by_run") is not True:
        raise BenchmarkError("a warm observation requires the simulator created by this cold run")
    state = device_state(udid)
    if state is None:
        raise BenchmarkError(f"the owned simulator {udid} does not exist")
    # Warm restart: shut down the same simulator, then boot it again with its
    # installed app and retained data.
    if state in ("Booted", "Booting"):
        simctl("shutdown", udid)
    state = device_state(udid)
    if state != "Shutdown":
        raise BenchmarkError(f"the owned simulator {udid} was not shut down before the warm restart")
    simctl("boot", udid)
    wait_for_boot(udid, args.boot_timeout)
    return udid


def run_condition(args, arm_name, condition, udid_slot, output):
    output.mkdir(parents=True, exist_ok=True)
    if condition == "cold":
        udid = prepare_cold_simulator(args, udid_slot)
    else:
        udid = prepare_warm_simulator(args, udid_slot)
    context = ObservationContext(args, udid, condition, output, arm_name)
    context.simulator_scope = {
        "udid": udid_slot.get("udid", udid), "name": udid_slot.get("name"),
        "device_type": args.device_type, "runtime": args.runtime,
        "created_by_run": udid_slot.get("created_by_run", False),
    }
    identity = getattr(args, "app_identity", None) or preflight_app(args.app)
    context.app_sha256 = identity["sha256"]
    context.app_sha256_basis = identity["sha256_basis"]
    context.source_revisions = dict(args.revisions)
    receipt = run_observation(arm_name, context)
    problems = validate_receipt(receipt)
    if problems:
        receipt["schema_problems"] = problems
        if receipt["status"] == "passed":
            receipt["status"] = "failed"
            receipt["error"] = "benchmark receipt failed validation"
    secrets = getattr(context, "secrets", ())
    if not secrets and getattr(args, "credentials", None):
        secrets = load_mock_credentials(args.credentials)
    clean_receipt = scrub_secrets(receipt, secrets)
    assert_no_secrets(clean_receipt, secrets, label="receipt.json")
    (output / "receipt.json").write_text(
        json.dumps(clean_receipt, indent=2, sort_keys=True) + "\n")
    return clean_receipt


def cleanup_owned_simulator(udid_slot, simctl_runner=None):
    """Delete the simulator this run created; never act on unowned or
    implicitly inferred devices. Failures are surfaced in the record and
    absence is verified against the device list, never assumed."""
    if simctl_runner is None:
        simctl_runner = simctl
    udid = udid_slot.get("udid")
    record = {"requested": False}
    if not udid:
        record["reason"] = "no simulator was selected by this run"
        return record
    if not udid_slot.get("created_by_run"):
        record["reason"] = ("the simulator was not created by this run; "
                            "unowned devices are never shut down or deleted")
        return record
    record.update(requested=True, udid=udid)
    try:
        simctl_runner("shutdown", udid)
        record["shutdown"] = "passed"
    except (OSError, subprocess.SubprocessError) as error:
        record["shutdown"] = f"failed: {sanitize(str(error))[:200]}"
    try:
        simctl_runner("delete", udid)
        record["delete"] = "passed"
    except (OSError, subprocess.SubprocessError) as error:
        record["delete"] = f"failed: {sanitize(str(error))[:200]}"
    try:
        states = parse_device_states(simctl_runner("list", "-j", "devices"))
        record["verified_absent"] = udid not in states
        if not record["verified_absent"]:
            record["state_after"] = states.get(udid)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        record["verified_absent"] = False
        record["absence_check_error"] = sanitize(str(error))[:200]
    return record


def drive_conditions(args, conditions, udid_slot,
                    run_condition_fn=None, simctl_runner=None):
    """Run every requested condition with the owned cold simulator cleaned
    up in finally, whatever preflight, build or trial exception happens.

    Failures are surfaced on stderr and in the returned record instead of
    being swallowed, and a failed cleanup keeps the exit status failing.
    """
    if run_condition_fn is None:
        run_condition_fn = run_condition
    if simctl_runner is None:
        simctl_runner = simctl
    succeeded = True
    try:
        for condition in conditions:
            receipt = run_condition_fn(args, args.arm, condition, udid_slot,
                                        args.output / condition)
            print(f"{args.arm} {condition}: {receipt['status']}", flush=True)
            succeeded = succeeded and receipt["status"] == "passed"
    except (BenchmarkError, InventoryError, AcceptanceError, OSError,
            subprocess.SubprocessError) as caught:
        succeeded = False
        print(f"{args.arm}: aborted: {caught}", file=sys.stderr, flush=True)
    finally:
        cleanup = cleanup_owned_simulator(udid_slot, simctl_runner)
    return cleanup, succeeded


def aggregate_numeric_stats(values):
    """Compute exact summary statistics for positive numeric trial observations."""
    nums = sorted(float(v) for v in values)
    if not nums:
        return None
    count = len(nums)
    mid = count // 2
    median = nums[mid] if count % 2 == 1 else (nums[mid - 1] + nums[mid]) / 2.0
    pos = 0.95 * (count - 1)
    lo = int(pos)
    hi = min(lo + 1, count - 1)
    p95 = nums[lo] + (pos - lo) * (nums[hi] - nums[lo])
    return {
        "count": count,
        "min": round(nums[0], 3),
        "median": round(median, 3),
        "mean": round(sum(nums) / count, 3),
        "max": round(nums[-1], 3),
        "p95": round(p95, 3),
    }


def _peak_memory_by_role(receipt):
    peaks = {}
    stages = (receipt.get("memory") or {}).get("stages") or {}
    for samples in stages.values():
        if not isinstance(samples, list):
            continue
        for sample in samples:
            if not isinstance(sample, dict) or sample.get("status") != "sampled":
                continue
            role = sample.get("role")
            footprint = sample.get("ri_phys_footprint_bytes")
            if isinstance(role, str) and isinstance(footprint, int):
                peaks[role] = max(peaks.get(role, 0), footprint)
    return peaks


def summarize_trials(manifest, trial_records):
    """Aggregate latency, milestone, and peak-footprint stats across multi-trial runs."""
    by_condition = {}
    for condition in manifest.get("conditions") or ():
        passed_receipts = []
        failed = 0
        cleanups_ok = 0
        for rec in trial_records:
            if rec.get("cleanup_verified_absent"):
                cleanups_ok += 1
            receipt = (rec.get("receipts") or {}).get(condition)
            if isinstance(receipt, dict) and receipt.get("status") == "passed":
                passed_receipts.append(receipt)
            else:
                failed += 1
        elapsed_vals = []
        arm_start_vals = []
        first_snap_vals = []
        tap_vals = []
        shutdown_vals = []
        milestone_vals = {}
        peak_mem_vals = {}
        for receipt in passed_receipts:
            if isinstance(receipt.get("elapsed_seconds"), (int, float)):
                elapsed_vals.append(float(receipt["elapsed_seconds"]))
            timings = receipt.get("timings") or {}
            arm_ns = (timings.get("arm_start") or {}).get("elapsed_ns")
            if isinstance(arm_ns, int):
                arm_start_vals.append(arm_ns / 1_000_000)
            fs_ns = (timings.get("first_usable_snapshot") or {}).get("success_timing_ns")
            if isinstance(fs_ns, int):
                first_snap_vals.append(fs_ns / 1_000_000)
            tap_ns = (timings.get("tap_to_fresh_frame") or {}).get("success_timing_ns")
            if isinstance(tap_ns, int):
                tap_vals.append(tap_ns / 1_000_000)
            shut_ns = (receipt.get("shutdown") or {}).get("elapsed_ns")
            if isinstance(shut_ns, int):
                shutdown_vals.append(shut_ns / 1_000_000)
            for name, m in (timings.get("meer_milestones") or {}).items():
                if isinstance(m, dict) and isinstance(m.get("ms"), (int, float)):
                    milestone_vals.setdefault(name, []).append(float(m["ms"]))
            for role, bytes_val in _peak_memory_by_role(receipt).items():
                peak_mem_vals.setdefault(role, []).append(bytes_val)
        cond_summary = {
            "trials_requested": len(trial_records),
            "trials_passed": len(passed_receipts),
            "trials_failed": failed,
            "cleanups_verified_absent": cleanups_ok,
        }
        for key, vals in (
            ("elapsed_seconds", elapsed_vals),
            ("arm_start_ms", arm_start_vals),
            ("first_usable_snapshot_ms", first_snap_vals),
            ("tap_to_fresh_frame_ms", tap_vals),
            ("shutdown_ms", shutdown_vals),
        ):
            stats = aggregate_numeric_stats(vals)
            if stats is not None:
                cond_summary[key] = stats
        if milestone_vals:
            cond_summary["meer_milestones_ms"] = {
                name: aggregate_numeric_stats(vals)
                for name, vals in sorted(milestone_vals.items())
            }
        if peak_mem_vals:
            cond_summary["peak_footprint_bytes"] = {
                role: aggregate_numeric_stats(vals)
                for role, vals in sorted(peak_mem_vals.items())
            }
        by_condition[condition] = cond_summary
    return {
        "schema_version": 1,
        "arm": manifest.get("arm"),
        "workload": manifest.get("workload"),
        "trials_requested": len(trial_records),
        "conditions": by_condition,
    }


def drive_trials(args, manifest, drive_conditions_fn=None, secrets=()):
    """Execute one or multiple independent cold/warm cycles and aggregate stats."""
    if drive_conditions_fn is None:
        drive_conditions_fn = drive_conditions
    conditions = manifest["conditions"]
    trials = getattr(args, "trials", 1)
    if trials == 1:
        (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        cleanup, succeeded = drive_conditions_fn(args, conditions, {})
        (args.output / "simulator-cleanup.json").write_text(
            json.dumps(sanitize(cleanup), indent=2, sort_keys=True) + "\n")
        cleanup_ok = cleanup.get("requested") is not True or cleanup.get("verified_absent") is True
        return cleanup, succeeded, cleanup_ok, None

    top_manifest = {**manifest, "trials": trials}
    (args.output / "manifest.json").write_text(json.dumps(top_manifest, indent=2, sort_keys=True) + "\n")
    all_succeeded = True
    all_cleanups_ok = True
    trial_records = []
    for index in range(1, trials + 1):
        trial_dir = args.output / f"trial-{index}"
        trial_dir.mkdir(parents=True, exist_ok=True)
        trial_manifest = {**manifest, "trial": index, "trials": trials}
        (trial_dir / "manifest.json").write_text(json.dumps(trial_manifest, indent=2, sort_keys=True) + "\n")
        trial_args = argparse.Namespace(**{**vars(args), "output": trial_dir})
        cleanup, succeeded = drive_conditions_fn(trial_args, conditions, {})
        (trial_dir / "simulator-cleanup.json").write_text(
            json.dumps(sanitize(cleanup), indent=2, sort_keys=True) + "\n")
        cleanup_ok = cleanup.get("requested") is not True or cleanup.get("verified_absent") is True
        all_succeeded = all_succeeded and succeeded
        all_cleanups_ok = all_cleanups_ok and cleanup_ok
        receipts = {}
        for condition in conditions:
            receipt_path = trial_dir / condition / "receipt.json"
            if receipt_path.is_file():
                try:
                    receipts[condition] = json.loads(receipt_path.read_text())
                except ValueError:
                    pass
        trial_records.append({
            "trial": index,
            "succeeded": succeeded,
            "cleanup_verified_absent": cleanup_ok,
            "receipts": receipts,
        })
    summary = scrub_secrets(summarize_trials(manifest, trial_records), secrets)
    assert_no_secrets(summary, secrets, label="summary.json")
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    last_cleanup = {"requested": True, "trials": trials, "verified_absent": all_cleanups_ok}
    return last_cleanup, all_succeeded, all_cleanups_ok, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=sorted(ARM_CLASSES), required=True)
    parser.add_argument("--condition", choices=("cold", "both"), default="both")
    parser.add_argument("--trials", type=int, default=1,
                        help="number of independent condition cycles to run (>1 writes summary.json)")
    parser.add_argument("--workload", choices=("fixture", "meer"), default="fixture")
    parser.add_argument("--simulator-name-prefix", default="AshnazgIosBenchmark")
    parser.add_argument("--app", required=True,
                        help="prebuilt .app bundle; installed before timing")
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--device-type", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--tap-identifier", default=None)
    parser.add_argument("--tap-label", default=None)
    parser.add_argument("--expected-before", default=None)
    parser.add_argument("--expected-after", default=None)
    parser.add_argument("--credentials", type=Path, default=None,
                        help="mode-600 JSON file with synthetic Meer email and password")
    parser.add_argument("--initial-url", default=None,
                        help="loopback http(s) URL for Meer --initialUrl")
    parser.add_argument("--arm-command", default=None,
                        help="argv template for the ashnazg-mcp or direct-xctest arm; "
                             "{udid} is substituted with the owned simulator, and "
                             "direct XCTest can pass -resultBundlePath {xcresult}")
    parser.add_argument("--ashnazg-manifest-template", type=Path,
                        help="app manifest template used by --ios-app {manifest}; app and "
                             "simulator pins are set by the harness at each start")
    parser.add_argument("--xctest-artifacts", default=None,
                        help="optional directory of declared direct-xctest captures to hash")
    parser.add_argument("--revision", action="append", default=[], metavar="NAME=VALUE",
                        help="explicit source revision identity, repeatable")
    parser.add_argument("--first-snapshot-timeout", type=float,
                        default=DEFAULT_FIRST_SNAPSHOT_TIMEOUT)
    parser.add_argument("--boot-timeout", type=float, default=240.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.trials < 1:
        parser.error("--trials must be at least 1")
    try:
        require_macos_host()
    except BenchmarkError as error:
        parser.error(str(error))
    secrets = ()
    if args.workload == "meer":
        if args.tap_identifier is None:
            args.tap_identifier = MEER_HOME_SEARCH
        if args.expected_before is None:
            args.expected_before = MEER_HOME_SEARCH
        if args.expected_after is None:
            args.expected_after = MEER_HOME_SEARCH
        if args.credentials is None:
            parser.error("--credentials is required for --workload meer")
        try:
            secrets = load_mock_credentials(args.credentials)
            validate_loopback_http_url(args.initial_url)
        except BenchmarkError as error:
            parser.error(str(error))
    else:
        missing = [
            flag for flag, val in (
                ("--tap-identifier", args.tap_identifier),
                ("--expected-before", args.expected_before),
                ("--expected-after", args.expected_after),
            )
            if val is None
        ]
        if missing:
            parser.error(f"{', '.join(missing)} required for --workload fixture")
    if args.arm in ("ashnazg-mcp", "direct-xctest") and not args.arm_command:
        parser.error(f"--arm-command is required for {args.arm}")
    if args.ashnazg_manifest_template is not None:
        command = shlex.split(args.arm_command or "")
        if (args.arm != "ashnazg-mcp"
                or not any(command[i:i + 2] == ["--ios-app", "{manifest}"]
                           for i in range(len(command)))
                or args.simulator_name_prefix != "AshnazgIosBenchmark"):
            parser.error("the manifest template requires the ashnazg-mcp arm, "
                         "--ios-app {manifest}, and the default simulator name prefix")
        try:
            template = json.loads(read_text_bounded(args.ashnazg_manifest_template))
        except (OSError, ValueError, BenchmarkError) as error:
            parser.error(f"failed to read the Ashnazg manifest template: {error}")
        if not isinstance(template, dict):
            parser.error("the Ashnazg manifest template must be a JSON object")
        args.ashnazg_manifest_template = template
    elif args.arm in ("ashnazg-mcp", "direct-xctest") and "{udid}" not in args.arm_command:
        parser.error(f"--arm-command for {args.arm} must select the owned simulator "
                     "with an explicit {udid} placeholder")
    revisions = {}
    for item in args.revision:
        name, separator, value = item.partition("=")
        if not separator or not name or not value:
            parser.error(f"--revision must be NAME=VALUE, got {item!r}")
        revisions[name] = value
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = "unknown"
    revisions.setdefault("repository", head)
    try:
        app_identity = preflight_app(args.app)
    except BenchmarkError as error:
        parser.error(str(error))
    args.app_identity = app_identity
    args.revisions = revisions
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "arm": args.arm,
        "workload": args.workload,
        "host": {
            "macos": subprocess.run(["sw_vers", "-productVersion"], capture_output=True,
                                    text=True, check=True).stdout.strip(),
            "xcode": subprocess.run(["xcodebuild", "-version"], capture_output=True,
                                    text=True, check=True).stdout.strip().splitlines()[0],
        },
        "source_revisions": revisions,
        "app": {"path": app_identity["path"], "bundle_id": args.bundle_id,
                "sha256": app_identity["sha256"],
                "sha256_basis": app_identity["sha256_basis"]},
        "device_type": args.device_type,
        "runtime": args.runtime,
        "conditions": list(("cold", "warm") if args.condition == "both"
                           else (args.condition,)),
    }
    cleanup, succeeded, cleanup_ok, _ = drive_trials(args, manifest, secrets=secrets)
    if not cleanup_ok:
        print(f"owned simulator cleanup did not verify absence: "
              f"{json.dumps(sanitize(cleanup), sort_keys=True)}", file=sys.stderr)
    print(f"owned simulator cleanup: "
          f"{'verified' if cleanup_ok else 'INCOMPLETE'}")
    return int(not (succeeded and cleanup_ok))


if __name__ == "__main__":
    sys.exit(main())
