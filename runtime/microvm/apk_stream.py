"""Strict framing and private staging for one APK upload per connection."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import socket
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
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
    return _validate_header_object(value)


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
    return validated


def _open_staging_directory(staging_dir):
    path = os.fspath(staging_dir)
    try:
        before = os.lstat(path)
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise UploadError("unsafe staging directory")
        if before.st_uid != os.getuid() or before.st_mode & 0o077:
            raise UploadError("unsafe staging directory")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            after = os.fstat(fd)
            if (not stat.S_ISDIR(after.st_mode) or after.st_uid != os.getuid() or after.st_mode & 0o077 or
                    (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
                raise UploadError("unsafe staging directory")
        except BaseException:
            os.close(fd)
            raise
        return fd, os.path.abspath(path)
    except UploadError:
        raise
    except OSError:
        raise UploadError("staging directory unavailable") from None


def _create_staged_file(dir_fd):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(16):
        name = "upload-" + secrets.token_hex(16) + ".apk"
        try:
            fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            continue
        try:
            st = os.fstat(fd)
            return name, fd, st.st_dev, st.st_ino
        except BaseException:
            try:
                os.close(fd)
            finally:
                # The name was generated here and created exclusively. If
                # its metadata cannot be read, remove this still-private entry.
                try:
                    os.unlink(name, dir_fd=dir_fd)
                except OSError:
                    pass
            raise UploadError("unable to inspect staged upload") from None
    raise UploadError("unable to create staged upload")


def _remove_owned_file(dir_fd, name, device, inode):
    try:
        current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) != (device, inode):
        raise OSError("staged file was replaced before cleanup")
    os.unlink(name, dir_fd=dir_fd)


class StagedApk:
    """An owned staged APK whose exact directory entry is removed on close."""

    def __init__(self, path, size, sha256, dir_fd, name, device, inode):
        self.path = path
        self.size = size
        self.sha256 = sha256
        self._dir_fd = dir_fd
        self._name = name
        self._device = device
        self._inode = inode
        self._closed = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        failure = None
        try:
            _remove_owned_file(self._dir_fd, self._name, self._device, self._inode)
        except OSError as exc:
            failure = exc
        try:
            os.close(self._dir_fd)
        except OSError as exc:
            if failure is None:
                failure = exc
        self._dir_fd = -1
        if failure is not None:
            raise failure

    def __enter__(self):
        if self._closed:
            raise ValueError("staged APK is closed")
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()
        return False


def receive_apk(sock, header, *, staging_dir, deadline, clock=time.monotonic):
    """Receive exact APK bytes, verify digest and sender half-close, then stage."""
    validated = _revalidate_header(header)
    effective_deadline = _effective_deadline(deadline, clock)
    dir_fd, directory_path = _open_staging_directory(staging_dir)
    name = None
    file_fd = None
    device = inode = None
    try:
        name, file_fd, device, inode = _create_staged_file(dir_fd)
        digest = hashlib.sha256()
        received = 0
        while received < validated.apk_size:
            chunk = _recv(sock, min(64 * 1024, validated.apk_size - received), effective_deadline, clock)
            if not chunk:
                raise UploadError("upload ended before declared size")
            view = memoryview(chunk)
            while view:
                _set_timeout(sock, effective_deadline, clock)
                try:
                    written = os.write(file_fd, view)
                except OSError:
                    raise UploadError("staged upload write failed") from None
                if written <= 0:
                    raise UploadError("staged upload write failed")
                view = view[written:]
            digest.update(chunk)
            received += len(chunk)
        if digest.hexdigest() != validated.apk_sha256:
            raise UploadError("upload digest mismatch")
        # This final one-byte read both requires a sender write-half-close and
        # rejects any byte beyond the declared payload under the same deadline.
        extra = _recv(sock, 1, effective_deadline, clock)
        if extra:
            raise UploadError("upload contains trailing bytes")
        try:
            os.fsync(file_fd)
        except OSError:
            raise UploadError("staged upload finalization failed") from None
        os.close(file_fd)
        file_fd = None
        return StagedApk(os.path.join(directory_path, name), received, digest.hexdigest(),
                         dir_fd, name, device, inode)
    except BaseException as exc:
        cleanup_failure = None
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError as close_exc:
                cleanup_failure = close_exc
        if name is not None:
            try:
                _remove_owned_file(dir_fd, name, device, inode)
            except OSError as cleanup_exc:
                cleanup_failure = cleanup_exc
        try:
            os.close(dir_fd)
        except OSError as close_exc:
            cleanup_failure = cleanup_failure or close_exc
        if cleanup_failure is not None:
            raise UploadError("upload failed and staged file cleanup failed") from cleanup_failure
        if isinstance(exc, UploadError):
            raise
        raise UploadError("upload failed") from None
