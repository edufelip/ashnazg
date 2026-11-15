#!/usr/bin/env python3
"""Small, bounded AF_VSOCK control and raw ADB bridge for the Linux guest."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

CONTROL_PORT = 10777
ADB_PORT = 15555
ADB_TARGET = ("127.0.0.1", 5555)
MAX_CONTROL_BYTES = 1024
MAX_CONTROL_CONNECTIONS = 8
MAX_PROXY_CONNECTIONS = 8
MAX_PROXY_BUFFER = 256 * 1024


class ConnectionLimiter:
    """A nonblocking admission gate with checked release semantics."""

    def __init__(self, maximum: int):
        if maximum < 1:
            raise ValueError("maximum must be positive")
        self._semaphore = threading.BoundedSemaphore(maximum)
        self._lock = threading.Lock()
        self._active = 0
        self._maximum = maximum

    def acquire(self) -> bool:
        if not self._semaphore.acquire(blocking=False):
            return False
        with self._lock:
            self._active += 1
            if self._active > self._maximum:
                raise RuntimeError("connection limiter exceeded its bound")
        return True

    def release(self) -> None:
        with self._lock:
            if self._active <= 0:
                raise ValueError("release without a matching acquire")
            self._active -= 1
        self._semaphore.release()


def serve_control_request(
    connection: socket.socket,
    *,
    is_ready: Callable[[], bool],
    on_shutdown: Callable[[], None] | None = None,
    max_request_bytes: int = MAX_CONTROL_BYTES,
) -> None:
    """Answer exactly one bounded newline JSON status request, otherwise close."""
    try:
        deadline = time.monotonic() + 2.0
        frame = bytearray()
        while len(frame) <= max_request_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0: return
            connection.settimeout(remaining)
            chunk = connection.recv(min(256, max_request_bytes + 1 - len(frame)))
            if not chunk:
                return
            frame.extend(chunk)
            newline = frame.find(b"\n")
            if newline >= 0:
                if newline + 1 != len(frame) or newline + 1 > max_request_bytes:
                    return
                try:
                    request = json.loads(frame[:newline].decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return
                if request == {"method": "shutdown"} and on_shutdown is not None:
                    connection.sendall(b'{"schema_version":1,"status":"stopping"}\n')
                    on_shutdown()
                    return
                if not isinstance(request, dict) or request != {"method": "status"}:
                    return
                if not is_ready():
                    return
                response = {
                    "schema_version": 1,
                    "status": "ready",
                    "android_serial": "127.0.0.1:5555",
                }
                connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode())
                return
        return
    except (OSError, TimeoutError):
        return
    finally:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()


def proxy_streams(client: socket.socket, upstream: socket.socket) -> None:
    """Relay both directions with bounded buffering and TCP-style half closes."""
    selector = selectors.DefaultSelector()
    peers = {client: upstream, upstream: client}
    outgoing = {client: bytearray(), upstream: bytearray()}
    read_open = {client: True, upstream: True}
    write_shutdown = {client: False, upstream: False}
    sockets = (client, upstream)
    try:
        for sock in sockets:
            sock.setblocking(False)
            selector.register(sock, selectors.EVENT_READ)
        while any(read_open.values()) or any(outgoing.values()):
            for sock in sockets:
                if (not read_open[sock] and not write_shutdown[sock]
                        and not outgoing[peers[sock]]):
                    try:
                        peers[sock].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    write_shutdown[sock] = True
            for key, events in selector.select(timeout=0.5):
                source = key.fileobj
                destination = peers[source]
                if events & selectors.EVENT_READ and read_open[source]:
                    try:
                        data = source.recv(min(65536, MAX_PROXY_BUFFER - len(outgoing[destination])))
                    except (BlockingIOError, InterruptedError):
                        data = None
                    if data == b"":
                        read_open[source] = False
                    elif data:
                        outgoing[destination].extend(data)
                if events & selectors.EVENT_WRITE and outgoing[source]:
                    try:
                        sent = source.send(outgoing[source])
                    except (BlockingIOError, InterruptedError):
                        sent = 0
                    if sent:
                        del outgoing[source][:sent]
                for candidate in sockets:
                    try:
                        registered = selector.get_key(candidate)
                    except KeyError:
                        registered = None
                    peer_output = outgoing[peers[candidate]]
                    can_read = read_open[candidate] and len(peer_output) < MAX_PROXY_BUFFER
                    can_write = bool(outgoing[candidate])
                    mask = (selectors.EVENT_READ if can_read else 0) | (selectors.EVENT_WRITE if can_write else 0)
                    if mask:
                        if registered is None:
                            selector.register(candidate, mask)
                        else:
                            selector.modify(candidate, mask)
                    elif registered is not None:
                        selector.unregister(candidate)

    except (OSError, ValueError):
        pass
    finally:
        selector.close()
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def proxy_connection(client: socket.socket, connect_upstream: Callable[[], socket.socket]) -> None:
    upstream = None
    try:
        upstream = connect_upstream()
        proxy_streams(client, upstream)
    except OSError:
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.close()
        if upstream is not None:
            upstream.close()


class BridgeServer:
    def __init__(self, control_listener: socket.socket, adb_listener: socket.socket,
                 on_shutdown: Callable[[], None] | None = None, allowed_cid: int | None = None):
        self.listeners = (control_listener, adb_listener)
        self.limiters = (
            ConnectionLimiter(MAX_CONTROL_CONNECTIONS),
            ConnectionLimiter(MAX_PROXY_CONNECTIONS),
        )
        self.executors = (
            ThreadPoolExecutor(max_workers=MAX_CONTROL_CONNECTIONS, thread_name_prefix="vsock-status"),
            ThreadPoolExecutor(max_workers=MAX_PROXY_CONNECTIONS, thread_name_prefix="vsock-adb"),
        )
        self._active: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self.on_shutdown = on_shutdown
        self.allowed_cid = allowed_cid

    def _serve(self, index: int) -> None:
        listener = self.listeners[index]
        limiter = self.limiters[index]
        executor = self.executors[index]
        while not self._stopping.is_set():
            try:
                connection, address = listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            if self.allowed_cid is not None and (not address or address[0] != self.allowed_cid):
                connection.close()
                continue
            if not limiter.acquire():
                connection.close()
                continue
            with self._lock:
                self._active.add(connection)
            executor.submit(self._handle, index, connection, limiter)

    def _handle(self, index: int, connection: socket.socket, limiter: ConnectionLimiter) -> None:
        try:
            if index == 0:
                serve_control_request(connection, is_ready=lambda: not self._stopping.is_set(),
                                      on_shutdown=self.on_shutdown)
            else:
                proxy_connection(connection, lambda: socket.create_connection(ADB_TARGET, timeout=5))
        finally:
            with self._lock:
                self._active.discard(connection)
            limiter.release()

    def serve_forever(self) -> None:
        threads = [threading.Thread(target=self._serve, args=(i,), daemon=True) for i in range(2)]
        for thread in threads:
            thread.start()
        try:
            while any(thread.is_alive() for thread in threads):
                for thread in threads:
                    thread.join(timeout=0.25)
        finally:
            self.close()
            for executor in self.executors:
                executor.shutdown(wait=True, cancel_futures=True)

    def close(self) -> None:
        self._stopping.set()
        for sock in self.listeners:
            try:
                sock.close()
            except OSError:
                pass
        with self._lock:
            active = tuple(self._active)
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()


def _vsock_listener(port: int) -> socket.socket:
    if not hasattr(socket, "AF_VSOCK"):
        raise RuntimeError("Python or kernel does not provide AF_VSOCK")
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    sock.bind((socket.VMADDR_CID_ANY, port))
    sock.listen(MAX_PROXY_CONNECTIONS)
    return sock


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("/var/lib/ashnazg/redroid"))
    args = parser.parse_args()
    # The existing redroid.py supervisor starts this process only after ADB readiness.
    # It owns Docker cleanup; this child only owns the two vsock listeners.
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if b"/sbin/ashnazg-init" not in Path('/proc/1/cmdline').read_bytes():
        raise RuntimeError("the VM bridge may run only under the Ashnazg guest init")
    server = BridgeServer(_vsock_listener(CONTROL_PORT), _vsock_listener(ADB_PORT),
                          on_shutdown=lambda: os.kill(1, signal.SIGTERM), allowed_cid=socket.VMADDR_CID_HOST)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: server.close())
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
