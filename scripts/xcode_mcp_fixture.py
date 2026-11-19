"""Capture an owned iOS fixture through Apple MCP after simctl installs it."""

import argparse
import hashlib
import itertools
import json
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


def observe_fixture(call, simulator, output):
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
        state = call("DeviceInteractionSynthesize", {
            "interactSessionKey": session_key, "activationBundleId": BUNDLE,
        })
        receipt["capture_seconds"] = round(time.monotonic() - begin, 6)
        receipt["application_state"] = state.get("applicationState")
        receipt["screenshot"] = retain_file(state, "screenshotPath", output / "before.png")
        receipt["hierarchy"] = retain_file(state, "hierarchyPath", output / "before-hierarchy.txt")
        receipt["status"] = "captured"
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
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        ["xcrun", "mcpbridge"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    client = StdioClient(process)
    try:
        list_tools(client)
        receipt = observe_fixture(tool_caller(client), args.simulator, args.output)
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


if __name__ == "__main__":
    main()
