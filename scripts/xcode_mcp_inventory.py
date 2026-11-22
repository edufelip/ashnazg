"""Capture Xcode MCP protocol admission and advertised tool contracts."""

import argparse
import json
import subprocess
import time
from pathlib import Path

from scripts.native_cli_acceptance import StdioClient, sanitize


class InventoryError(RuntimeError):
    pass


def list_tools(client):
    def request(request_id, method, params):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        response = client.request(message, timeout=20)
        if "error" in response:
            raise InventoryError(f"{method} rejected: {sanitize(response['error'])}")
        if not isinstance(response.get("result"), dict):
            raise InventoryError(f"{method} returned a malformed result")
        return response["result"]

    initialized = request(1, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "ashnazg-xcode-inventory", "version": "1"},
    })
    if not isinstance(initialized.get("protocolVersion"), str):
        raise InventoryError("initialize omitted protocolVersion")
    client.notification({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools, cursors, params = [], set(), {}
    for request_id in range(2, 18):
        page = request(request_id, "tools/list", params)
        entries = page.get("tools")
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise InventoryError("tools/list omitted its tool array")
        tools.extend(entries)
        cursor = page.get("nextCursor")
        if cursor is None:
            return {"initialize": initialized, "tools": tools}
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise InventoryError("tools/list returned an invalid or repeated cursor")
        cursors.add(cursor)
        params = {"cursor": cursor}
    raise InventoryError("tools/list exceeded the pagination bound")


def inventory(command):
    started = time.monotonic()
    receipt = {"status": "blocked", "command": command}
    client = process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        client = StdioClient(process)
        receipt.update(list_tools(client))
        receipt["status"] = "listed"
    except (OSError, RuntimeError) as error:
        receipt["error"] = str(sanitize(str(error)))[:2000]
    finally:
        if process is not None:
            if process.stdin:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                receipt["status"] = "blocked"
                receipt["cleanup_error"] = "bridge did not exit on stdin EOF"
            receipt["exit_code"] = process.returncode
            receipt["stderr_tail"] = sanitize(client.stderr.decode("utf-8", errors="replace")[-4000:])
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = inventory(["xcrun", "mcpbridge"])
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"Xcode MCP inventory: {result['status']}; {len(result.get('tools', []))} advertised tools")


if __name__ == "__main__":
    main()
