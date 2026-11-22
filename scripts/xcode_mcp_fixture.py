"""Capture an owned iOS fixture through Apple MCP after simctl installs it."""

import argparse
import hashlib
import itertools
import json
import re
import subprocess
import time
from pathlib import Path

from scripts.native_cli_acceptance import StdioClient, sanitize
from scripts.xcode_mcp_inventory import InventoryError, list_tools

BUNDLE = "dev.ashnazg.ios.fixture"
CAPTURE_LIMIT = 16 * 1024 * 1024


def tool_caller(client):
    ids = itertools.count(20)

    def call(name, arguments):
        response = client.request({
            "jsonrpc": "2.0", "id": next(ids), "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, timeout=180)
        result = response.get("result", {})
        if "error" in response or result.get("isError"):
            raise InventoryError(f"{name} rejected: {response.get('error', result.get('content'))}")
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            raise InventoryError(f"{name} did not return structured output")
        return payload

    return call


def retain_file(result, key, output):
    value = result.get(key)
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise InventoryError(f"{key} did not identify a local file")
    path = Path(value)
    if not path.is_file():
        raise InventoryError(f"{key} was unavailable on this host")
    with path.open("rb") as stream:
        data = stream.read(CAPTURE_LIMIT + 1)
    if not data or len(data) > CAPTURE_LIMIT:
        raise InventoryError(f"{key} exceeded the capture bounds")
    if key == "screenshotPath" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise InventoryError("screenshotPath did not contain a PNG")
    output.write_bytes(data)
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def target_point(hierarchy, identifier, label):
    lines = hierarchy.splitlines()
    escaped_id = re.escape(identifier)
    id_field = re.compile(
        r"\b(?:identifier|accessibilityIdentifier)\s*[:=]\s*(['\"])" + escaped_id + r"\1"
    )
    identifiers = [line for line in lines if id_field.search(line)]
    matches = identifiers or [line for line in lines if f'"{label}"' in line or f"'{label}'" in line]
    if len(matches) != 1:
        raise InventoryError(f"the fixture requires a unique target for {identifier}")
    point = re.search(r"\b(?:hitPoint|center)\s*:\s*\{([0-9.]+),\s*([0-9.]+)\}", matches[0])
    if point is None or any(not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) for value in point.groups()):
        raise InventoryError(f"the hierarchy did not provide a usable point for {identifier}")
    return " ".join(point.groups()), "identifier" if identifiers else "label"


def exercise_fixture(call, arguments, output, receipt):
    before = (output / "before-hierarchy.txt").read_text()
    if "Count: 0" not in before:
        raise InventoryError("the pre-tap hierarchy did not show Count: 0")
    point, mode = target_point(before, "fixture-increment", "Increment")
    receipt["counter_selector_basis"] = mode
    begin = time.monotonic()
    acted = call("DeviceInteractionSynthesize", {**arguments, "interactionCommand": f"t {point}"})
    receipt["after_tap_png"] = retain_file(acted, "screenshotPath", output / "after-tap.png")
    receipt["after_tap_hierarchy"] = retain_file(acted, "hierarchyPath", output / "after-tap-hierarchy.txt")
    after = (output / "after-tap-hierarchy.txt").read_text()
    if "Count: 1" not in after or "Count: 0" in after:
        raise InventoryError("the tap did not change the observed counter from 0 to 1")
    if receipt["screenshot"]["sha256"] == receipt["after_tap_png"]["sha256"]:
        raise InventoryError("the post-tap frame repeated the pre-tap PNG")
    receipt["tap_to_validated_png_seconds"] = round(time.monotonic() - begin, 6)
    receipt["counter_transition"] = [0, 1]

    point, mode = target_point(after, "fixture-input", "Probe text")
    receipt["text_selector_basis"] = mode
    marker = "AshnazgAppleProbe"
    if marker in after:
        raise InventoryError("the text marker was already in the hierarchy")
    typed = call("DeviceInteractionSynthesize", {
        **arguments, "interactionCommand": f"t {point} sender keyboard kbd {marker}",
    })
    receipt["after_type_png"] = retain_file(typed, "screenshotPath", output / "after-type.png")
    receipt["after_type_hierarchy"] = retain_file(typed, "hierarchyPath", output / "after-type-hierarchy.txt")
    if marker not in (output / "after-type-hierarchy.txt").read_text():
        raise InventoryError("the typed marker did not appear in the hierarchy")
    receipt["typing_observed"] = True


def observe_fixture(call, simulator, output, exercise=False):
    receipt = {"status": "blocked", "simulator": simulator}
    started = time.monotonic()
    session_key = None
    try:
        session = call("DeviceInteractionStartSession", {
            "deviceIdentifier": simulator, "sessionIdentifier": "Ashnazg Fixture Contract",
        })
        session_key = session.get("interactionSessionKey")
        if not isinstance(session_key, str) or not session_key:
            session_key = None
            raise InventoryError("start session omitted the interaction key")
        device = session.get("deviceUUID")
        if session.get("deviceIsSimulator") is not True or not isinstance(device, str) or device.lower() != simulator.lower():
            raise InventoryError("Apple selected a device other than the owned simulator")
        begin = time.monotonic()
        arguments = {"interactSessionKey": session_key, "activationBundleId": BUNDLE}
        state = call("DeviceInteractionSynthesize", arguments)
        receipt["capture_seconds"] = round(time.monotonic() - begin, 6)
        receipt["application_state"] = state.get("applicationState")
        receipt["screenshot"] = retain_file(state, "screenshotPath", output / "before.png")
        receipt["hierarchy"] = retain_file(state, "hierarchyPath", output / "before-hierarchy.txt")
        receipt["status"] = "captured"
        if exercise:
            exercise_fixture(call, arguments, output, receipt)
            receipt["status"] = "exercised"
    except (OSError, RuntimeError) as error:
        message = str(error).replace(session_key, "[redacted]") if session_key else str(error)
        receipt["error"] = sanitize(message)[:2000]
    finally:
        if session_key:
            try:
                call("DeviceInteractionEndSession", {"interactionSessionKey": session_key})
                receipt["end_session"] = "passed"
            except (OSError, RuntimeError):
                receipt["end_session"] = "failed"
                receipt["status"] = "blocked"
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 6)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulator", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exercise", action="store_true", help="Verify one tap and non-secret text entry")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        ["xcrun", "mcpbridge"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    client = StdioClient(process)
    try:
        list_tools(client)
        receipt = observe_fixture(tool_caller(client), args.simulator, args.output, exercise=args.exercise)
    except (OSError, RuntimeError) as error:
        receipt = {"status": "blocked", "error": sanitize(str(error))[:2000]}
    finally:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            receipt.update(status="blocked", cleanup_error="bridge did not exit on stdin EOF")
        receipt["bridge_exit_code"] = process.returncode
    (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(f"Apple MCP fixture state: {receipt['status']}")
    return int(args.exercise and receipt["status"] != "exercised")


if __name__ == "__main__":
    raise SystemExit(main())
