"""Route the shared guest control channel without consuming upload bytes."""

from __future__ import annotations

import json
import math
import socket
import time


HOST_CID = 2
MAX_HEADER_SIZE = 4096
MAX_LEGACY_SIZE = 1024
UPLOAD_SECONDS = 30.0
LEGACY_SECONDS = 2.0
SESSION_SECONDS = 180.0


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _decode_frame(raw):
    try:
        value = json.loads(raw[:-1].decode("utf-8", "strict"),
                           object_pairs_hook=_unique_object, parse_constant=_reject_constant,
                           parse_float=_finite_float)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _remaining(deadline, clock):
    try:
        left = deadline - float(clock())
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0
    return left if math.isfinite(left) and left > 0 else 0.0


def _legacy_key_started(raw):
    """Recognize a legacy object as soon as its first member name is complete."""
    candidate = bytes(raw).lstrip(b" \t\r\n")
    if not candidate.startswith(b"{"):
        return False
    candidate = candidate[1:].lstrip(b" \t\r\n")
    if not candidate.startswith(b'"'):
        return False
    end = candidate.find(b'"', 1)
    if end < 0:
        return False
    try:
        return json.loads(candidate[:end + 1].decode("ascii")) == "method"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False


class _ReplayConnection:
    """Replay one already-read header, then transparently use its owned socket."""

    def __init__(self, connection, header):
        self._connection = connection
        self._header = header
        self._offset = 0

    def __repr__(self):
        return "<ReplayConnection>"

    def recv(self, size, *args):
        if self._offset < len(self._header):
            end = min(len(self._header), self._offset + max(0, size))
            chunk = self._header[self._offset:end]
            self._offset = end
            return chunk
        return self._connection.recv(size, *args)

    def settimeout(self, timeout):
        return self._connection.settimeout(timeout)

    def sendall(self, data, *args):
        return self._connection.sendall(data, *args)

    def shutdown(self, how):
        return self._connection.shutdown(how)

    def close(self):
        return self._connection.close()


def serve_session_control(connection, session, *, peer_cid, is_ready,
                          on_shutdown=None, clock=time.monotonic):
    """Serve one control request and own the supplied connection through close."""
    try:
        try:
            entered = float(clock())
        except (TypeError, ValueError, OverflowError, OSError):
            return
        if not math.isfinite(entered):
            return
        if peer_cid != HOST_CID:
            return
        upload_deadline = entered + UPLOAD_SECONDS
        raw = bytearray()
        legacy = False
        while len(raw) < MAX_HEADER_SIZE:
            deadline = entered + LEGACY_SECONDS if legacy else upload_deadline
            remaining = _remaining(deadline, clock)
            if remaining <= 0:
                return
            try:
                connection.settimeout(remaining)
                byte = connection.recv(1)
            except (OSError, TimeoutError, ValueError):
                return
            if not byte:
                return
            raw.extend(byte)
            if not legacy and _legacy_key_started(raw):
                legacy = True
                if _remaining(entered + LEGACY_SECONDS, clock) <= 0:
                    return
            if byte == b"\n":
                break
        else:
            return

        if not raw or raw[-1:] != b"\n":
            return
        value = _decode_frame(raw)
        if value is None:
            return
        if "method" in value:
            if len(raw) > MAX_LEGACY_SIZE or _remaining(entered + LEGACY_SECONDS, clock) <= 0:
                return
            if value == {"method": "shutdown"}:
                if on_shutdown is None:
                    return
                response = b'{"schema_version":1,"status":"stopping"}\n'
                deadline = entered + LEGACY_SECONDS
                connection.settimeout(_remaining(deadline, clock))
                connection.sendall(response)
                on_shutdown()
                return
            if value != {"method": "status"} or not is_ready():
                return
            response = (b'{"schema_version":1,"status":"ready",'
                        b'"android_serial":"127.0.0.1:5555"}\n')
            deadline = entered + LEGACY_SECONDS
            connection.settimeout(_remaining(deadline, clock))
            connection.sendall(response)
            return
        if value.get("op") != "prepare_agent" or session is None:
            return
        # Upload routing gets the original entry-based deadline, even when the
        # shared channel spent time reading the header.
        session.serve_prepare(_ReplayConnection(connection, bytes(raw)), peer_cid=peer_cid,
                              deadline=entered + SESSION_SECONDS,
                              upload_deadline=upload_deadline)
    except (OSError, TimeoutError, ValueError):
        return
    finally:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            connection.close()
        except OSError:
            pass

