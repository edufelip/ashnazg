"""Authenticated, single-session owner for guest APK upload operations."""

from __future__ import annotations

import hmac
import json
import math
import socket
import threading
import time

from .apk_stream import UploadError, read_upload_header, receive_apk


HOST_CID = 2
MAX_OPERATION_SECONDS = 180.0
MAX_REPLY_SIZE = 4096


class SessionError(RuntimeError):
    """A bounded shutdown or session lifecycle failure."""


class _RequestFailure(Exception):
    def __init__(self, code):
        self.code = code


class _InterruptibleConnection:
    """Make blocking reads promptly observe shutdown on platforms where close alone does not."""

    def __init__(self, connection):
        self._connection = connection
        self.cancelled = threading.Event()
        self._timeout_at = None

    def settimeout(self, timeout):
        self._timeout_at = time.monotonic() + timeout
        self._connection.settimeout(min(timeout, 0.1))

    def recv(self, size):
        while True:
            if self.cancelled.is_set():
                raise OSError("connection cancelled")
            try:
                return self._connection.recv(size)
            except (TimeoutError, socket.timeout):
                if self.cancelled.is_set():
                    raise OSError("connection cancelled") from None
                if self._timeout_at is None or time.monotonic() >= self._timeout_at:
                    raise
                self._connection.settimeout(min(self._timeout_at - time.monotonic(), 0.1))

    def sendall(self, data):
        return self._connection.sendall(data)

    def shutdown(self, how):
        return self._connection.shutdown(how)

    def close(self):
        return self._connection.close()


class GuestSession:
    """Own one prepared agent and serialize all incoming APK uploads."""

    def __init__(self, backend, *, clock=time.monotonic):
        self._backend = backend
        self._clock = clock
        self._condition = threading.Condition()
        self._state = "new"
        self._session_id = None
        self._install_token = None
        self._agent_port = None
        self._upload_active = False
        self._inflight = 0
        self._connections = set()
        self._interruptible = {}
        self._closed = False
        self._backend_close_started = False
        self._backend_close_done = False
        self._backend_close_failed = False

    def __repr__(self):
        with self._condition:
            return f"GuestSession(state={self._state!r}, inflight={self._inflight})"

    def _deadline(self, deadline):
        try:
            now = float(self._clock())
            requested = float(deadline)
        except (TypeError, ValueError, OverflowError, OSError):
            raise _RequestFailure("timeout") from None
        if not math.isfinite(now) or not math.isfinite(requested):
            raise _RequestFailure("timeout")
        effective = min(requested, now + MAX_OPERATION_SECONDS)
        if effective <= now:
            raise _RequestFailure("timeout")
        return effective

    def _remaining(self, deadline):
        try:
            remaining = deadline - float(self._clock())
        except (TypeError, ValueError, OverflowError, OSError):
            raise _RequestFailure("timeout") from None
        if not math.isfinite(remaining) or remaining <= 0:
            raise _RequestFailure("timeout")
        return remaining

    def _send(self, connection, value, deadline):
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii") + b"\n"
        if len(raw) >= MAX_REPLY_SIZE:
            raise _RequestFailure("failed")
        try:
            connection.settimeout(self._remaining(deadline))
            connection.sendall(raw)
        except (OSError, TimeoutError):
            raise _RequestFailure("failed") from None

    def _reply_error(self, connection, code, deadline):
        try:
            self._send(connection, {"schema_version": 1, "status": "error", "error": code}, deadline)
        except _RequestFailure:
            pass

    def _register(self, connection, operation):
        with self._condition:
            if self._closed:
                return "closed"
            if self._upload_active:
                return "busy"
            if operation == "prepare_agent" and self._state != "new":
                return "busy"
            if operation == "install" and self._state != "ready":
                return "unauthorized"
            self._upload_active = True
            self._inflight += 1
            self._connections.add(connection)
            self._interruptible[connection] = _InterruptibleConnection(connection)
            if operation == "prepare_agent":
                self._state = "preparing"
            return None

    def _alive(self):
        try:
            return self._backend.is_alive() is True
        except BaseException:
            return False

    def _close_backend_once(self):
        with self._condition:
            if self._backend_close_started:
                return
            self._backend_close_started = True
        failed = False
        try:
            self._backend.close()
        except BaseException as error:
            failed = True
            if not isinstance(error, Exception):
                raise
        finally:
            with self._condition:
                self._backend_close_failed = failed
                self._backend_close_done = True
                self._condition.notify_all()

    def _invalidate_dead_backend(self):
        with self._condition:
            if self._state == "ready":
                self._state = "terminal"
                self._session_id = None
                self._install_token = None
                self._agent_port = None

    def _terminate_prepare(self):
        with self._condition:
            if not self._closed:
                self._state = "terminal"
            self._session_id = None
            self._install_token = None
            self._agent_port = None
        self._close_backend_once()

    def _finish(self, connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            connection.close()
        except OSError:
            pass
        with self._condition:
            self._connections.discard(connection)
            self._interruptible.pop(connection, None)
            self._upload_active = False
            self._inflight -= 1
            self._condition.notify_all()

    def _serve(self, connection, *, peer_cid, deadline, operation):
        if peer_cid != HOST_CID:
            try:
                effective = self._deadline(deadline)
                self._reply_error(connection, "unauthorized", effective)
            except _RequestFailure:
                pass
            try:
                connection.close()
            except OSError:
                pass
            return

        try:
            effective = self._deadline(deadline)
        except _RequestFailure as failure:
            try:
                self._reply_error(connection, failure.code, float(self._clock()) + 0.001)
            finally:
                try:
                    connection.close()
                except OSError:
                    pass
            return

        rejection = self._register(connection, operation)
        if rejection:
            self._reply_error(connection, rejection, effective)
            try:
                connection.close()
            except OSError:
                pass
            return

        with self._condition:
            protocol_connection = self._interruptible[connection]

        staged = None
        prepare_attempt = operation == "prepare_agent"
        try:
            header = read_upload_header(protocol_connection, deadline=effective, clock=self._clock)
            if header.op != operation:
                raise _RequestFailure("invalid_request")
            if operation == "prepare_agent":
                if header.rpc_token == header.install_token:
                    raise _RequestFailure("invalid_request")
            else:
                with self._condition:
                    session_id = self._session_id
                    install_token = self._install_token
                    ready = self._state == "ready" and not self._closed
                if not ready:
                    raise _RequestFailure("unauthorized")
                if not self._alive():
                    self._invalidate_dead_backend()
                    raise _RequestFailure("backend_unavailable")
                if header.session_id != session_id or not hmac.compare_digest(
                        header.install_token, install_token or ""):
                    raise _RequestFailure("unauthorized")

            self._send(protocol_connection, {"schema_version": 1, "status": "ready_for_payload"}, effective)
            staged = receive_apk(protocol_connection, header, deadline=effective, clock=self._clock)
            self._remaining(effective)

            if operation == "prepare_agent":
                port = self._backend.prepare_agent(staged, header.rpc_token, deadline=effective)
                self._remaining(effective)
                if type(port) is not int or not 1 <= port <= 65535:
                    raise _RequestFailure("backend_failed")
                with self._condition:
                    if self._closed:
                        raise _RequestFailure("closed")
                if not self._alive():
                    raise _RequestFailure("backend_unavailable")
                with self._condition:
                    if self._closed:
                        raise _RequestFailure("closed")
                    self._session_id = header.session_id
                    self._install_token = header.install_token
                    self._agent_port = port
                    self._state = "ready"
                    self._send(protocol_connection, {"schema_version": 1, "status": "agent_ready",
                                                    "session_id": header.session_id}, effective)
            else:
                self._backend.install(staged, package=header.package if header.launch else None,
                                      deadline=effective)
                self._remaining(effective)
                with self._condition:
                    if self._closed or self._state != "ready":
                        raise _RequestFailure("closed")
                if not self._alive():
                    self._invalidate_dead_backend()
                    raise _RequestFailure("backend_unavailable")
                with self._condition:
                    if self._closed or self._state != "ready":
                        raise _RequestFailure("closed")
                    self._send(protocol_connection, {"schema_version": 1, "status": "installed",
                                                    "session_id": header.session_id,
                                                    "launched": header.launch}, effective)
        except UploadError:
            self._reply_error(protocol_connection, "invalid_upload", effective)
            if prepare_attempt:
                self._terminate_prepare()
        except _RequestFailure as failure:
            self._reply_error(protocol_connection, failure.code, effective)
            if prepare_attempt:
                self._terminate_prepare()
        except BaseException:
            self._reply_error(protocol_connection, "backend_failed", effective)
            if prepare_attempt:
                self._terminate_prepare()
            elif not self._alive():
                self._invalidate_dead_backend()
        finally:
            if staged is not None:
                staged.close()
            self._finish(connection)

    def serve_prepare(self, connection, *, peer_cid, deadline):
        """Serve one host-CID prepare request and close the borrowed connection."""
        self._serve(connection, peer_cid=peer_cid, deadline=deadline, operation="prepare_agent")

    def serve_install(self, connection, *, peer_cid, deadline):
        """Serve one authenticated APK installation and close the borrowed connection."""
        self._serve(connection, peer_cid=peer_cid, deadline=deadline, operation="install")

    def agent_endpoint(self):
        with self._condition:
            if self._closed or self._state != "ready":
                return None
            port = self._agent_port
        if not self._alive():
            self._invalidate_dead_backend()
            return None
        with self._condition:
            if self._closed or self._state != "ready" or self._agent_port != port:
                return None
        return ("127.0.0.1", port)

    def close(self):
        """Reject new work, interrupt active sockets and concurrently cancel backend work."""
        with self._condition:
            if self._closed:
                backend_close = False
                connections = ()
                protocols = ()
            else:
                self._closed = True
                self._state = "closed"
                self._session_id = None
                self._install_token = None
                self._agent_port = None
                connections = tuple(self._connections)
                protocols = tuple(self._interruptible.values())
                backend_close = True
                self._condition.notify_all()
        for protocol in protocols:
            protocol.cancelled.set()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass
            try:
                connection.close()
            except OSError:
                pass
        if backend_close:
            self._close_backend_once()

    def drain(self, *, timeout):
        """Wait up to timeout seconds for admitted calls to finish; timeout is an error."""
        try:
            duration = float(timeout)
        except (TypeError, ValueError, OverflowError):
            raise SessionError("invalid drain timeout") from None
        if not math.isfinite(duration) or duration < 0:
            raise SessionError("invalid drain timeout")
        end = time.monotonic() + duration
        with self._condition:
            while (self._inflight or
                   ((self._closed or self._backend_close_started) and not self._backend_close_done)):
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise SessionError("session drain timed out") from None
                self._condition.wait(remaining)
            if self._backend_close_failed:
                raise SessionError("backend cleanup failed") from None
        return True
