#!/usr/bin/env python3
"""Drive the production `ashnazg mcp --ios-fixture` server over stdio MCP.

macOS only. Exercises the actual public protocol (initialize, tools/list,
tools/call) against one live XCTest fixture session: an accessibility
snapshot, a tap whose selector comes from that snapshot, fresh screenshots,
and keyboard text entry that appends like the Android `type` action, with
the resulting distinct accessibility state and PNG evidence correlated
through session sequence ids and live counter observations. Verifies
owned-resource cleanup on EOF and writes a sanitized machine-readable
receipt plus raw artifacts.

Usage:
    python3 platform/ios/verify_mcp_fixture.py \
        --binary target/debug/ashnazg --output-dir <empty owned dir>
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
FROZEN_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "runtime/mcp-tools.schema.json"
UI_TOOLS = ("ui_act", "ui_screenshot", "ui_snapshot")
# The server opens the fixture session (simulator, build, install) before it
# answers initialize, so early requests carry the cold-start deadline.
COLD_TIMEOUT = 900.0
REQUEST_TIMEOUT = 120.0
SHUTDOWN_TIMEOUT = 300.0
MAX_STDERR_BYTES = 1 << 20


class AcceptanceError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def png_identity(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def device_udids() -> set[str]:
    listing = subprocess.run(
        ["xcrun", "simctl", "list", "-j", "devices"], capture_output=True, check=True
    )
    return {
        device["udid"] for group in json.loads(listing.stdout)["devices"].values()
        for device in group
    }


class StdioClient:
    """Line-delimited JSON-RPC reader with a bounded background drain."""

    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.stderr_tail: list[bytes] = []
        self.lines: queue.Queue[dict | None] = queue.Queue()

    def start(self) -> None:
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stdout(self) -> None:
        stream = self.process.stdout
        assert stream is not None
        try:
            for line in iter(stream.readline, b""):
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self.lines.put(json.loads(text))
        except Exception as error:  # noqa: BLE001 - drain thread must never raise
            self.lines.put({"drain_error": str(error)})
        finally:
            self.lines.put(None)

    def _drain_stderr(self) -> None:
        stream = self.process.stderr
        assert stream is not None
        total = 0
        try:
            for line in iter(stream.readline, b""):
                total += len(line)
                if total > MAX_STDERR_BYTES:
                    line = line[: MAX_STDERR_BYTES - total]
                self.stderr_tail.append(line)
        except Exception:  # noqa: BLE001
            pass

    def stderr_text(self) -> str:
        return b"".join(self.stderr_tail).decode("utf-8", "replace")

    def request(self, payload: dict, timeout: float, allow_error: bool = False) -> dict:
        request_id = payload["id"]
        wire = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        self.process.stdin.write(wire)
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcceptanceError(f"request {payload['method']} timed out")
            try:
                message = self.lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise AcceptanceError(f"request {payload['method']} timed out") from exc
            if message is None:
                raise AcceptanceError(
                    f"server closed stdout while answering {payload['method']}"
                )
            if "drain_error" in message:
                raise AcceptanceError("stdout drain failed: " + message["drain_error"])
            if message.get("id") != request_id:
                continue
            if "error" in message:
                if allow_error:
                    return message
                raise AcceptanceError(
                    f"{payload['method']} failed: {message['error'].get('message')}"
                )
            return message["result"]

    def notify(self, method: str) -> None:
        wire = json.dumps({"jsonrpc": "2.0", "method": method}, separators=(",", ":")).encode() + b"\n"
        self.process.stdin.write(wire)
        self.process.stdin.flush()

    def session_descriptor(self) -> dict:
        for raw in b"".join(self.stderr_tail).splitlines():
            text = raw.decode("utf-8", "replace")
            marker = "ios-fixture-session "
            if marker in text:
                return json.loads(text.split(marker, 1)[1].strip())
        raise AcceptanceError(
            "server never reported its ios-fixture-session descriptor; stderr:\n"
            + self.stderr_text()[-4096:]
        )


def structured(result: dict) -> dict:
    if not result.get("structuredContent"):
        raise AcceptanceError(f"tool result has no structured content: {result.get('content')}")
    return result["structuredContent"]


def image_and_metadata(result: dict) -> tuple[bytes, dict]:
    blocks = result.get("content") or []
    images = [block for block in blocks if block.get("type") == "image"]
    texts = [block for block in blocks if block.get("type") == "text"]
    if len(images) != 1 or len(texts) != 1:
        raise AcceptanceError(f"screenshot result blocks were {blocks}")
    png = base64.b64decode(images[0]["data"], validate=True)
    metadata = json.loads(texts[0]["text"])
    return png, metadata


def expect_tool_rejected(result: dict, what: str) -> None:
    """A rejected tool call surfaces as either an MCP isError result or a
    JSON-RPC error response."""
    if "error" in result:
        return
    if not result.get("result", {}).get("isError"):
        raise AcceptanceError(f"{what} must be rejected in iOS mode")


def run_acceptance(binary: Path, output: Path, source_sha: str) -> dict:
    if sys.platform != "darwin":
        raise AcceptanceError("the iOS fixture bridge requires macOS")
    binary = binary.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise AcceptanceError(f"--binary must be an executable file: {binary}")
    if not output.is_dir():
        output.mkdir(parents=True, mode=0o700)
    if any(output.iterdir()):
        raise AcceptanceError("--output-dir must be empty to preserve evidence")

    environment = dict(os.environ)
    environment.pop("ASHNAZG_TOKEN", None)
    environment["RUST_LOG"] = "info"
    for tool in ("xcodebuild", "xcrun", "xcodegen"):
        if subprocess.run(["sh", "-c", f"command -v {tool}"], capture_output=True).returncode:
            raise AcceptanceError(f"{tool} must be on PATH for the iOS fixture bridge")

    receipt: dict = {
        "schema_version": 1,
        "status": "running",
        "source_sha": source_sha,
        "binary": {"path": str(binary), "sha256": sha256_bytes(binary.read_bytes())},
        "tool_sequence": [],
        "environment": {},
        "cleanup": {},
        "artifacts": {"raw_dir": str(output)},
    }
    started = time.monotonic()
    descriptor: dict | None = None
    next_id = [1]

    launch_started = time.monotonic()
    process = subprocess.Popen(
        [str(binary), "mcp", "--ios-fixture"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        bufsize=0,
    )
    client = StdioClient(process)
    client.start()

    def record(tool: str, arguments: dict, result: dict, elapsed: float) -> None:
        sanitized = dict(arguments)
        if sanitized.get("target", {}).get("testId") == "fixture-secret":
            sanitized["text"] = "<redacted synthetic secret>"
        entry = {"tool": tool, "arguments": sanitized, "elapsed_seconds": round(elapsed, 3)}
        if "content" in result:
            entry["content_kinds"] = [block.get("type") for block in result["content"]]
        if result.get("isError"):
            entry["isError"] = True
        receipt["tool_sequence"].append(entry)

    def call(name: str, arguments: dict, timeout: float, allow_error: bool = False) -> dict:
        began = time.monotonic()
        payload = {
            "jsonrpc": "2.0",
            "id": next_id[0],
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        result = client.request(payload, timeout, allow_error=allow_error)
        next_id[0] += 1
        record(name, arguments, result, time.monotonic() - began)
        return result

    failure: str | None = None
    # Fixture-only synthetic sentinel for the secure redaction checks; bound
    # before the session so failure paths can always scrub it from retained
    # text. It is never a real credential.
    secret_value = "synthetic-fixture-secret-3fa1"
    try:
        init = client.request(
            {
                "jsonrpc": "2.0",
                "id": next_id[0],
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "ios-mcp-fixture-acceptance", "version": "1"},
                },
            },
            COLD_TIMEOUT,
        )
        next_id[0] += 1
        if init.get("protocolVersion") != PROTOCOL_VERSION:
            raise AcceptanceError("initialize did not negotiate the expected protocol version")
        client.notify("notifications/initialized")

        listed = client.request(
            {"jsonrpc": "2.0", "id": next_id[0], "method": "tools/list", "params": {}}, REQUEST_TIMEOUT
        )
        next_id[0] += 1
        tools = {tool["name"]: tool for tool in listed.get("tools", [])}
        if set(tools) != set(UI_TOOLS):
            raise AcceptanceError(f"iOS mode must list exactly {UI_TOOLS}, got {sorted(tools)}")
        frozen = json.loads(FROZEN_SCHEMA_PATH.read_text())
        for name in UI_TOOLS:
            if tools[name].get("inputSchema") != frozen[name]:
                raise AcceptanceError(f"{name} inputSchema differs from the frozen production contract")

        # The session opens before initialize is answered, so the descriptor
        # is already on stderr here.
        descriptor = client.session_descriptor()
        receipt["environment"] = {
            "macos": subprocess.run(["sw_vers", "-productVersion"], capture_output=True, check=True)
            .stdout.decode()
            .strip(),
            "xcode": subprocess.run(["xcodebuild", "-version"], capture_output=True, check=True)
            .stdout.decode()
            .strip()
            .splitlines()[0],
            "ios_runtime": descriptor["simulator"]["runtime"],
            "simulator": descriptor["simulator"],
            "fixture_app": descriptor["fixtureApp"],
        }
        receipt["native_command"] = descriptor["command"]
        receipt["session"] = {
            "bridge_port": descriptor["bridgePort"],
            "work_dir": descriptor["workDir"],
            "pid": descriptor["pid"],
        }

        # Live snapshot: the tap selector must come from this response.
        snapshot = structured(call("ui_snapshot", {"includeInvisible": False}, COLD_TIMEOUT))
        nodes = snapshot.get("nodes") or []
        (output / "snapshot-before.json").write_text(json.dumps(snapshot, indent=2, sort_keys=True))
        buttons = [
            node
            for node in nodes
            if node.get("role") == "button"
            and node.get("testId") == "fixture-increment"
            and node.get("enabled") is True
        ]
        if len(buttons) != 1:
            raise AcceptanceError(f"expected one enabled fixture-increment button, got {buttons}")
        selector = {"testId": buttons[0]["testId"]}
        if not any(node.get("text") == "Count: 0" for node in nodes):
            raise AcceptanceError(f"initial snapshot did not show Count: 0: {nodes}")
        first_snapshot_ready = time.monotonic()

        png_before, meta_before = image_and_metadata(
            call("ui_screenshot", {"format": "png"}, REQUEST_TIMEOUT)
        )
        (output / "screenshot-before.png").write_bytes(png_before)
        if png_identity(png_before) is None:
            raise AcceptanceError("the pre-tap screenshot is not a valid PNG")
        if meta_before.get("counterLabel") != "Count: 0":
            raise AcceptanceError(f"pre-tap counter observation was {meta_before.get('counterLabel')}")

        tap_started = time.monotonic()
        acted = structured(call("ui_act", {"action": "tap", "target": selector}, REQUEST_TIMEOUT))
        (output / "snapshot-after.json").write_text(json.dumps(acted, indent=2, sort_keys=True))
        if acted.get("settled") is not True:
            raise AcceptanceError("the live tap did not settle")
        state_after = acted.get("snapshot") or {}
        nodes_after = state_after.get("nodes") or []
        if not any(node.get("text") == "Count: 1" for node in nodes_after):
            raise AcceptanceError(f"post-tap snapshot did not show Count: 1: {nodes_after}")
        if any(node.get("text") == "Count: 0" for node in nodes_after):
            raise AcceptanceError("post-tap snapshot still shows Count: 0")
        if state_after.get("snapshotId", 0) <= snapshot.get("snapshotId", 0):
            raise AcceptanceError("post-tap snapshot did not advance the session sequence")

        png_after, meta_after = image_and_metadata(
            call("ui_screenshot", {"format": "png"}, REQUEST_TIMEOUT)
        )
        (output / "screenshot-after.png").write_bytes(png_after)
        if png_after == png_before:
            raise AcceptanceError("post-tap screenshot is byte-identical to the pre-tap screenshot")
        if png_identity(png_after) is None:
            raise AcceptanceError("the post-tap screenshot is not a valid PNG")
        # The screenshot op itself advances the sequence, so require it to be
        # beyond the tap and to observe the post-tap counter state.
        if meta_after.get("snapshotId", 0) <= state_after.get("snapshotId", 0):
            raise AcceptanceError("post-tap screenshot did not follow the settled tap state")
        if meta_after.get("counterLabel") != "Count: 1":
            raise AcceptanceError(f"post-tap counter observation was {meta_after.get('counterLabel')}")
        post_tap_png_verified = time.monotonic()
        receipt["state_evidence"] = {
            "selector_from_snapshot": selector,
            "button_role": buttons[0]["role"],
            "count_before": "Count: 0",
            "count_after": "Count: 1",
            "snapshot_ids": {
                "before": snapshot.get("snapshotId"),
                "after_tap": state_after.get("snapshotId"),
                "after_screenshot": meta_after.get("snapshotId"),
            },
            "counter_observations": {
                "before_screenshot": meta_before.get("counterLabel"),
                "after_screenshot": meta_after.get("counterLabel"),
            },
        }
        receipt["png_evidence"] = {
            "before": {
                "bytes": len(png_before),
                "sha256": sha256_bytes(png_before),
                "dimensions": png_identity(png_before),
            },
            "after": {
                "bytes": len(png_after),
                "sha256": sha256_bytes(png_after),
                "dimensions": png_identity(png_after),
            },
            "distinct": sha256_bytes(png_before) != sha256_bytes(png_after),
            "correlated_state": {
                "snapshot_id": meta_after.get("snapshotId"),
                "counter_label": meta_after.get("counterLabel"),
            },
            "mime": "image/png",
        }
        # Single-run monotonic observations with their exact boundaries; both
        # intervals end before any text-entry request runs. Neither is a
        # benchmark or a latency claim.
        receipt["fixture_timings"] = {
            "child_launch_to_first_validated_snapshot": {
                "seconds": round(first_snapshot_ready - launch_started, 3),
                "start": "immediately before spawning the mcp --ios-fixture child",
                "end": "first ui_snapshot validated (Count: 0, one enabled fixture-increment)",
            },
            "tap_to_verified_post_tap_png": {
                "seconds": round(post_tap_png_verified - tap_started, 3),
                "start": "immediately before the ui_act tap request",
                "end": "post-tap PNG verified (valid, distinct, sequence-correlated, Count: 1)",
            },
            "note": "single-run monotonic observations on this host only",
        }

        # Text entry (fixture-scoped): type appends onto existing text like
        # the Android type action, proven by typing twice into the same
        # nonempty field. The selector comes from the live post-tap snapshot
        # and the payload rides the wire separately from the selector text.
        input_nodes = [node for node in nodes_after if node.get("testId") == "fixture-input"]
        if (
            len(input_nodes) != 1
            or input_nodes[0].get("role") != "textfield"
            or input_nodes[0].get("enabled") is not True
        ):
            raise AcceptanceError(
                f"expected one enabled fixture-input textfield in the post-tap snapshot, got {input_nodes}"
            )
        first_typed = structured(
            call(
                "ui_act",
                {"action": "type", "target": {"testId": "fixture-input"}, "text": "Ashnazg"},
                REQUEST_TIMEOUT,
            )
        )
        (output / "snapshot-after-first-type.json").write_text(
            json.dumps(first_typed, indent=2, sort_keys=True)
        )
        typed_state = first_typed.get("snapshot") or {}
        if first_typed.get("settled") is not True:
            raise AcceptanceError("the first type action did not settle")
        if typed_state.get("snapshotId", 0) <= meta_after.get("snapshotId", 0):
            raise AcceptanceError("the first type action did not advance the session sequence")
        if not any(
            node.get("testId") == "fixture-input" and node.get("value") == "Ashnazg"
            for node in typed_state.get("nodes") or []
        ):
            raise AcceptanceError(f"the first type did not show value Ashnazg: {typed_state.get('nodes')}")
        second_result = call(
            "ui_act",
            {"action": "type", "target": {"testId": "fixture-input"}, "text": "-fixture"},
            REQUEST_TIMEOUT,
            allow_error=True,
        )
        if "error" in second_result or second_result.get("isError"):
            # Inspect the result once without replaying the rejected mutation.
            latest = structured(call("ui_snapshot", {"includeInvisible": False}, REQUEST_TIMEOUT))
            receipt["append_failure"] = {
                "before_snapshot_id": typed_state.get("snapshotId"),
                "after_snapshot_id": latest.get("snapshotId"),
                "observed_fixture_values": [
                    node.get("value")
                    for node in latest.get("nodes", [])
                    if node.get("testId") == "fixture-input"
                ],
            }
            raise AcceptanceError("the second type action failed; see the fresh append_failure readback")
        second_typed = structured(second_result)
        (output / "snapshot-after-second-type.json").write_text(
            json.dumps(second_typed, indent=2, sort_keys=True)
        )
        appended_state = second_typed.get("snapshot") or {}
        if second_typed.get("settled") is not True:
            raise AcceptanceError("the second type action did not settle")
        if appended_state.get("snapshotId", 0) <= typed_state.get("snapshotId", 0):
            raise AcceptanceError("the second type action did not advance the session sequence")
        if not any(
            node.get("testId") == "fixture-input" and node.get("value") == "Ashnazg-fixture"
            for node in appended_state.get("nodes") or []
        ):
            raise AcceptanceError("the second type did not append onto the existing nonempty text")
        noop_typed = structured(
            call(
                "ui_act",
                {"action": "type", "target": {"testId": "fixture-input"}, "text": ""},
                REQUEST_TIMEOUT,
            )
        )
        if noop_typed.get("settled") is not True:
            raise AcceptanceError("the empty type payload did not settle as a no-op")
        if not any(
            node.get("testId") == "fixture-input" and node.get("value") == "Ashnazg-fixture"
            for node in (noop_typed.get("snapshot") or {}).get("nodes") or []
        ):
            raise AcceptanceError("the empty type payload changed the observed field value")
        # A direct fresh snapshot confirms the persisted typed state.
        confirmed = structured(call("ui_snapshot", {"includeInvisible": False}, REQUEST_TIMEOUT))
        (output / "snapshot-after-types.json").write_text(
            json.dumps(confirmed, indent=2, sort_keys=True)
        )
        confirmed_nodes = confirmed.get("nodes") or []
        if not any(
            node.get("testId") == "fixture-input" and node.get("value") == "Ashnazg-fixture"
            for node in confirmed_nodes
        ):
            raise AcceptanceError("the direct fresh snapshot lost the typed value")

        # Secure field: a fixture-only synthetic sentinel is typed through
        # the real bridge and must never surface in any snapshot, artifact,
        # or receipt this verifier retains. Both post-type states are
        # validated entirely in memory before any secure artifact is written;
        # every failure message reports counts and labels, never node text.
        secret_nodes = [node for node in confirmed_nodes if node.get("testId") == "fixture-secret"]
        if len(secret_nodes) != 1:
            raise AcceptanceError(
                f"expected one fixture-secret node in the fresh snapshot, found {len(secret_nodes)}"
            )
        if secret_nodes[0].get("role") != "securetextfield":
            raise AcceptanceError("the fixture-secret node did not report the securetextfield role")
        if "value" in secret_nodes[0]:
            raise AcceptanceError("the snapshot serialized a secure field value before typing")
        secure_typed = structured(
            call(
                "ui_act",
                {"action": "type", "target": {"testId": "fixture-secret"}, "text": secret_value},
                REQUEST_TIMEOUT,
            )
        )
        secure_fresh = structured(call("ui_snapshot", {"includeInvisible": False}, REQUEST_TIMEOUT))
        if secure_typed.get("settled") is not True:
            raise AcceptanceError("the secure type action did not settle")
        for label, document, state in [
            ("act response", secure_typed, secure_typed.get("snapshot") or {}),
            ("fresh snapshot", secure_fresh, secure_fresh),
        ]:
            typed_secret = [
                node for node in state.get("nodes") or []
                if node.get("testId") == "fixture-secret"
            ]
            if len(typed_secret) != 1:
                raise AcceptanceError(f"the {label} lost or duplicated the fixture-secret node")
            if typed_secret[0].get("role") != "securetextfield":
                raise AcceptanceError(f"the {label} changed the fixture-secret role")
            if "value" in typed_secret[0]:
                raise AcceptanceError(f"the secure field value leaked into the {label}")
            # The sentinel scan covers the complete document, including the
            # settled/snapshot wrapper, not only the node list.
            if secret_value in json.dumps(document):
                raise AcceptanceError(f"the synthetic sentinel leaked into the {label}")
        if secret_value in client.stderr_text():
            raise AcceptanceError("the synthetic sentinel leaked into server stderr")
        # Only after both states validate are the secure artifacts retained.
        (output / "snapshot-after-secure-type.json").write_text(
            json.dumps(secure_typed, indent=2, sort_keys=True)
        )
        (output / "snapshot-after-secure.json").write_text(
            json.dumps(secure_fresh, indent=2, sort_keys=True)
        )
        for artifact in output.glob("*.json"):
            if secret_value in artifact.read_text():
                raise AcceptanceError(f"the synthetic sentinel leaked into {artifact.name}")
        receipt["text_evidence"] = {
            "typed_values": ["Ashnazg", "-fixture", ""],
            "append_proof": {
                "first": "Ashnazg",
                "second": "-fixture",
                "observed": "Ashnazg-fixture",
                "observed_in": [
                    "second act response",
                    "empty-payload no-op response",
                    "direct fresh ui_snapshot",
                ],
            },
            "selector_from_snapshot": {"testId": "fixture-input", "role": "textfield"},
            "sequence_advanced": {
                "after_tap_screenshot": meta_after.get("snapshotId"),
                "first_type": typed_state.get("snapshotId"),
                "second_type": appended_state.get("snapshotId"),
            },
            "secure_redaction": {
                "target": {"testId": "fixture-secret", "role": "securetextfield"},
                "settled": True,
                "value_key_absent_from": [
                    "act response snapshot",
                    "direct fresh ui_snapshot",
                    "retained json artifacts",
                    "server stderr",
                    "this receipt",
                ],
                "payload": "<redacted synthetic sentinel>",
                "native_diagnostics_unverified": (
                    "xcodebuild/xcresult diagnostics retained by Xcode were not scanned for"
                    " the synthetic sentinel; this verifier only proves redaction of bridge"
                    " snapshots and the evidence it owns"
                ),
            },
        }

        # Bad-input boundaries: a non-editable target, a selector with no
        # match, and a type without text are each rejected cleanly.
        expect_tool_rejected(
            call(
                "ui_act",
                {"action": "type", "target": {"testId": "fixture-increment"}, "text": "x"},
                REQUEST_TIMEOUT,
                allow_error=True,
            ),
            "type into the non-editable increment button",
        )
        expect_tool_rejected(
            call(
                "ui_act",
                {"action": "type", "target": {"testId": "fixture-missing"}, "text": "x"},
                REQUEST_TIMEOUT,
                allow_error=True,
            ),
            "type into a selector with no match",
        )
        expect_tool_rejected(
            call(
                "ui_act",
                {"action": "type", "target": {"testId": "fixture-input"}},
                REQUEST_TIMEOUT,
                allow_error=True,
            ),
            "type without text",
        )

        # Explicit iOS-mode surface: an action that validates against the
        # shared schema but has no iOS mapping, and the disabled Android
        # tool, must each fail cleanly (JSON-RPC error or MCP isError).
        expect_tool_rejected(
            call(
                "ui_act",
                {"action": "clear", "target": selector},
                REQUEST_TIMEOUT,
                allow_error=True,
            ),
            "the clear action",
        )
        expect_tool_rejected(
            call(
                "app_install",
                {"path": "/nonexistent.apk", "launch": False},
                REQUEST_TIMEOUT,
                allow_error=True,
            ),
            "the disabled app_install tool",
        )

        receipt["status"] = "tools_verified"
    except AcceptanceError as error:
        failure = str(error).replace(secret_value, "<redacted synthetic sentinel>")
    finally:
        # EOF: the deterministic close path must verify cleanup. Keep working
        # when no descriptor ever arrived (startup failure).
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        exit_code: int | None = None
        try:
            exit_code = process.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30.0)
            failure = failure or "server did not exit after EOF within the deadline"
        # Startup errors can occur after ownership was recorded and before
        # initialize answered. Verify that device identity on either path.
        if descriptor is None:
            try:
                descriptor = client.session_descriptor()
            except AcceptanceError:
                descriptor = None
        receipt["cleanup"] = {
            "process_exit_code": exit_code,
            "stderr_tail": client.stderr_text()[-4096:].replace(
                secret_value, "<redacted synthetic sentinel>"
            ),
        }
        if descriptor is not None:
            work_dir = Path(descriptor["workDir"])
            receipt["session"] = {
                "bridge_port": descriptor["bridgePort"],
                "work_dir": str(work_dir),
                "pid": descriptor["pid"],
            }
            receipt["cleanup"]["work_dir_removed"] = not work_dir.exists()
            remaining = device_udids()
            receipt["cleanup"]["simulator_deleted"] = (
                descriptor["simulator"]["udid"] not in remaining
            )
            if failure is not None and (work_dir / "xcodebuild.log").is_file():
                with (work_dir / "xcodebuild.log").open("rb") as log:
                    end = log.seek(0, os.SEEK_END)
                    log.seek(max(0, end - 128 * 1024))
                    lines = log.read().decode("utf-8", errors="replace").splitlines()
                receipt["native_failure_lines"] = [
                    line.replace(secret_value, "<redacted synthetic sentinel>")[:512]
                    for line in lines
                    if "token" not in line.lower()
                    and any(term in line.lower() for term in (
                        "error:", "failed", "coresimulator.simerror", "** test"
                    ))
                ][-16:]
        if failure is None:
            if exit_code != 0:
                failure = f"server exited with {exit_code} on clean EOF"
            elif descriptor is None:
                failure = "server never opened the iOS fixture session"
            elif not receipt["cleanup"]["work_dir_removed"]:
                failure = "the owned work directory was not removed on close"
            elif not receipt["cleanup"]["simulator_deleted"]:
                failure = "the owned simulator was not deleted on close"

    receipt["status"] = "failed" if failure is not None else "passed"
    receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if failure is not None:
        receipt["error"] = failure
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True))
    if failure is not None:
        raise AcceptanceError(failure)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path("target/debug/ashnazg"))
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    source_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        receipt = run_acceptance(arguments.binary, arguments.output_dir, source_sha)
    except AcceptanceError as error:
        print(f"ios mcp fixture acceptance FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2, sort_keys=True))
    print(f"ios mcp fixture acceptance PASSED; evidence at {arguments.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
