#!/usr/bin/env python3
"""Exercise the production Ashnazg stdio MCP CLI in an owned home directory."""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import queue
import re
import select
import signal
import socket
import stat
import struct
import zlib
import zipfile
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2024-11-05"
TOOLS = ("app_install", "app_logs", "ui_act", "ui_screenshot", "ui_snapshot")
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "runtime/mcp-tools.schema.json"
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_STDOUT_BYTES = 32 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_PROCESS_OUTPUT_BYTES = 1024 * 1024
MAX_PROCESS_ROWS = 16 * 1024
REQUEST_TIMEOUT = 35.0
STARTUP_TIMEOUT = 1250.0
STARTUP_RETRY_INTERVAL = 2.0
SHUTDOWN_GRACE = 250.0
KILL_GRACE = 5.0
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SECRET_TEXT_RE = re.compile(
    r"(?i)(ASHNAZG_TOKEN|rpc_token|install_token|authorization|bearer|token)\s*([:= ]\s*)[^\s,}\"]+"
)


class AcceptanceError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize(value: Any) -> Any:
    """Remove credential-shaped fields and redact recognizable secret assignments."""
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if re.search(r"token|authorization|secret|credential_(?:contents|bytes)", str(key), re.I)
            else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return SECRET_TEXT_RE.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", value)
    return value


def bounded_text(value: str, limit: int = 4096) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 16] + "[truncated]"


def _identity(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "other",
    }


def _tree_identity(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    root_identity = _identity(path)
    if root_identity is not None:
        result["."] = root_identity
    pending = [(path, Path("."), 0)]
    visited = 0
    while pending and visited < 512:
        directory, relative, depth = pending.pop(0)
        if depth >= 8:
            continue
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        for child in children:
            if visited >= 512:
                break
            rel = relative / child.name
            if child.name == ".ashnazg-session-v1.json":
                continue
            identity = _identity(child)
            if identity is None:
                continue
            result[rel.as_posix()] = identity
            visited += 1
            if identity["kind"] == "directory":
                pending.append((child, rel, depth + 1))
    return result


def _credential_metadata(state_dir: Path) -> dict[str, Any]:
    credential = state_dir / ".ashnazg-session-v1.json"
    identity = _identity(credential)
    return {"exists": identity is not None, "mode": identity["mode"] if identity else None}


def _preserved_state_identities(before: dict[str, Any], after: dict[str, Any]) -> bool:
    # Runtime receipts and serial logs are rewritten diagnostics. The directory,
    # disk, ownership markers, locks, template, and any other retained files
    # must keep their identities.
    return all(after.get(name) == identity for name, identity in before.items()
               if name not in {"runtime.json", "serial.log"})


def _ports_closed() -> dict[str, bool | None]:
    result = {}
    for port in (15555, 7777, 17778):
        sock = socket.socket()
        sock.settimeout(0.15)
        try:
            code = sock.connect_ex(("127.0.0.1", port))
            result[str(port)] = False if code == 0 else True if code == errno.ECONNREFUSED else None
        except OSError:
            result[str(port)] = None
        finally:
            sock.close()
    return result


def _bounded_ps_output(ps_path: Path, *, seconds: float = 2.0,
                       maximum: int = MAX_PROCESS_OUTPUT_BYTES) -> bytes | None:
    child = None
    deadline = time.monotonic() + seconds
    try:
        child = subprocess.Popen([str(ps_path), "-axo", "pid=,ppid=,comm="], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env={"PATH": "/usr/bin:/bin"})
        assert child.stdout is not None
        output = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([child.stdout], [], [], remaining)[0]:
                return None
            chunk = os.read(child.stdout.fileno(), min(65536, maximum + 1 - len(output)))
            if not chunk:
                return bytes(output) if child.wait(timeout=max(0.001, deadline-time.monotonic())) == 0 else None
            output.extend(chunk)
            if len(output) > maximum:
                return None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        if child is not None:
            if child.poll() is None:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
            try:
                child.wait(timeout=1)
            finally:
                if child.stdout is not None:
                    child.stdout.close()


def _process_descendants(parent_pid: int) -> dict[str, Any]:
    """Return complete bounded PID/PPID/command metadata or unavailable."""
    ps_path = next((candidate for candidate in (Path("/bin/ps"), Path("/usr/bin/ps")) if candidate.is_file()), None)
    if ps_path is None:
        return {"status": "unavailable", "processes": []}
    try:
        output = _bounded_ps_output(ps_path)
    except (OSError, subprocess.TimeoutExpired):
        output = None
    if output is None or len(output) > MAX_PROCESS_OUTPUT_BYTES:
        return {"status": "unavailable", "processes": []}
    lines = output.decode("utf-8", "replace").splitlines()
    if len(lines) > MAX_PROCESS_ROWS:
        return {"status": "unavailable", "processes": []}
    children: dict[int, list[tuple[int, str]]] = {}
    for raw_line in lines:
        fields = raw_line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        command = Path(fields[2]).name[:128]
        children.setdefault(ppid, []).append((pid, command))
    descendants = []
    frontier = [parent_pid]
    seen = {parent_pid}
    while frontier:
        current = frontier.pop(0)
        for pid, command in children.get(current, []):
            if pid in seen:
                continue
            if len(descendants) >= 64:
                return {"status": "unavailable", "processes": []}
            seen.add(pid)
            descendants.append({"pid": pid, "ppid": current, "command": command})
            frontier.append(pid)
    return {"status": "available", "processes": descendants}


def _pids_alive(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for process in processes:
        pid = process.get("pid")
        if not isinstance(pid, int):
            continue
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except OSError:
            alive = True
        results.append({"pid": pid, "command": process.get("command", ""), "alive": alive})
    return results


class StdioClient:
    def __init__(self, process: subprocess.Popen[bytes]):
        self.process = process
        self.frames: queue.Queue[bytes | None | BaseException] = queue.Queue()
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.stderr_truncated = False
        self.stderr = bytearray()
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        try:
            assert self.process.stdout is not None
            while True:
                line = self.process.stdout.readline(MAX_FRAME_BYTES + 1)
                if not line:
                    self.frames.put(None)
                    return
                self.stdout_bytes += len(line)
                if self.stdout_bytes > MAX_STDOUT_BYTES or len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                    self.frames.put(AcceptanceError("CLI stdout exceeded protocol bounds"))
                    return
                self.frames.put(line)
        except BaseException as exc:
            self.frames.put(exc)

    def _read_stderr(self) -> None:
        try:
            assert self.process.stderr is not None
            read1 = getattr(self.process.stderr, "read1", None)
            while True:
                # BufferedReader.read(4096) blocks until 4096 bytes or EOF, stalling
                # on single-line startup descriptors emitted on stderr.
                chunk = read1(4096) if read1 is not None else self.process.stderr.read(4096)
                if not chunk:
                    return
                if self.stderr_bytes + len(chunk) > MAX_STDERR_BYTES:
                    self.stderr_truncated = True
                self.stderr_bytes = min(MAX_STDERR_BYTES, self.stderr_bytes + len(chunk))
                if len(self.stderr) < MAX_STDERR_BYTES:
                    self.stderr.extend(chunk[: MAX_STDERR_BYTES - len(self.stderr)])
        except BaseException:
            return

    def request(self, message: dict[str, Any], *, timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
        if self.process.stdin is None:
            raise AcceptanceError("CLI stdin is unavailable")
        wire = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(wire) > MAX_FRAME_BYTES:
            raise AcceptanceError("MCP request exceeded protocol frame bound")
        try:
            self.process.stdin.write(wire)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AcceptanceError("CLI closed MCP stdin before replying") from exc
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcceptanceError("MCP response timed out")
            try:
                frame = self.frames.get(timeout=remaining)
            except queue.Empty as exc:
                raise AcceptanceError("MCP response timed out") from exc
            if frame is None:
                raise AcceptanceError("CLI closed stdout before MCP response")
            if isinstance(frame, BaseException):
                raise AcceptanceError(str(frame)) from frame
            try:
                response = json.loads(frame)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AcceptanceError("CLI emitted invalid newline-delimited JSON") from exc
            if not isinstance(response, dict):
                raise AcceptanceError("CLI emitted a non-object MCP frame")
            if response.get("id") == message.get("id"):
                return response
            # Notifications and unrelated server messages are retained by count only.

    def notification(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise AcceptanceError("CLI stdin is unavailable")
        wire = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        self.process.stdin.write(wire)
        self.process.stdin.flush()


def _write_json(path: Path, value: Any) -> dict[str, Any]:
    data = (json.dumps(sanitize(value), sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(data)
    return {"path": path.name, "size": len(data), "sha256": sha256_bytes(data)}


def _response_result(response: dict[str, Any], request_id: int) -> Any:
    if response.get("id") != request_id:
        raise AcceptanceError("MCP response ID does not match its request")
    if "error" in response:
        error = response["error"]
        message = error.get("message", "MCP request failed") if isinstance(error, dict) else "MCP request failed"
        raise AcceptanceError(bounded_text(str(message)))
    if "result" not in response:
        raise AcceptanceError("MCP response omitted result")
    return response["result"]


def _tool_payload(result: Any) -> Any:
    if not isinstance(result, dict):
        raise AcceptanceError("MCP tool result is malformed")
    if result.get("isError") is True:
        raise AcceptanceError("MCP tool returned isError")
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError:
                    return item["text"]
    return result


def _tool_call(client: StdioClient, records: list[dict[str, Any]], output: Path,
               protocol: dict[str, Any], name: str, arguments: dict[str, Any], request_id: int) -> tuple[Any, dict[str, Any]]:
    request = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
               "params": {"name": name, "arguments": arguments}}
    response = client.request(request)
    payload_error = None
    try:
        payload = _tool_payload(_response_result(response, request_id))
    except AcceptanceError as exc:
        payload = None
        payload_error = exc
    req_meta = _write_json(output / f"{request_id:03d}-{name}.request.json", request)
    # Preserve screenshot bytes separately and redact the large base64 data in frame evidence.
    sanitized_response = json.loads(json.dumps(response))
    result = sanitized_response.get("result")
    if name == "ui_screenshot" and isinstance(result, dict):
        content = result.get("content", [])
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("type") == "image" and isinstance(item.get("data"), str):
                item["data"] = "[image bytes stored separately]"
    if name == "app_logs" and isinstance(result, dict) and payload_error is None:
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        log_lines = payload.get("lines") if isinstance(payload, dict) else payload
        line_count = len(log_lines) if isinstance(log_lines, list) else None
        sanitized_result = result
        if isinstance(sanitized_result, dict):
            sanitized_result.clear()
            sanitized_result.update({
                "structuredContent": {
                    "line_count": line_count,
                    "truncated": payload.get("truncated") if isinstance(payload, dict) else None,
                    "result_sha256": sha256_bytes(payload_bytes),
                },
                "content": [],
            })
        receipt_logs = {"line_count": line_count,
                        "truncated": payload.get("truncated") if isinstance(payload, dict) else None,
                        "result_sha256": sha256_bytes(payload_bytes)}
        protocol.setdefault("sanitized_tool_results", {})["app_logs"] = receipt_logs
    res_meta = _write_json(output / f"{request_id:03d}-{name}.response.json", sanitized_response)
    records.extend([req_meta, res_meta])
    protocol["requests"].append(sanitize(request))
    protocol["responses"].append(sanitize(sanitized_response))
    if payload_error is not None:
        raise payload_error
    return payload, response


def decode_png(image: bytes) -> dict[str, Any]:
    """Decode bounded non-interlaced 8-bit RGB/RGBA PNGs, including CRC/filter checks."""
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AcceptanceError("invalid PNG signature")
    offset, chunks, header, ended, data_started = 8, 0, None, False, False
    compressed = bytearray()
    while offset < len(image):
        chunks += 1
        if chunks > 4096 or offset + 12 > len(image):
            raise AcceptanceError("invalid PNG framing")
        length, = struct.unpack_from('>I', image, offset)
        kind = image[offset+4:offset+8]
        end = offset + 12 + length
        if end > len(image):
            raise AcceptanceError("truncated PNG chunk")
        body = image[offset+8:offset+8+length]
        crc, = struct.unpack_from('>I', image, end-4)
        if zlib.crc32(kind + body) & 0xffffffff != crc:
            raise AcceptanceError("PNG CRC mismatch")
        if kind == b'IHDR':
            if header is not None or chunks != 1 or length != 13:
                raise AcceptanceError("invalid PNG header")
            width, height, depth, color, comp, filt, interlace = struct.unpack('>IIBBBBB', body)
            if not (0 < width <= 4096 and 0 < height <= 4096 and width * height <= 8 * 1024**2):
                raise AcceptanceError("PNG dimensions exceed bounds")
            if depth != 8 or color not in (2, 6) or comp or filt or interlace:
                raise AcceptanceError("unsupported PNG encoding")
            header = (width, height, 3 if color == 2 else 4)
        elif kind == b'IDAT':
            if header is None or ended:
                raise AcceptanceError("invalid PNG data ordering")
            data_started = True
            compressed.extend(body)
        elif kind == b'IEND':
            if not data_started or length or end != len(image):
                raise AcceptanceError("invalid PNG end")
            ended = True
        elif kind[:1].isupper() and kind != b'PLTE':
            raise AcceptanceError("unknown critical PNG chunk")
        offset = end
    if header is None or not ended:
        raise AcceptanceError("incomplete PNG")
    width, height, channels = header
    stride = width * channels
    expected = height * (stride + 1)
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(bytes(compressed), expected + 1)
    except zlib.error as exc:
        raise AcceptanceError("invalid PNG deflate stream") from exc
    if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise AcceptanceError("PNG decompressed size or stream mismatch")
    pixels = bytearray()
    prior = bytearray(stride)
    for row in range(height):
        position = row * (stride + 1)
        filter_type = raw[position]
        current = bytearray(raw[position+1:position+1+stride])
        if filter_type > 4:
            raise AcceptanceError("invalid PNG row filter")
        for index in range(stride):
            left = current[index-channels] if index >= channels else 0
            up = prior[index]
            diagonal = prior[index-channels] if index >= channels else 0
            if filter_type == 1: prediction = left
            elif filter_type == 2: prediction = up
            elif filter_type == 3: prediction = (left + up) // 2
            elif filter_type == 4:
                guess = left + up - diagonal
                distances = (abs(guess-left), abs(guess-up), abs(guess-diagonal))
                prediction = (left, up, diagonal)[distances.index(min(distances))]
            else: prediction = 0
            current[index] = (current[index] + prediction) & 255
        if channels == 3:
            for index in range(0, len(current), 3):
                pixels.extend(current[index:index+3])
                pixels.append(255)
        else:
            pixels.extend(current)
        prior = current
    return {"width": width, "height": height, "channels": 4, "encoding_channels": channels,
            "pixel_sha256": sha256_bytes(pixels)}


def _screenshot(output: Path, response: dict[str, Any], fallback: Any, records: list[dict[str, Any]], stem: str = "screenshot") -> dict[str, Any]:
    result = response.get("result", {})
    if not isinstance(result, dict):
        raise AcceptanceError("screenshot result is malformed")
    image_b64 = None
    metadata = None
    mime = None
    for item in result.get("content", []) if isinstance(result.get("content"), list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "image":
            image_b64 = item.get("data")
            mime = item.get("mimeType")
        elif item.get("type") == "text" and isinstance(item.get("text"), str):
            try:
                metadata = json.loads(item["text"])
            except json.JSONDecodeError:
                pass
    if image_b64 is None and isinstance(fallback, dict):
        image_b64 = fallback.get("data")
        metadata = fallback
        mime = fallback.get("mimeType")
    if not isinstance(image_b64, str):
        raise AcceptanceError("ui_screenshot omitted image content")
    try:
        image = base64.b64decode(image_b64, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise AcceptanceError("ui_screenshot image is not valid base64") from exc
    if not image or len(image) > 8 * 1024 * 1024:
        raise AcceptanceError("ui_screenshot image is empty or exceeds 8 MiB")
    if mime == "image/png":
        valid = image.startswith(b"\x89PNG\r\n\x1a\n")
        suffix = "png"
    elif mime == "image/webp":
        valid = len(image) >= 12 and image[:4] == b"RIFF" and image[8:12] == b"WEBP"
        suffix = "webp"
    else:
        raise AcceptanceError("ui_screenshot MIME type is unsupported")
    if not valid:
        raise AcceptanceError("ui_screenshot bytes do not match MIME type")
    if not isinstance(metadata, dict):
        raise AcceptanceError("ui_screenshot metadata is missing")
    snapshot_id = metadata.get("snapshotId")
    if not isinstance(snapshot_id, int) or isinstance(snapshot_id, bool) or snapshot_id <= 0:
        raise AcceptanceError("ui_screenshot snapshotId is invalid")
    if (not isinstance(metadata.get("width"), int) or isinstance(metadata.get("width"), bool) or
            metadata["width"] <= 0 or not isinstance(metadata.get("height"), int) or
            isinstance(metadata.get("height"), bool) or metadata["height"] <= 0):
        raise AcceptanceError("ui_screenshot dimensions are missing")
    if not isinstance(metadata.get("marks"), list):
        raise AcceptanceError("ui_screenshot semantic marks are missing")
    if mime != "image/png":
        raise AcceptanceError("acceptance requires decoded PNG evidence")
    decoded = decode_png(image)
    if decoded["width"] != metadata["width"] or decoded["height"] != metadata["height"]:
        raise AcceptanceError("PNG dimensions disagree with screenshot metadata")
    image_path = output / f"{stem}.{suffix}"
    image_path.write_bytes(image)
    item = {"path": image_path.name, "size": len(image), "sha256": sha256_bytes(image)}
    records.append(item)
    return {**item, **decoded, "mime_type": mime, "snapshot_id": snapshot_id,
            "width": metadata["width"], "height": metadata["height"], "marks_count": len(metadata["marks"])}


def _snapshot_nodes(snapshot: Any) -> Any:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("nodes"), list):
        raise AcceptanceError("UI snapshot omitted semantic nodes")
    return snapshot["nodes"]


def _first_clickable_target(snapshot: Any) -> dict[str, str]:
    for node in _snapshot_nodes(snapshot):
        if not isinstance(node, dict) or node.get("clickable") is not True or node.get("enabled") is False:
            continue
        test_id = node.get("testId")
        if test_id == "dev.ashnazg.fixture:id/increment":
            return {"testId": test_id}
    raise AcceptanceError("fixture UI snapshot has no enabled clickable semantic target")


def _prepare_home(home_arg: Path | None, output: Path) -> tuple[Path, bool]:
    if home_arg is None:
        home = output / "home"
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        return home, True
    home = home_arg.expanduser().absolute()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise AcceptanceError("--home must be an owned directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise AcceptanceError("--home must already be private mode 0700")
    return home, False


def _verified_candidate(directory: Path, binary: Path, fixture: Path, source_sha: str) -> dict[str, Any]:
    if __package__:
        from scripts.runtime_release import verify_release
    else:
        from runtime_release import verify_release
    verify_release(directory, source_sha=source_sha, version="0.3.1")
    catalog_path = directory / "ashnazg-runtime-v0.3.1.json"
    catalog = json.loads(catalog_path.read_text())
    with zipfile.ZipFile(directory / "ashnazg-v0.3.1-macos-aarch64.zip") as archive:
        metadata = json.loads(archive.read("manifest.json"))["files"]["bin/ashnazg"]
    if binary.stat().st_size != metadata["size"] or sha256_file(binary) != metadata["sha256"]:
        raise AcceptanceError("tested binary differs from source-bound candidate package")
    expected_apk = catalog["artifacts"]["fixture"]
    if fixture.stat().st_size != expected_apk["size"] or sha256_file(fixture) != expected_apk["sha256"]:
        raise AcceptanceError("tested fixture differs from source-bound candidate artifact")
    return {"source_sha": source_sha, "candidate_sha256": sha256_file(directory / "candidate.json"),
            "catalog_sha256": sha256_file(catalog_path),
            "provenance_scope": "verified prepared candidate metadata; not intrinsic binary-to-source attestation"}


def run_acceptance(binary: Path, fixture_apk: Path, home_arg: Path | None, output: Path,
                   mode: str, source_sha: str, candidate_dir: Path) -> dict[str, Any]:
    if mode not in ("eof", "sigterm", "sigterm-inflight"):
        raise AcceptanceError("--mode must be eof, sigterm, or sigterm-inflight")
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise AcceptanceError("--source-sha must be a 40-character lowercase hexadecimal SHA")
    binary = binary.resolve(strict=True)
    fixture_apk = fixture_apk.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise AcceptanceError("--binary must be an executable file")
    if not fixture_apk.is_file():
        raise AcceptanceError("--fixture-apk must be a regular file")
    candidate = _verified_candidate(candidate_dir, binary, fixture_apk, source_sha)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_info = output.lstat()
    if not stat.S_ISDIR(output_info.st_mode) or output_info.st_uid != os.getuid() or stat.S_IMODE(output_info.st_mode) != 0o700:
        raise AcceptanceError("--output-dir must be an owned private directory")
    if any(output.iterdir()):
        raise AcceptanceError("--output-dir must be empty to preserve existing evidence")
    home, default_home = _prepare_home(home_arg, output)
    path_dir = output / "private-path"
    path_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path_dir, 0o700)
    tmp_dir = output / "tmp"
    tmp_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(tmp_dir, 0o700)
    cache_dir = home / "Library/Caches/ashnazg/runtime"
    state_dir = home / f"Library/Application Support/ashnazg/native-mcp/{source_sha}"
    cache_before = _tree_identity(cache_dir)
    state_before = _tree_identity(state_dir)
    cache_marker = _identity(cache_dir / "cache.json")
    listeners_before = _ports_closed()
    environment = {
        "PATH": str(path_dir), "HOME": str(home), "TMPDIR": str(tmp_dir),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
    }
    process: subprocess.Popen[bytes] | None = None
    client: StdioClient | None = None
    records: list[dict[str, Any]] = []
    started_at = time.monotonic()
    receipt: dict[str, Any] = {
        "schema_version": 1, "status": "running", "mode": mode, "source_sha": source_sha,
        "driver_sha256": sha256_file(Path(__file__)),
        "candidate": candidate,
        "inputs": {"binary": {"path": str(binary), "size": binary.stat().st_size, "sha256": sha256_file(binary)},
                   "fixture_apk": {"path": str(fixture_apk), "size": fixture_apk.stat().st_size,
                                   "sha256": sha256_file(fixture_apk)}},
        "environment": {"path_is_private_empty_dir": not any(path_dir.iterdir()),
                        "home": str(home), "home_mode_700": stat.S_IMODE(home.stat().st_mode) == 0o700,
                        "default_home_created": default_home,
                        "excluded_variables": ["ASHNAZG_TOKEN", "ANDROID_SERIAL", "ADB", "ASHNAZG_ADB",
                                               "ASHNAZG_AAPT", "AAPT", "ANDROID_HOME", "ANDROID_SDK_ROOT",
                                               "ANDROID_SDK_HOME", "ANDROID_SDK_PATH", "SDKPATH"],
                        "passed_variables": sorted(environment)},
        "cache": {"path": str(cache_dir), "before": cache_before, "marker_before": cache_marker,
                  "claim": "not_observable"},
        "state": {"path": str(state_dir), "before": state_before},
        "listeners_before": listeners_before,
        "protocol": {"requests": [], "responses": [], "payloads": []},
        "tools_called": [],
    }
    failure: str | None = None
    ready_at: float | None = None
    readiness = {"credential_at_ready": {"exists": False, "mode": None}}
    shutdown = {"mode": mode, "stdin_open_at_signal": False, "joined": False,
                "exit_code": None, "direct_child_pid": None}
    try:
        if not all(listeners_before.values()):
            raise AcceptanceError("acceptance ports are already occupied")
        process = subprocess.Popen([str(binary), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=environment, cwd=str(home), bufsize=0)
        client = StdioClient(process)
        shutdown["direct_child_pid"] = process.pid
        next_id = 1

        def rpc(request: dict[str, Any], label: str) -> dict[str, Any]:
            response = client.request(request)
            records.extend([
                _write_json(output / f"{len(records)+1:03d}-{label}.request.json", request),
                _write_json(output / f"{len(records)+2:03d}-{label}.response.json", response),
            ])
            receipt["protocol"]["requests"].append(sanitize(request))
            receipt["protocol"]["responses"].append(sanitize(response))
            return response

        init = rpc({"jsonrpc": "2.0", "id": next_id, "method": "initialize",
                    "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                               "clientInfo": {"name": "native-cli-acceptance", "version": "1"}}}, "initialize")
        init_result = _response_result(init, next_id)
        if not isinstance(init_result, dict) or init_result.get("protocolVersion") != PROTOCOL_VERSION:
            raise AcceptanceError("MCP initialize did not negotiate protocol version 2024-11-05")
        next_id += 1
        client.notification({"jsonrpc": "2.0", "method": "notifications/initialized"})
        receipt["protocol"]["requests"].append({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = rpc({"jsonrpc": "2.0", "id": next_id, "method": "tools/list", "params": {}}, "tools-list")
        listed_result = _response_result(listed, next_id)
        tool_names = [item.get("name") for item in listed_result.get("tools", []) if isinstance(item, dict)]
        if set(tool_names) != set(TOOLS) or len(tool_names) != len(TOOLS):
            raise AcceptanceError("tools/list did not expose exactly the five production tools")
        receipt["tools_list"] = tool_names
        schemas = {item["name"]: item.get("inputSchema", {}) for item in listed_result["tools"]}
        expected_schemas = json.loads(SCHEMA_PATH.read_text())
        if schemas != expected_schemas:
            raise AcceptanceError("tools/list input schemas differ from the frozen production contracts")
        receipt["tool_schema_sha256"] = sha256_file(SCHEMA_PATH)
        receipt["handshake"] = {"elapsed_seconds": round(time.monotonic() - started_at, 3),
                                "credential_at_tools_list": _credential_metadata(state_dir)}
        next_id += 1

        # Only a harmless snapshot is retried while the production runtime finishes startup.
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            request = {"jsonrpc": "2.0", "id": next_id, "method": "tools/call",
                       "params": {"name": "ui_snapshot", "arguments": {"includeInvisible": False}}}
            try:
                response = client.request(request, timeout=min(REQUEST_TIMEOUT, max(0.01, deadline-time.monotonic())))
                evidence_index = len(receipt["protocol"]["requests"])
                records.extend([_write_json(output / f"{evidence_index:03d}-readiness.request.json", request),
                                _write_json(output / f"{evidence_index:03d}-readiness.response.json", response)])
                receipt["protocol"]["requests"].append(sanitize(request))
                receipt["protocol"]["responses"].append(sanitize(response))
                _response_result(response, next_id)
                _tool_payload(response.get("result"))
                ready_at = time.monotonic()
                readiness["credential_at_ready"] = _credential_metadata(state_dir)
                readiness["descendants_at_ready"] = _process_descendants(process.pid)
                readiness["listeners_at_ready"] = _ports_closed()
                if readiness["credential_at_ready"] != {"exists": True, "mode": 0o600}:
                    raise AcceptanceError("native session credential missing or unsafe at readiness")
                if any(value is not False for value in readiness["listeners_at_ready"].values()):
                    raise AcceptanceError("native session relays missing at readiness")
                descendants = readiness["descendants_at_ready"]
                receipt["process_inventory"] = descendants
                if descendants["status"] != "available" or not any(
                        item["command"].startswith("ashnazg-vm") for item in descendants["processes"]) or not any(
                        item["command"] == "ashnazg" for item in descendants["processes"]):
                    raise AcceptanceError("owned helper descendant unavailable at readiness")
                break
            except AcceptanceError as exc:
                retryable_startup = str(exc).lower() in {
                    "startup unavailable",
                    "native runtime is still starting",
                    "native runtime is still starting; retry the request",
                }
                if not retryable_startup or time.monotonic() >= deadline:
                    raise
                receipt.setdefault("startup_retries", 0)
                receipt["startup_retries"] += 1
                time.sleep(min(STARTUP_RETRY_INTERVAL, max(0, deadline-time.monotonic())))
            finally:
                next_id += 1
        receipt["ready"] = {"elapsed_seconds": round(ready_at - started_at, 3),
                            **readiness}
        install, _ = _tool_call(client, records, output, receipt["protocol"], "app_install",
                                {"path": str(fixture_apk), "launch": True}, next_id)
        receipt["tools_called"].append("app_install")
        if not isinstance(install, dict) or install.get("installed") is not True or install.get("package") != "dev.ashnazg.fixture":
            raise AcceptanceError("app_install did not infer and launch the APK package")
        package = install["package"]
        receipt["installation"] = {"installed": True, "inferred_package": package}
        next_id += 1
        snapshot, _ = _tool_call(client, records, output, receipt["protocol"], "ui_snapshot", {"includeInvisible": False}, next_id)
        receipt["tools_called"].append("ui_snapshot")
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("snapshotId"), int) or
                isinstance(snapshot.get("snapshotId"), bool) or snapshot["snapshotId"] <= 0):
            raise AcceptanceError("ui_snapshot omitted a snapshot ID")
        initial_nodes = _snapshot_nodes(snapshot)
        def count_value(value):
            values = [node.get("text") for node in _snapshot_nodes(value)
                      if isinstance(node, dict) and node.get("testId") == "dev.ashnazg.fixture:id/increment"]
            if len(values) != 1 or not isinstance(values[0], str) or re.fullmatch(r"Count: [0-9]+", values[0]) is None:
                raise AcceptanceError("fixture counter semantic value is missing or ambiguous")
            return int(values[0].split(": ")[1])
        initial_count = count_value(snapshot)
        next_id += 1
        before_image, before_response = _tool_call(client, records, output, receipt["protocol"], "ui_screenshot",
                                                   {"format": "png", "somOverlay": False}, next_id)
        receipt["screenshot_before"] = _screenshot(output, before_response, before_image, records, "screenshot-before")
        next_id += 1
        act_args = {"action": "tap", "target": _first_clickable_target(snapshot)}
        receipt["interaction"] = {"action": "tap", "target": act_args["target"]}
        acted, _ = _tool_call(client, records, output, receipt["protocol"], "ui_act", act_args, next_id)
        receipt["tools_called"].append("ui_act")
        if not isinstance(acted, dict) or acted.get("settled") is not True or not isinstance(acted.get("snapshot"), dict):
            raise AcceptanceError("ui_act did not return a settled semantic snapshot")
        action_snapshot = acted["snapshot"]
        if (not isinstance(action_snapshot.get("snapshotId"), int) or
                isinstance(action_snapshot.get("snapshotId"), bool) or
                action_snapshot["snapshotId"] <= snapshot["snapshotId"]):
            raise AcceptanceError("ui_act did not return a changed fresh snapshot")
        if count_value(action_snapshot) != initial_count + 1:
            raise AcceptanceError("fixture counter did not increment exactly once")
        receipt["interaction"].update(count_before=initial_count, count_after=initial_count + 1)
        if _snapshot_nodes(action_snapshot) == initial_nodes:
            raise AcceptanceError("ui_act did not change the semantic UI snapshot")
        next_id += 1
        screenshot, screenshot_response = _tool_call(client, records, output, receipt["protocol"], "ui_screenshot",
                                                     {"format": "png", "somOverlay": False}, next_id)
        receipt["tools_called"].append("ui_screenshot")
        receipt["screenshot"] = _screenshot(output, screenshot_response, screenshot, records)
        receipt["screenshot"]["fresh_after_action"] = receipt["screenshot"]["snapshot_id"] >= action_snapshot["snapshotId"]
        if not receipt["screenshot"]["fresh_after_action"]:
            raise AcceptanceError("screenshot snapshot is not fresh after ui_act")
        if any(receipt["screenshot"][key] != receipt["screenshot_before"][key] for key in ("width", "height")):
            raise AcceptanceError("screenshot dimensions changed across the fixture action")
        if receipt["screenshot"]["pixel_sha256"] == receipt["screenshot_before"]["pixel_sha256"]:
            raise AcceptanceError("screenshot pixels did not change after the fixture action")
        receipt["screenshot"]["pixels_changed_after_action"] = True
        next_id += 1
        logs, _ = _tool_call(client, records, output, receipt["protocol"], "app_logs", {"sinceMs": None, "tagFilter": None}, next_id)
        receipt["tools_called"].append("app_logs")
        if not isinstance(logs, (dict, list, str)):
            raise AcceptanceError("app_logs returned an unsupported result")
        next_id += 1
        if mode == "sigterm-inflight":
            pending = {"jsonrpc": "2.0", "id": next_id, "method": "tools/call",
                       "params": {"name": "ui_act", "arguments": {
                           "action": "long_press", "target": {"text": "Long press target"}}}}
            # The pinned agent holds a long press for 800 ms. Require the
            # submitted request still to be unanswered over a measured 200 ms
            # observation interval before interrupting the real CLI.
            client.notification(pending)
            records.append(_write_json(output / "inflight.request.json", pending))
            observation_start = time.monotonic()
            observation_end = observation_start + 0.2
            while time.monotonic() < observation_end:
                try:
                    frame = client.frames.get(timeout=max(0.001, observation_end - time.monotonic()))
                except queue.Empty:
                    break
                if frame is None or isinstance(frame, BaseException):
                    raise AcceptanceError("CLI ended during in-flight observation")
                observed = json.loads(frame)
                if observed.get("id") == next_id:
                    raise AcceptanceError("in-flight request replied before interruption")
            receipt["inflight"] = {"request_id": next_id, "request_written": True,
                                   "unanswered_at_signal": True,
                                   "observed_pending_seconds": round(time.monotonic() - observation_start, 3),
                                   "claim": "client-observed submitted request without a response; no server-entry timestamp"}
        shutdown["stdin_open_at_signal"] = mode != "eof"
        if mode == "eof":
            assert process.stdin is not None
            process.stdin.close()
        else:
            process.send_signal(signal.SIGTERM)
        try:
            shutdown["exit_code"] = process.wait(timeout=SHUTDOWN_GRACE)
            shutdown["joined"] = True
        except subprocess.TimeoutExpired:
            process.terminate()  # This is the owned direct CLI child created above.
            try:
                shutdown["exit_code"] = process.wait(timeout=KILL_GRACE)
                shutdown["joined"] = True
            except subprocess.TimeoutExpired:
                process.kill()
                shutdown["exit_code"] = process.wait(timeout=KILL_GRACE)
                shutdown["joined"] = True
            raise AcceptanceError("CLI did not exit within the bounded owned-child shutdown grace")
        if shutdown["exit_code"] != 0:
            raise AcceptanceError(f"CLI exited with status {shutdown['exit_code']}")
        if mode == "sigterm-inflight":
            client._stdout_thread.join(timeout=1)
            replies = []
            while not client.frames.empty():
                frame = client.frames.get_nowait()
                if isinstance(frame, bytes):
                    response = json.loads(frame)
                    if response.get("id") == next_id:
                        replies.append(response)
                        if "error" not in response and response.get("result", {}).get("isError") is not True:
                            raise AcceptanceError("in-flight request returned success after interruption")
            receipt["inflight"]["replies_after_signal"] = sanitize(replies)

    except BaseException as exc:
        failure = bounded_text(str(exc)) if isinstance(exc, Exception) else "acceptance interrupted"
    finally:
        if process is not None and process.poll() is None:
            if not failure and process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            elif failure:
                try:
                    process.send_signal(signal.SIGTERM)
                except OSError:
                    pass
            try:
                process.wait(timeout=SHUTDOWN_GRACE)
                shutdown["joined"] = True
                shutdown["exit_code"] = process.returncode
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=KILL_GRACE)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=KILL_GRACE)
                shutdown["joined"] = True
                shutdown["exit_code"] = process.returncode
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            if client is not None:
                client._stderr_thread.join(timeout=1)
            stderr = bytes(client.stderr) if client is not None else b""
            receipt["process"] = {"pid": process.pid, "exit_code": process.returncode,
                                  "stdout_bytes": client.stdout_bytes if client else 0,
                                  "stderr_bytes": client.stderr_bytes if client else 0,
                                  "stderr_truncated": client.stderr_truncated if client else False,
                                  "stderr_sha256": sha256_bytes(stderr)}
        receipt["shutdown"] = shutdown
        listeners_after = _ports_closed()
        receipt["cleanup"] = {
            "credential_absent": not _credential_metadata(state_dir)["exists"],
            "credential_at_ready": readiness["credential_at_ready"],
            "listeners": {"before": listeners_before,
                          "at_ready": readiness.get("listeners_at_ready"),
                          "after": listeners_after,
                          "closed_after": listeners_after},
            "state_identity_preserved": _preserved_state_identities(state_before, _tree_identity(state_dir)),
            "mutable_state_diagnostics": ["runtime.json", "serial.log"],
            "state_after": _tree_identity(state_dir),
            "cache_identity_preserved": all(_tree_identity(cache_dir).get(name) == identity
                                             for name, identity in cache_before.items()),
            "owned_descendants_after_shutdown": _pids_alive(readiness.get("descendants_at_ready", {}).get("processes", [])),
        }
        cleanup = receipt["cleanup"]
        if not failure:
            if not cleanup["credential_absent"] or not all(listeners_after.values()):
                failure = "native cleanup left credentials or listeners"
            elif not cleanup["state_identity_preserved"] or not cleanup["cache_identity_preserved"]:
                failure = "native cleanup changed a retained state or cache identity"
            elif any(item["alive"] for item in cleanup["owned_descendants_after_shutdown"]):
                failure = "owned helper or bridge descendant remained alive"
        receipt["cache"]["after"] = _tree_identity(cache_dir)
        receipt["cache"]["marker_after"] = _identity(cache_dir / "cache.json")
        receipt["state"]["after"] = _tree_identity(state_dir)
        receipt["protocol"]["payloads"] = records
        receipt["status"] = "failed" if failure else "passed"
        if failure:
            receipt["error"] = sanitize(failure)
        receipt_path = output / "receipt.json"
        receipt_path.write_text(json.dumps(sanitize(receipt), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if failure:
        raise AcceptanceError(failure)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--candidate-dir", required=True, type=Path)
    parser.add_argument("--fixture-apk", required=True, type=Path)
    parser.add_argument("--home", type=Path, help="owned private HOME (defaults to output-dir/home)")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=("eof", "sigterm", "sigterm-inflight"), required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args(argv)
    try:
        run_acceptance(args.binary, args.fixture_apk, args.home, args.output_dir, args.mode, args.source_sha, args.candidate_dir)
        print(json.dumps({"status": "passed", "receipt": str(args.output_dir / "receipt.json")}))
        return 0
    except Exception as exc:
        print(sanitize(bounded_text(str(exc))), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
