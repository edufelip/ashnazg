#!/usr/bin/env python3
"""Real-device protocol and MCP acceptance checks for Ashnazg v0.1."""

from __future__ import annotations

import argparse
import base64
import json
import os
import select
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.harness import DeviceHarness, HarnessError, artifact_apks

MAX_MESSAGE = 8 * 1024 * 1024
FIXTURE_LOG_TAG = "AshnazgFixture"


class IntegrationFailure(RuntimeError):
    pass


class RpcClient:
    def __init__(self, token: str, timeout: float = 10.0):
        self.token = token
        self.timeout = timeout
        self.sock = socket.create_connection(("127.0.0.1", 7777), timeout=timeout)
        self.file = self.sock.makefile("rb")
        self.next_id = 1
        self.last_snapshot: dict | None = None
        self.last_logs: list[dict] = []

    def send(self, method: str, params: dict | None = None, token: str | None = None,
             request_id: int | None = None) -> int:
        request_id = request_id if request_id is not None else self.next_id
        self.next_id = max(self.next_id, request_id + 1)
        payload = {"jsonrpc": "2.0", "id": request_id, "token": self.token if token is None else token,
                   "method": method, "params": params or {}}
        wire = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        if len(wire) > MAX_MESSAGE:
            raise ValueError("request is over the protocol message limit")
        self.sock.sendall(wire)
        return request_id

    def response(self, expected_id: int | None = None) -> dict:
        self.sock.settimeout(self.timeout)
        line = self.file.readline(MAX_MESSAGE + 1)
        if not line:
            raise IntegrationFailure("guest closed the RPC connection")
        if len(line) > MAX_MESSAGE:
            raise IntegrationFailure("guest response exceeded 8 MiB")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IntegrationFailure(f"guest returned malformed JSON: {exc}") from exc
        if response.get("jsonrpc") != "2.0":
            raise IntegrationFailure(f"unexpected JSON-RPC envelope: {response}")
        if expected_id is not None and response.get("id") != expected_id:
            raise IntegrationFailure(f"correlation mismatch: expected {expected_id}, got {response.get('id')}")
        return response

    def call(self, method: str, params: dict | None = None) -> dict:
        request_id = self.send(method, params)
        response = self.response(request_id)
        if "error" in response:
            raise RpcFailure(response["error"], method)
        if "result" not in response:
            raise IntegrationFailure(f"response lacks result: {response}")
        result = response["result"]
        snapshot = result if method == "ui_snapshot" else result.get("snapshot") if method == "ui_act" else None
        if isinstance(snapshot, dict) and isinstance(snapshot.get("nodes"), list):
            self.last_snapshot = snapshot
        if method == "app_logs" and isinstance(result.get("lines"), list):
            params = params or {}
            self.last_logs.append({"tagFilter": params.get("tagFilter"), "sinceMs": params.get("sinceMs"),
                                   "lines": [str(line)[:1000] for line in result["lines"][:100]]})
        return result

    def close(self) -> None:
        self.file.close()
        self.sock.close()


class RpcFailure(IntegrationFailure):
    def __init__(self, error: dict, method: str):
        self.error = error
        self.method = method
        super().__init__(f"{method}: RPC error {error.get('code')}: {error.get('message')}")


def act(client: RpcClient, params: dict) -> dict:
    try:
        result = client.call("ui_act", params)
    except RpcFailure as exc:
        raise IntegrationFailure(f"ui_act action {params.get('action', '<missing>')} failed: {exc}") from exc
    if result.get("settled") is not True or not isinstance(result.get("snapshot", {}).get("nodes"), list):
        raise IntegrationFailure(f"ui_act must return a settled fresh snapshot: {result}")
    return result["snapshot"]


def expect_rpc_error(client: RpcClient, method: str, params: dict | None = None,
                     *, token: str | None = None, code: int | None = None,
                     message_contains: str | None = None) -> dict:
    request_id = client.send(method, params, token=token)
    response = client.response(request_id)
    if "error" not in response:
        raise IntegrationFailure(f"expected RPC error for {method}, received {response}")
    if code is not None and response["error"].get("code") != code:
        raise IntegrationFailure(f"{method} error code should be {code}, got {response['error']}")
    if message_contains and message_contains.casefold() not in response["error"].get("message", "").casefold():
        raise IntegrationFailure(f"{method} error should mention {message_contains!r}: {response['error']}")
    return response["error"]


def find_node(snapshot: dict, *, test_id: str | None = None, text: str | None = None) -> dict:
    nodes = snapshot.get("nodes")
    if not isinstance(nodes, list):
        raise IntegrationFailure(f"ui_snapshot has no nodes list: {snapshot}")
    matches = [node for node in nodes if (test_id is None or node.get("testId") == test_id)
               and (text is None or node.get("text") == text)]
    if len(matches) != 1:
        raise IntegrationFailure(f"expected exactly one node for testId={test_id!r}, text={text!r}; got {len(matches)}")
    return matches[0]


def assert_field_text(snapshot: dict, test_id: str, expected: str) -> None:
    field = find_node(snapshot, test_id=test_id)
    actual = field.get("text", "")
    if actual != expected:
        raise IntegrationFailure(f"field {test_id!r} text mismatch: expected {expected!r}, got {actual!r}")


def assert_button_label(snapshot: dict, test_id: str, expected: str) -> None:
    button = find_node(snapshot, test_id=test_id)
    if button.get("text") == expected or button.get("contentDescription") == expected:
        return
    rect = button.get("rect")
    if not isinstance(rect, list) or len(rect) != 4:
        raise IntegrationFailure(f"button {test_id!r} has no bounds for label association")
    for node in snapshot.get("nodes", []):
        if node.get("text") != expected and node.get("contentDescription") != expected:
            continue
        child_rect = node.get("rect")
        if (isinstance(child_rect, list) and len(child_rect) == 4 and
                rect[0] <= child_rect[0] and rect[1] <= child_rect[1] and
                child_rect[2] <= rect[2] and child_rect[3] <= rect[3]):
            return
    raise IntegrationFailure(f"button {test_id!r} did not expose expected state label {expected!r}")


def require_tagged_log_evidence(lines: list[str], tag: str, evidence: str) -> None:
    if not lines:
        raise IntegrationFailure(f"app_logs returned no lines for literal tag {tag!r}")
    if any(tag not in line for line in lines):
        raise IntegrationFailure(f"app_logs tagFilter {tag!r} returned a line without that literal tag")
    if not any(evidence in line for line in lines):
        raise IntegrationFailure(f"app_logs missed expected tagged evidence {evidence!r}")


def safe_snapshot(snapshot: dict) -> dict:
    """Keep useful fixture diagnostics while excluding password fields and oversized text."""
    nodes = []
    for node in snapshot.get("nodes", []):
        test_id = str(node.get("testId", ""))
        role = str(node.get("role", ""))
        password = "password" in test_id.casefold() or "password" in role.casefold()
        item = {key: node.get(key) for key in ("id", "testId", "role", "rect", "enabled", "focused", "clickable")
                if key in node and not (password and key in ("testId", "role"))}
        if not password and "text" in node:
            item["text"] = str(node["text"])[:120]
        nodes.append(item)
    return {"snapshotId": snapshot.get("snapshotId"), "nodes": nodes[:300],
            "nodes_truncated": len(nodes) > 300}


def verify_rpc(client: RpcClient, artifact_dir: Path) -> list[str]:
    passed: list[str] = []
    ping = client.call("ping")
    if ping.get("version") != "0.1.0" or not isinstance(ping.get("apiLevel"), int):
        raise IntegrationFailure(f"ping contract mismatch: {ping}")
    passed.append("ping")

    first = client.call("ui_snapshot")
    count = find_node(first, text="Count: 0")
    increment = find_node(first, test_id="dev.ashnazg.fixture:id/increment")
    if not increment.get("enabled"):
        raise IntegrationFailure("fixture increment button is not enabled")
    passed.append("semantic_snapshot")
    password = find_node(first, test_id="dev.ashnazg.fixture:id/password")
    if password.get("text") or any("secret-fixture-sentinel" in str(node.get("text", "")) for node in first["nodes"]):
        raise IntegrationFailure("password snapshot exposed its secure value")
    expect_rpc_error(client, "ui_act", {"action": "type", "target": {"testId": "dev.ashnazg.fixture:id/password"},
                                         "text": "new-secret"}, message_contains="clear the password field")
    act(client, {"action": "clear", "target": {"testId": "dev.ashnazg.fixture:id/password"}})
    act(client, {"action": "type", "target": {"testId": "dev.ashnazg.fixture:id/password"}, "text": "new-secret"})
    password_after_type = client.call("ui_snapshot")
    if any(value in str(node.get("text", "")) for node in password_after_type["nodes"]
           for value in ("secret-fixture-sentinel", "new-secret")):
        raise IntegrationFailure("password snapshot returned secret text after clear and type")
    act(client, {"action": "clear", "target": {"testId": "dev.ashnazg.fixture:id/password"}})
    passed.append("password_redaction_and_clear_type")

    # Two queued messages verify a single persistent TCP connection and response correlation.
    a = client.send("ping", request_id=9101)
    b = client.send("ui_snapshot", request_id=9102)
    if client.response(a).get("result", {}).get("version") != "0.1.0":
        raise IntegrationFailure("first pipelined response was not correlated to ping")
    second = client.response(b)
    if second.get("id") != b or not isinstance(second.get("result", {}).get("nodes"), list):
        raise IntegrationFailure("second pipelined response was not correlated to ui_snapshot")
    passed.append("persistent_correlation")

    tapped = act(client, {"action": "tap", "target": {"testId": "dev.ashnazg.fixture:id/increment"}})
    find_node(tapped, text="Count: 1")
    after_tap = client.call("ui_snapshot")
    find_node(after_tap, text="Count: 1")
    passed.append("tap_and_updated_count")

    marked_button = find_node(after_tap, test_id="dev.ashnazg.fixture:id/increment")
    image = client.call("ui_screenshot", {"format": "png", "somOverlay": True})
    verify_marks(image)
    if not any(mark.get("id") == marked_button["id"] for mark in image["marks"]):
        raise IntegrationFailure("overlay marks do not include the visible increment button")
    fresh_tap = act(client, {"action": "tap", "target": {"id": marked_button["id"], "snapshotId": image["snapshotId"]}})
    find_node(fresh_tap, text="Count: 2")
    passed.append("screenshot_mark_freshness")

    long_pressed = act(client, {"action": "long_press", "target": {"text": "Long press target"}})
    find_node(long_pressed, text="Long press complete")
    passed.append("long_press")

    act(client, {"action": "type", "target": {"testId": "dev.ashnazg.fixture:id/input"}, "text": "Ashnazg 你好"})
    typed = client.call("ui_snapshot")
    assert_field_text(typed, "dev.ashnazg.fixture:id/input", "Ashnazg 你好")
    act(client, {"action": "clear", "target": {"testId": "dev.ashnazg.fixture:id/input"}})
    cleared = client.call("ui_snapshot")
    assert_field_text(cleared, "dev.ashnazg.fixture:id/input", "")
    passed.extend(["type_unicode", "clear"])

    scrolled = act(client, {"action": "scroll_to", "target": {"text": "Scroll target"}, "direction": "down"})
    find_node(scrolled, text="Scroll target")
    passed.append("scroll_to")

    # Confirm stale IDs are rejected after a new snapshot has been taken.
    stale = {"action": "tap", "target": {"id": count["id"], "snapshotId": first["snapshotId"]}}
    expect_rpc_error(client, "ui_act", stale, message_contains="stale")
    passed.append("stale_id_rejected")

    # Duplicate text is intentionally a valid selector shape but must not pick either node.
    expect_rpc_error(client, "ui_act", {"action": "tap", "target": {"text": "Duplicate target"}}, message_contains="ambiguous")
    passed.append("ambiguous_selector_rejected")

    act(client, {"action": "deep_link", "text": "ashnazg://fixture"})
    linked = client.call("ui_snapshot")
    if not any("Deep link" in str(node.get("text", "")) for node in linked["nodes"]):
        raise IntegrationFailure("deep link did not open the fixture route")
    backed_out = act(client, {"action": "back"})
    find_node(backed_out, text="Count: 2")
    if any(node.get("text") == "Deep link" for node in backed_out["nodes"]):
        raise IntegrationFailure("back did not return from the deep-link activity to the fixture")
    passed.extend(["back", "deep_link"])

    # Literal filtering is checked with a sentinel tag, then a real nonmatching literal.
    logs = client.call("app_logs", {"tagFilter": FIXTURE_LOG_TAG, "sinceMs": 0})
    if not isinstance(logs.get("lines"), list) or not isinstance(logs.get("truncated"), bool):
        raise IntegrationFailure(f"app_logs response shape mismatch: {logs}")
    try:
        require_tagged_log_evidence(logs["lines"], FIXTURE_LOG_TAG, "count=2")
    except IntegrationFailure as exc:
        raise IntegrationFailure(f"{exc}: {logs}") from exc
    filtered = client.call("app_logs", {"tagFilter": "Ashnazg.*", "sinceMs": 0})
    if filtered.get("lines"):
        raise IntegrationFailure("app_logs tag filter was not literal or returned unrelated lines")
    recent = client.call("app_logs", {"tagFilter": FIXTURE_LOG_TAG, "sinceMs": 9_999_999_999_999})
    if recent.get("lines"):
        raise IntegrationFailure("app_logs sinceMs filtering returned only old entries")
    passed.append("logs_shape_literal_since")

    expect_rpc_error(client, "ping", token="incorrect-token")
    passed.append("authentication")
    expect_rpc_error(client, "made_up_method", code=-32601)
    expect_rpc_error(client, "ui_act", {"action": "not-real"}, code=-32602)
    passed.append("unknown_method_and_invalid_params")

    # A malformed envelope is rejected without corrupting subsequent requests on the socket.
    client.sock.sendall(b'{"jsonrpc":"1.0","id":9301,"token":"' + client.token.encode() + b'","method":"ping"}\n')
    bad = client.response(9301)
    if "error" not in bad:
        raise IntegrationFailure("malformed JSON-RPC version was accepted")
    client.call("ping")
    passed.append("malformed_envelope")

    # Oversize is checked on a separate connection since a compliant server closes it.
    with socket.create_connection(("127.0.0.1", 7777), timeout=4) as oversized:
        oversized.settimeout(4)
        try:
            oversized.sendall(b" " * (MAX_MESSAGE + 1) + b"\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            answer = oversized.recv(4096)
        except (ConnectionResetError, TimeoutError, socket.timeout) as exc:
            raise IntegrationFailure("oversized request was not rejected before the deadline") from exc
        if answer:
            try:
                error = json.loads(answer.splitlines()[0])
            except (json.JSONDecodeError, IndexError) as exc:
                raise IntegrationFailure("oversized request returned a malformed error") from exc
            if "error" not in error:
                raise IntegrationFailure("oversized request was accepted")
        elif not answer:
            # EOF is an acceptable bounded rejection. Verify another connection remains usable below.
            pass
    probe = RpcClient(client.token)
    try:
        probe.call("ping")
    finally:
        probe.close()
    passed.append("oversized_request_bounded")

    if image.get("mimeType") != "image/png":
        raise IntegrationFailure(f"PNG request returned {image.get('mimeType')!r}")
    png = base64.b64decode(image["data"], validate=True)
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise IntegrationFailure("PNG screenshot has invalid signature")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "fixture-overlay.png").write_bytes(png)
    webp = client.call("ui_screenshot", {"format": "webp", "somOverlay": False})
    webp_bytes = base64.b64decode(webp["data"], validate=True)
    if webp.get("mimeType") != "image/webp" or not webp_bytes.startswith(b"RIFF"):
        raise IntegrationFailure("WebP screenshot has invalid MIME type or RIFF signature")
    (artifact_dir / "fixture.webp").write_bytes(webp_bytes)
    passed.append("png_webp_screenshots_and_overlay_freshness")
    return passed


def verify_marks(image: dict) -> None:
    marks = image.get("marks")
    if not isinstance(marks, list) or not marks:
        raise IntegrationFailure("overlay screenshot returned no semantic marks")
    if not isinstance(image.get("snapshotId"), int):
        raise IntegrationFailure("overlay screenshot omitted its fresh snapshot ID")
    width, height = image.get("width"), image.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise IntegrationFailure("overlay screenshot omitted valid dimensions")
    for mark in marks:
        rect = mark.get("rect")
        if not isinstance(mark.get("id"), int) or not isinstance(rect, list) or len(rect) != 4:
            raise IntegrationFailure(f"overlay mark has invalid ID or geometry: {mark}")
        if not all(isinstance(v, int) for v in rect) or rect[0] >= rect[2] or rect[1] >= rect[3]:
            raise IntegrationFailure(f"overlay mark has invalid rectangle: {mark}")


def read_mcp_line(process: subprocess.Popen, timeout: float = 12.0) -> dict:
    if process.stdout is None:
        raise IntegrationFailure("MCP process stdout is unavailable")
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise IntegrationFailure("MCP server did not respond before deadline")
    line = process.stdout.readline()
    if not line:
        raise IntegrationFailure("MCP server closed stdout")
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise IntegrationFailure(f"MCP stdout included non-JSON data: {line[:200]!r}") from exc


def mcp_send(process: subprocess.Popen, message: dict, response_id: int | None = None,
             *, allow_error: bool = False) -> dict | None:
    if process.stdin is None:
        raise IntegrationFailure("MCP process stdin is unavailable")
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()
    if response_id is None:
        return None
    response = read_mcp_line(process)
    if response.get("id") != response_id:
        raise IntegrationFailure(f"MCP correlation mismatch: expected {response_id}, got {response}")
    if "error" in response and not allow_error:
        raise IntegrationFailure(f"MCP returned error: {response['error']}")
    return response


def verify_mcp(binary: str, harness: DeviceHarness, artifact_dir: Path) -> list[str]:
    # Framework compatibility checks may leave Compose or RN in the foreground.
    # Reset to the native contract fixture before driving MCP DTOs through it.
    setup_client = RpcClient(harness.token)
    try:
        prepare_native_fixture(setup_client, harness)
    finally:
        setup_client.close()
    env = os.environ.copy()
    env.update({"ASHNAZG_TOKEN": harness.token, "ASHNAZG_AGENT_ADDR": "127.0.0.1:7777",
                "ANDROID_SERIAL": harness.serial})
    process = subprocess.Popen([binary, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
    try:
        init = mcp_send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                            "clientInfo": {"name": "ashnazg-integration", "version": "0.1"}}}, 1)
        if not isinstance(init.get("result", {}).get("protocolVersion"), str):
            raise IntegrationFailure(f"MCP initialize did not negotiate a protocol version: {init}")
        mcp_send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = mcp_send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, 2)
        names = {item.get("name") for item in tools.get("result", {}).get("tools", [])}
        expected = {"ui_snapshot", "ui_act", "ui_screenshot", "app_logs", "app_install"}
        if not expected.issubset(names) or "device_boot" in names:
            raise IntegrationFailure(f"MCP tool advertisement mismatch: {sorted(names)}")
        def call_tool(request_id: int, name: str, arguments: dict, *, allow_error: bool = False) -> dict:
            response = mcp_send(process, {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                                          "params": {"name": name, "arguments": arguments}}, request_id,
                                allow_error=allow_error)
            if not allow_error and response.get("result", {}).get("isError"):
                raise IntegrationFailure(f"MCP tool {name} returned an error: {response['result']}")
            return response

        call_tool(3, "ui_snapshot", {})
        call_tool(4, "ui_act", {"action": "type", "target": {"testId": "dev.ashnazg.fixture:id/input"}, "text": "MCP 你好"})
        call_tool(5, "ui_act", {"action": "clear", "target": {"testId": "dev.ashnazg.fixture:id/input"}})
        call_tool(6, "ui_act", {"action": "deep_link", "text": "ashnazg://fixture"})
        call_tool(7, "ui_act", {"action": "back"})
        call_tool(8, "app_logs", {"tagFilter": "AshnazgAgent", "sinceMs": 0})
        failed_action = call_tool(10, "ui_act", {"action": "unsupported-action"}, allow_error=True)
        if "error" not in failed_action and not failed_action.get("result", {}).get("isError"):
            raise IntegrationFailure("MCP accepted an unsupported ui_act action")
        unknown_tool = call_tool(11, "not_a_real_tool", {}, allow_error=True)
        if "error" not in unknown_tool and not unknown_tool.get("result", {}).get("isError"):
            raise IntegrationFailure("MCP accepted an unknown tool name")

        called = call_tool(12, "ui_screenshot", {"format": "png"})
        content = called.get("result", {}).get("content", [])
        image_items = [item for item in content if item.get("type") == "image" and item.get("mimeType") == "image/png"]
        if not image_items:
            raise IntegrationFailure("MCP screenshot tool returned no PNG image content")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "mcp-screenshot.png").write_bytes(base64.b64decode(image_items[0]["data"], validate=True))
        fixture_apk = str((Path(__file__).resolve().parents[1] /
                           "agent-android/fixture/build/outputs/apk/debug/fixture-debug.apk").resolve())
        call_tool(13, "app_install", {"path": fixture_apk, "launch": False})
        launched = call_tool(14, "app_install", {"path": fixture_apk, "launch": True})
        launch_result = launched.get("result", {}).get("structuredContent")
        if not isinstance(launch_result, dict):
            raise IntegrationFailure(f"MCP app_install launch returned no structured result: {launched}")
        if launch_result.get("installed") is not True or launch_result.get("package") != "dev.ashnazg.fixture":
            raise IntegrationFailure(f"MCP app_install did not autodetect and launch the native fixture: {launch_result}")
        deadline = time.monotonic() + 15
        after_install = None
        snapshot_request_id = 15
        while time.monotonic() < deadline:
            after_install = call_tool(snapshot_request_id, "ui_snapshot", {}).get("result", {}).get("structuredContent")
            snapshot_request_id += 1
            if (isinstance(after_install, dict) and
                    any(node.get("testId") == "dev.ashnazg.fixture:id/increment" for node in after_install.get("nodes", [])) and
                    any(node.get("text") == "Count: 0" for node in after_install.get("nodes", []))):
                break
            time.sleep(0.25)
        else:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "failure-snapshot.json").write_text(json.dumps(safe_snapshot(after_install or {}), indent=2) + "\n")
            raise IntegrationFailure(f"MCP app_install launch did not leave the fixture active within 15s: {safe_snapshot(after_install or {})}")
        return ["mcp_initialize_and_tools_list", "mcp_snapshot", "mcp_ui_act_type_clear_deep_link_back",
                "mcp_app_logs", "mcp_app_install_without_launch", "mcp_app_install_autodetect_and_launch",
                "mcp_invalid_action_and_unknown_tool", "mcp_screenshot_image"]
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def verify_http_auth(binary: str, harness: DeviceHarness) -> list[str]:
    env = os.environ.copy()
    env.update({"ASHNAZG_TOKEN": harness.token, "ASHNAZG_AGENT_ADDR": "127.0.0.1:7777",
                "ASHNAZG_HTTP_TOKEN": harness.token, "ANDROID_SERIAL": harness.serial})
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen([binary, "mcp", "--http", f"127.0.0.1:{port}"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, env=env)
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                             "clientInfo": {"name": "ashnazg-http-auth-smoke", "version": "0.1"}}}

    def post(message: dict, *, authenticated: bool, session: str | None = None,
             version: str | None = None) -> tuple[int, dict, dict | None]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if authenticated:
            headers["Authorization"] = f"Bearer {harness.token}"
        if session:
            headers["Mcp-Session-Id"] = session
        if version:
            headers["MCP-Protocol-Version"] = version
        body = json.dumps(message, separators=(",", ":")).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=3)
            status, response_headers, raw = response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            status, response_headers, raw = exc.code, dict(exc.headers.items()), exc.read()
        parsed = None
        if raw:
            content_type = next((value for key, value in response_headers.items() if key.casefold() == "content-type"), "")
            if "text/event-stream" in content_type:
                data_lines = [line[5:].strip() for line in raw.decode("utf-8", "replace").splitlines() if line.startswith("data:")]
                if data_lines:
                    parsed = json.loads("\n".join(data_lines))
            else:
                parsed = json.loads(raw)
        return status, response_headers, parsed
    deadline = time.monotonic() + 12
    status = None
    last_error = None
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise IntegrationFailure(f"MCP HTTP server exited early: {output[-2000:]}")
            try:
                status, _, _ = post(initialize, authenticated=False)
                break
            except (OSError, urllib.error.URLError) as exc:
                last_error = str(exc)
                time.sleep(0.2)
        if status not in (401, 403):
            raise IntegrationFailure(f"MCP HTTP without Bearer token should return 401/403, got {status}: {last_error}")
        status, headers, response = post(initialize, authenticated=True)
        if status != 200 or not response or response.get("id") != 1 or "result" not in response:
            raise IntegrationFailure(f"authenticated MCP HTTP initialize failed: status={status}, response={response}")
        protocol = response["result"].get("protocolVersion")
        session = next((value for key, value in headers.items() if key.casefold() == "mcp-session-id"), None)
        status, _, _ = post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                            authenticated=True, session=session, version=protocol)
        if status not in (200, 202, 204):
            raise IntegrationFailure(f"authenticated MCP HTTP initialized notification failed: {status}")
        status, _, listed = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                                 authenticated=True, session=session, version=protocol)
        names = {tool.get("name") for tool in (listed or {}).get("result", {}).get("tools", [])}
        if status != 200 or "ui_snapshot" not in names:
            raise IntegrationFailure(f"authenticated MCP HTTP tools/list failed: status={status}, response={listed}")
        status, _, called = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "ui_snapshot", "arguments": {}}},
                                 authenticated=True, session=session, version=protocol)
        if status != 200 or not called or called.get("id") != 3 or called.get("result", {}).get("isError"):
            raise IntegrationFailure(f"authenticated MCP HTTP ui_snapshot failed: status={status}, response={called}")
        return ["mcp_http_rejects_unauthenticated_request", "mcp_http_authenticated_initialize_list_snapshot"]
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def verify_framework_fixture(client: RpcClient, harness: DeviceHarness, package: str,
                             activity: str, button_id: str, initial: str, updated: str,
                             artifact_dir: Path) -> list[str]:
    harness.adb_cmd("shell", "am", "force-stop", package)
    harness.adb_cmd("shell", "pm", "clear", package, timeout=30)
    harness.adb_cmd("shell", "am", "start", "-W", "-n", f"{package}/{activity}", timeout=60)
    deadline = time.monotonic() + 15
    snapshot = None
    while time.monotonic() < deadline:
        snapshot = client.call("ui_snapshot")
        if any(node.get("testId") == button_id for node in snapshot.get("nodes", [])):
            break
        time.sleep(0.3)
    else:
        raise IntegrationFailure(f"{package} did not expose semantic test ID {button_id!r}: {snapshot}")
    find_node(snapshot, test_id=button_id)
    assert_button_label(snapshot, button_id, initial)
    changed = act(client, {"action": "tap", "target": {"testId": button_id}})
    assert_button_label(changed, button_id, updated)
    input_id = "compose-input" if package.endswith("compose") else "rn-input"
    act(client, {"action": "type", "target": {"testId": input_id}, "text": "Framework 你好"})
    typed = client.call("ui_snapshot")
    assert_field_text(typed, input_id, "Framework 你好")
    act(client, {"action": "clear", "target": {"testId": input_id}})
    cleared = client.call("ui_snapshot")
    assert_field_text(cleared, input_id, "")
    screenshot = client.call("ui_screenshot", {"format": "png", "somOverlay": True})
    shot = base64.b64decode(screenshot["data"], validate=True)
    if screenshot.get("mimeType") != "image/png" or not shot.startswith(b"\x89PNG\r\n\x1a\n"):
        raise IntegrationFailure(f"{package} screenshot failed PNG validation")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / ("compose.png" if package.endswith("compose") else "react-native.png")).write_bytes(shot)
    return [f"{package}_semantic_id", f"{package}_tap_count", f"{package}_unicode_type_clear", f"{package}_screenshot"]


def prepare_native_fixture(client: RpcClient, harness: DeviceHarness) -> None:
    package = "dev.ashnazg.fixture"
    harness.adb_cmd("shell", "am", "force-stop", package)
    harness.adb_cmd("shell", "pm", "clear", package, timeout=30)
    harness.adb_cmd("shell", "am", "start", "-W", "-n", f"{package}/.MainActivity", timeout=60)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        snapshot = client.call("ui_snapshot")
        if any(node.get("testId") == "dev.ashnazg.fixture:id/increment" for node in snapshot.get("nodes", [])):
            if any(node.get("text") == "Count: 0" for node in snapshot["nodes"]):
                return
        time.sleep(0.25)
    raise IntegrationFailure(f"native fixture did not reach its clean initial state: {safe_snapshot(client.last_snapshot or {})}")


def source_metadata(root: Path) -> dict:
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout.strip()
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
        return {"head": head, "dirty": bool(status), "github_sha": os.environ.get("GITHUB_SHA")}
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "dirty": None, "github_sha": os.environ.get("GITHUB_SHA")}


def relative_artifact_paths(paths: list[Path]) -> list[str]:
    output = []
    for path in paths:
        try:
            output.append(str(path.resolve().relative_to(Path.cwd().resolve())))
        except ValueError:
            output.append(path.name)
    return sorted(output)


def artifact_evidence(run_dir: Path) -> tuple[list[str], list[str]]:
    screenshots = relative_artifact_paths(list(run_dir.glob("*.png")) + list(run_dir.glob("*.webp")))
    diagnostics = relative_artifact_paths(list(run_dir.glob("*.json")))
    return screenshots, diagnostics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--readiness-timeout", type=float, default=45.0)
    parser.add_argument("--no-mcp", action="store_true", help="skip host MCP smoke (RPC checks still run)")
    parser.add_argument("--framework-fixtures", action="store_true",
                        help="also run the real Compose and React Native fixture APKs")
    parser.add_argument("--output", type=Path, help="write the machine-readable acceptance summary")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/integration"),
                        help="directory for screenshot evidence")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}"
    artifact_dir = args.artifacts / run_id
    harness = None
    client = None
    failure_snapshot = None
    failure_logs: list[dict] = []
    try:
        binary = os.environ.get("ASHNAZG_BIN") or str(args.root / "target/debug/ashnazg")
        if not args.no_mcp and not Path(binary).is_file():
            raise HarnessError(f"host binary not found: {binary}; build it or set ASHNAZG_BIN")
        harness = DeviceHarness(args.readiness_timeout)
        agent, fixture = artifact_apks(args.root)
        harness.start(agent, fixture)
        client = RpcClient(harness.token)
        try:
            prepare_native_fixture(client, harness)
            rpc_passed = verify_rpc(client, artifact_dir)
            if args.framework_fixtures:
                compose_apk = args.root / "fixtures/compose/build/outputs/apk/debug/compose-debug.apk"
                rn_apk = args.root / "fixtures/reactnative/build/outputs/apk/debug/reactnative-debug.apk"
                harness.install(compose_apk, rn_apk)
                rpc_passed.extend(verify_framework_fixture(
                    client, harness, "com.ashnazg.fixture.compose", "com.ashnazg.fixture.compose.MainActivity",
                    "compose-button", "Compose count: 0", "Compose count: 1", artifact_dir))
                rpc_passed.extend(verify_framework_fixture(
                    client, harness, "com.ashnazg.fixture.reactnative", "com.ashnazg.fixture.reactnative.MainActivity",
                    "rn-button", "React Native count: 0", "React Native count: 1", artifact_dir))
        finally:
            client.close()
            failure_snapshot = client.last_snapshot
            failure_logs = client.last_logs
            client = None
        mcp_passed = [] if args.no_mcp else verify_http_auth(binary, harness) + verify_mcp(binary, harness, artifact_dir)
        screenshots, diagnostics = artifact_evidence(artifact_dir)
        summary = {"device": harness.serial, "cold_setup_ms": round(harness.cold_setup_ms, 3),
                   "rpc_checks": rpc_passed, "mcp_checks": mcp_passed,
                   "run_mode": {"mcp": not args.no_mcp, "framework_fixtures": args.framework_fixtures},
                   "framework_fixtures": args.framework_fixtures,
                   "run_id": run_id, "source": source_metadata(args.root),
                   "screenshot_evidence": screenshots, "diagnostic_evidence": diagnostics}
        rendered = json.dumps(summary, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n")
        print(rendered)
        return 0
    except (HarnessError, IntegrationFailure, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        if client is not None:
            failure_snapshot = client.last_snapshot
            failure_logs = client.last_logs
        diagnostic_collection_error = None
        if harness is not None:
            try:
                harness.collect_failure_diagnostics(artifact_dir)
            except Exception as diagnostic_exc:
                # Do not let optional diagnostics replace the original acceptance failure.
                message = str(diagnostic_exc).replace(harness.token, "<redacted>") if harness.token else str(diagnostic_exc)
                diagnostic_collection_error = f"{type(diagnostic_exc).__name__}: {message[:300]}"
        if failure_snapshot is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "failure-snapshot.json").write_text(json.dumps(safe_snapshot(failure_snapshot), indent=2) + "\n")
        if failure_logs:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "failure-logs.json").write_text(json.dumps(failure_logs, indent=2) + "\n")
        summary = {"run_id": run_id, "run_mode": {"mcp": not args.no_mcp,
                                                     "framework_fixtures": args.framework_fixtures},
                   "framework_fixtures": args.framework_fixtures,
                   "source": source_metadata(args.root), "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                   "diagnostic_evidence": relative_artifact_paths(list(artifact_dir.glob("*.json")))}
        if diagnostic_collection_error:
            summary["diagnostic_collection_error"] = diagnostic_collection_error
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(summary, indent=2) + "\n")
        print(f"integration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
        if harness is not None:
            harness.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
