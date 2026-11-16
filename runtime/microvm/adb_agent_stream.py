"""Owned raw agent streams over the guest's existing ADB Smart Socket."""

from __future__ import annotations

import errno
import math
import select
import socket
import threading
import time


ADB_SERVER_HOST = "127.0.0.1"
ADB_SERVER_PORT = 5037
ANDROID_SERIAL = "127.0.0.1:5555"
AGENT_SERVICE = "tcp:7777"
MAX_CONNECTIONS = 8
MAX_HANDSHAKE_SECONDS = 5.0
MAX_FAILURE_MESSAGE = 4 * 1024
_WAIT_SLICE = 0.05


class AgentStreamError(RuntimeError):
    """A generic Smart Socket connection or handshake failure."""

    def __init__(self):
        super().__init__("agent stream unavailable")


class AgentStreamPool:
    """Admit and own at most eight fixed ADB-to-agent raw streams."""

    def __init__(self, *, clock=time.monotonic, cancel=None):
        self._clock = clock
        self._cancel = cancel
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()
        self._slots = 0
        self._closed = False

    def __repr__(self):
        with self._lock:
            return "AgentStreamPool(active=%d, closed=%s)" % (self._slots, self._closed)

    def open(self, *, deadline):
        """Open one stream, retaining the same socket after both OKAY replies."""
        end = self._deadline(deadline)
        with self._lock:
            if self._closed or self._cancelled() or self._slots >= MAX_CONNECTIONS:
                raise AgentStreamError()
            # Reserve before socket creation/connect so concurrent opens cannot
            # exceed the cap, including sockets in their handshake.
            self._slots += 1

        connection = None
        tracked = False
        handed_off = False
        try:
            connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.setblocking(False)
            with self._lock:
                if self._closed or self._cancelled():
                    raise AgentStreamError()
                self._sockets.add(connection)
                tracked = True

            self._connect(connection, end)
            self._send_request(connection, "host:transport:" + ANDROID_SERIAL, end)
            self._read_status(connection, end)
            self._send_request(connection, AGENT_SERVICE, end)
            self._read_status(connection, end)
            self._check(end)
            connection.setblocking(True)
            handed_off = True
            return _OwnedAgentStream(self, connection)
        except AgentStreamError:
            raise
        except (OSError, ValueError, OverflowError, TypeError):
            raise AgentStreamError() from None
        finally:
            if connection is not None and not handed_off:
                if tracked:
                    self._forget_and_close(connection)
                else:
                    self._shutdown_socket(connection)
            if not tracked:
                self._release_reservation()

    def close(self):
        """Close only this pool's admitted sockets; safe to call repeatedly."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sockets = tuple(self._sockets)
            self._sockets.clear()
            self._slots -= len(sockets)
        for connection in sockets:
            self._shutdown_socket(connection)

    def _deadline(self, deadline):
        try:
            now = float(self._clock())
            requested = float(deadline)
        except (TypeError, ValueError, OverflowError, OSError):
            raise AgentStreamError() from None
        if not math.isfinite(now) or not math.isfinite(requested) or requested <= now:
            raise AgentStreamError()
        return min(requested, now + MAX_HANDSHAKE_SECONDS)

    def _cancelled(self):
        try:
            cancel = self._cancel
            if cancel is None:
                return False
            if callable(cancel):
                return bool(cancel())
            is_set = getattr(cancel, "is_set", None)
            return bool(is_set()) if callable(is_set) else bool(cancel)
        except Exception:
            return True

    def _check(self, deadline):
        try:
            current = float(self._clock())
        except (TypeError, ValueError, OverflowError, OSError):
            raise AgentStreamError() from None
        with self._lock:
            closed = self._closed
        if closed or self._cancelled() or not math.isfinite(current) or current >= deadline:
            raise AgentStreamError()

    def _wait(self, connection, *, reading, deadline):
        while True:
            self._check(deadline)
            remaining = deadline - float(self._clock())
            try:
                readable, writable, exceptional = select.select(
                    [connection] if reading else [], [] if reading else [connection],
                    [connection], min(_WAIT_SLICE, remaining))
            except (OSError, ValueError):
                raise AgentStreamError() from None
            if exceptional:
                raise AgentStreamError()
            if (readable if reading else writable):
                self._check(deadline)
                return

    def _connect(self, connection, deadline):
        self._check(deadline)
        try:
            result = connection.connect_ex((ADB_SERVER_HOST, ADB_SERVER_PORT))
        except OSError:
            raise AgentStreamError() from None
        if result in (0, errno.EISCONN):
            self._check(deadline)
            return
        pending = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR}
        if result not in pending:
            raise AgentStreamError()
        while True:
            self._wait(connection, reading=False, deadline=deadline)
            try:
                error = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            except OSError:
                raise AgentStreamError() from None
            if error == 0:
                self._check(deadline)
                return
            if error not in pending:
                raise AgentStreamError()

    def _send_bytes(self, connection, data, deadline):
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            self._check(deadline)
            try:
                count = connection.send(view[sent:])
            except (BlockingIOError, InterruptedError):
                self._wait(connection, reading=False, deadline=deadline)
                continue
            except OSError:
                raise AgentStreamError() from None
            if count <= 0:
                raise AgentStreamError()
            sent += count

    def _recv_exact(self, connection, size, deadline):
        output = bytearray()
        while len(output) < size:
            self._check(deadline)
            try:
                chunk = connection.recv(size - len(output))
            except (BlockingIOError, InterruptedError):
                self._wait(connection, reading=True, deadline=deadline)
                continue
            except OSError:
                raise AgentStreamError() from None
            if not chunk:
                raise AgentStreamError()
            output.extend(chunk)
        return bytes(output)

    def _send_request(self, connection, service, deadline):
        try:
            encoded = service.encode("ascii")
        except UnicodeError:
            raise AgentStreamError() from None
        if len(encoded) > 0xFFFF:
            raise AgentStreamError()
        self._send_bytes(connection, ("%04x" % len(encoded)).encode("ascii") + encoded, deadline)

    def _read_status(self, connection, deadline):
        status = self._recv_exact(connection, 4, deadline)
        if status == b"OKAY":
            return
        if status != b"FAIL":
            raise AgentStreamError()
        raw_length = self._recv_exact(connection, 4, deadline)
        if any(byte not in b"0123456789abcdefABCDEF" for byte in raw_length):
            raise AgentStreamError()
        length = int(raw_length, 16)
        if length > MAX_FAILURE_MESSAGE:
            raise AgentStreamError()
        # Failure text may contain credentials or server internals. Consume it
        # only to keep framing exact, and never decode, retain, or expose it.
        remaining = length
        while remaining:
            chunk = self._recv_exact(connection, min(1024, remaining), deadline)
            remaining -= len(chunk)
        raise AgentStreamError()

    def _forget_and_close(self, connection):
        with self._lock:
            if connection in self._sockets:
                self._sockets.remove(connection)
                self._shutdown_socket(connection)
                self._slots -= 1
                return
        self._shutdown_socket(connection)

    def _release_reservation(self):
        with self._lock:
            self._slots -= 1

    @staticmethod
    def _shutdown_socket(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass


class _OwnedAgentStream:
    """Small socket facade that deregisters exactly once when closed."""

    __slots__ = ("_pool", "_socket", "_lock", "_closed")

    def __init__(self, pool, connection):
        self._pool = pool
        self._socket = connection
        self._lock = threading.Lock()
        self._closed = False

    def __repr__(self):
        return "OwnedAgentStream(closed=%s)" % self._closed

    def fileno(self):
        with self._lock:
            return -1 if self._closed else self._socket.fileno()

    def _socket_or_error(self):
        with self._lock:
            if self._closed:
                raise OSError(errno.EBADF, "closed stream")
            return self._socket

    def send(self, data, flags=0):
        return self._socket_or_error().send(data, flags)

    def sendall(self, data, flags=0):
        return self._socket_or_error().sendall(data, flags)

    def recv(self, size, flags=0):
        return self._socket_or_error().recv(size, flags)

    def setblocking(self, flag):
        return self._socket_or_error().setblocking(flag)

    def settimeout(self, value):
        return self._socket_or_error().settimeout(value)

    def gettimeout(self):
        return self._socket_or_error().gettimeout()

    def shutdown(self, how):
        return self._socket_or_error().shutdown(how)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pool._forget_and_close(self._socket)
