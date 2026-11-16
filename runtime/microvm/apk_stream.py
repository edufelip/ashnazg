"""Strict framing and sealed anonymous storage for one APK upload per connection."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import fcntl
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Optional


MAX_HEADER_SIZE = 4096
MAX_APK_SIZE = 128 * 1024 * 1024
MAX_OPERATION_SECONDS = 30.0
_TOKEN_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_PACKAGE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\Z", re.ASCII)


class UploadError(ValueError):
    """A safely worded framing or staging failure."""


@dataclass(frozen=True)
class UploadHeader:
    schema_version: int
    op: str
    session_id: str
    apk_size: int
    apk_sha256: str
    rpc_token: Optional[str] = field(default=None, repr=False)
    install_token: Optional[str] = field(default=None, repr=False)
    launch: Optional[bool] = None
    package: Optional[str] = None
    _deadline: Optional[float] = field(default=None, repr=False, compare=False)


def _effective_deadline(deadline, clock):
    try:
        requested = float(deadline)
        now = float(clock())
    except (TypeError, ValueError, OverflowError, OSError):
        raise UploadError("invalid upload deadline") from None
    if not math.isfinite(requested) or not math.isfinite(now):
        raise UploadError("invalid upload deadline")
    effective = min(requested, now + MAX_OPERATION_SECONDS)
    if effective <= now:
        raise UploadError("upload deadline expired")
    return effective


def _set_timeout(sock, deadline, clock):
    try:
        remaining = deadline - float(clock())
    except (TypeError, ValueError, OverflowError, OSError):
        raise UploadError("invalid upload deadline") from None
    if not math.isfinite(remaining) or remaining <= 0:
        raise UploadError("upload deadline expired")
    try:
        sock.settimeout(remaining)
    except (AttributeError, OSError, ValueError):
        raise UploadError("upload connection unavailable") from None


def _recv(sock, count, deadline, clock):
    _set_timeout(sock, deadline, clock)
    try:
        return sock.recv(count)
    except (OSError, TimeoutError):
        raise UploadError("upload read failed or timed out") from None


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_session_id(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _validate_header_object(value):
    if not isinstance(value, dict):
        raise UploadError("invalid upload header")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise UploadError("invalid upload header")
    operation = value.get("op")
    common = {"schema_version", "op", "session_id", "apk_size", "apk_sha256"}
    if operation == "prepare_agent":
        expected = common | {"rpc_token", "install_token"}
    elif operation == "install":
        expected = common | {"install_token", "launch"}
        if value.get("launch") is True:
            expected.add("package")
    else:
        raise UploadError("invalid upload header")
    if set(value) != expected:
        raise UploadError("invalid upload header")
    if not _canonical_session_id(value.get("session_id")):
        raise UploadError("invalid upload header")
    if type(value.get("apk_size")) is not int or not 1 <= value["apk_size"] <= MAX_APK_SIZE:
        raise UploadError("invalid upload header")
    if not isinstance(value.get("apk_sha256"), str) or not _DIGEST_RE.fullmatch(value["apk_sha256"]):
        raise UploadError("invalid upload header")
    if operation == "prepare_agent":
        if any(not isinstance(value.get(key), str) or not _TOKEN_RE.fullmatch(value[key])
               for key in ("rpc_token", "install_token")):
            raise UploadError("invalid upload header")
        return UploadHeader(1, operation, value["session_id"], value["apk_size"],
                            value["apk_sha256"], value["rpc_token"], value["install_token"])
    if not isinstance(value.get("install_token"), str) or not _TOKEN_RE.fullmatch(value["install_token"]):
        raise UploadError("invalid upload header")
    if type(value.get("launch")) is not bool:
        raise UploadError("invalid upload header")
    package = value.get("package")
    if value["launch"]:
        if (not isinstance(package, str) or len(package) > 255 or
                not package.isascii() or not _PACKAGE_RE.fullmatch(package)):
            raise UploadError("invalid upload header")
    return UploadHeader(1, operation, value["session_id"], value["apk_size"],
                        value["apk_sha256"], install_token=value["install_token"],
                        launch=value["launch"], package=package)


def read_upload_header(sock, *, deadline, clock=time.monotonic):
    """Read and validate one newline terminated UTF-8 JSON header byte by byte."""
    effective_deadline = _effective_deadline(deadline, clock)
    raw = bytearray()
    while len(raw) < MAX_HEADER_SIZE:
        byte = _recv(sock, 1, effective_deadline, clock)
        if not byte:
            raise UploadError("incomplete upload header")
        raw.extend(byte)
        if byte == b"\n":
            break
    else:
        raise UploadError("upload header too large")
    try:
        decoded = raw[:-1].decode("utf-8", "strict")
        value = json.loads(decoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
        raise UploadError("invalid upload header") from None
    return replace(_validate_header_object(value), _deadline=effective_deadline)


def _revalidate_header(header):
    if not isinstance(header, UploadHeader):
        raise UploadError("invalid upload header")
    value = {
        "schema_version": header.schema_version,
        "op": header.op,
        "session_id": header.session_id,
        "apk_size": header.apk_size,
        "apk_sha256": header.apk_sha256,
    }
    if header.op == "prepare_agent":
        value["rpc_token"] = header.rpc_token
        value["install_token"] = header.install_token
    elif header.op == "install":
        value["install_token"] = header.install_token
        value["launch"] = header.launch
        if header.package is not None:
            value["package"] = header.package
    else:
        raise UploadError("invalid upload header")
    validated = _validate_header_object(value)
    if validated != header:
        raise UploadError("invalid upload header")
    return header


def _new_memfd():
    descriptor = None
    try:
        descriptor = os.memfd_create("ashnazg-apk", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException as exc:
        if descriptor is not None:
            os.close(descriptor)
        if not isinstance(exc, Exception):
            raise
        raise UploadError("anonymous APK storage requires Linux memfd support") from None


def _seal_memfd(descriptor):
    seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
    fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
    if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & seals != seals:
        raise UploadError("anonymous APK sealing failed")


class StagedApk:
    """Verified immutable upload; callers borrow its descriptor while it is open."""

    def __init__(self, descriptor, size, sha256):
        self.size = size
        self.sha256 = sha256
        self._descriptor = descriptor
        self._lock = threading.Lock()

    def fileno(self):
        with self._lock:
            if self._descriptor is None:
                raise ValueError("staged APK is closed")
            return self._descriptor

    def close(self):
        with self._lock:
            descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            os.close(descriptor)

    def __enter__(self):
        self.fileno()
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()
        return False


def receive_apk(sock, header, *, deadline, clock=time.monotonic):
    """Verify exact APK bytes and half-close, then return sealed anonymous storage."""
    validated = _revalidate_header(header)
    effective_deadline = _effective_deadline(deadline, clock)
    if validated._deadline is not None:
        effective_deadline = min(effective_deadline,
                                 _effective_deadline(validated._deadline, clock))
    descriptor = None
    try:
        descriptor = _new_memfd()
        digest = hashlib.sha256()
        received = 0
        while received < validated.apk_size:
            chunk = _recv(sock, min(64 * 1024, validated.apk_size - received), effective_deadline, clock)
            if not chunk:
                raise UploadError("upload ended before declared size")
            view = memoryview(chunk)
            while view:
                _set_timeout(sock, effective_deadline, clock)
                written = os.write(descriptor, view)
                if written <= 0:
                    raise UploadError("anonymous upload write failed")
                view = view[written:]
            digest.update(chunk)
            received += len(chunk)
        if digest.hexdigest() != validated.apk_sha256:
            raise UploadError("upload digest mismatch")
        if _recv(sock, 1, effective_deadline, clock):
            raise UploadError("upload contains trailing bytes")
        _seal_memfd(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        _set_timeout(sock, effective_deadline, clock)
        result = StagedApk(descriptor, received, digest.hexdigest())
        descriptor = None
        return result
    except BaseException as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                raise UploadError("upload failed and descriptor cleanup failed") from None
        if isinstance(exc, UploadError) or not isinstance(exc, Exception):
            raise
        raise UploadError("upload failed") from None
