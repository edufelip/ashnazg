"""Bounded loopback-only MCP acceptance client; no SDK or ADB integration."""
import base64
import json
import urllib.error
import urllib.request

MAX_RESPONSE = 16 * 1024 * 1024


class ProofFailure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ProofFailure("unexpected HTTP redirect")


def decode_response(raw):
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        try:
            text = raw.decode("utf-8")
            events = []
            for block in text.replace("\r\n", "\n").split("\n\n"):
                lines = [line[5:].lstrip(" ") for line in block.splitlines() if line.startswith("data:")]
                if lines:
                    events.append(json.loads("\n".join(lines)))
            if len(events) != 1:
                raise ProofFailure("unexpected MCP event count")
            value = events[0]
        except (ValueError, UnicodeError):
            raise ProofFailure("invalid MCP response") from None
    if not isinstance(value, dict):
        raise ProofFailure("invalid MCP response object")
    return value


class McpHttpClient:
    def __init__(self, address, bearer):
        host, separator, port = address.rpartition(":")
        if host != "127.0.0.1" or separator != ":" or not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ProofFailure("invalid owned MCP listener")
        self.url = "http://" + address + "/mcp"
        self.bearer = bearer
        self.session = None
        self.version = None
        self.request_id = 0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __repr__(self):
        return "McpHttpClient(<private bearer>)"

    def post(self, message, authenticate=True, timeout=190):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if authenticate:
            headers["Authorization"] = "Bearer " + self.bearer
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.version:
            headers["MCP-Protocol-Version"] = self.version
        request = urllib.request.Request(self.url, data=json.dumps(message, separators=(",", ":")).encode(), headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                status, response_headers, raw = response.status, response.headers, response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as response:
            status, response_headers, raw = response.code, response.headers, response.read(MAX_RESPONSE + 1)
        except (urllib.error.URLError, OSError):
            raise ProofFailure("MCP HTTP request failed") from None
        if len(raw) > MAX_RESPONSE:
            raise ProofFailure("MCP response exceeded bound")
        if self.bearer and self.bearer.encode() in raw:
            raise ProofFailure("MCP response exposed bearer")
        return status, response_headers, decode_response(raw) if raw else None

    def request(self, method, params=None, allow_error=False):
        self.request_id += 1
        message = {"jsonrpc": "2.0", "id": self.request_id, "method": method}
        if params is not None:
            message["params"] = params
        status, headers, value = self.post(message)
        if status != 200 or value is None or value.get("id") != self.request_id:
            raise ProofFailure("MCP request status or correlation failed")
        if "error" in value and not allow_error:
            raise ProofFailure("MCP request returned an error")
        if value.get("result", {}).get("isError") and not allow_error:
            raise ProofFailure("MCP tool returned an error")
        return headers, value

    def initialize(self):
        initial = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "ashnazg-native-mcp-acceptance", "version": "1"}}
        status, _, _ = self.post({"jsonrpc": "2.0", "id": 9000, "method": "initialize", "params": initial}, authenticate=False, timeout=10)
        if status != 401:
            raise ProofFailure("MCP accepted missing bearer")
        headers, response = self.request("initialize", initial)
        self.session = headers.get("Mcp-Session-Id")
        self.version = response.get("result", {}).get("protocolVersion")
        if not isinstance(self.version, str) or not self.version:
            raise ProofFailure("MCP protocol negotiation failed")
        status, _, _ = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if status not in (200, 202, 204):
            raise ProofFailure("MCP initialization notification failed")
        return response

    def tool(self, name, arguments, allow_error=False):
        return self.request("tools/call", {"name": name, "arguments": arguments}, allow_error)[1]


def structured(response):
    value = response.get("result", {}).get("structuredContent")
    if not isinstance(value, dict):
        raise ProofFailure("tool omitted structured result")
    return value


def screenshot(response):
    content = response.get("result", {}).get("content", [])
    images = [item for item in content if item.get("type") == "image" and item.get("mimeType") == "image/png"]
    if len(images) != 1:
        raise ProofFailure("screenshot omitted one PNG")
    try:
        image = base64.b64decode(images[0]["data"], validate=True)
    except (KeyError, ValueError):
        raise ProofFailure("invalid screenshot encoding") from None
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ProofFailure("invalid PNG signature")
    metadata = [item for item in content if item.get("type") == "text"]
    if len(metadata) != 1:
        raise ProofFailure("screenshot omitted metadata")
    try:
        metadata = json.loads(metadata[0]["text"])
    except (KeyError, ValueError):
        raise ProofFailure("invalid screenshot metadata") from None
    return image, metadata
