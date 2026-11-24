import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from runtime.microvm.agent_session import GuestSession, SessionError


APK = b"agent apk bytes"
SESSION = str(uuid.uuid4())
RPC = "a" * 64
INSTALL = "b" * 64


def frame(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def prepare_header(**changes):
    value = {
        "schema_version": 1, "op": "prepare_agent", "session_id": SESSION,
        "rpc_token": RPC, "install_token": INSTALL, "apk_size": len(APK),
        "apk_sha256": hashlib.sha256(APK).hexdigest(),
    }
    value.update(changes)
    return value


def install_header(**changes):
    value = {
        "schema_version": 1, "op": "install", "session_id": SESSION,
        "install_token": INSTALL, "apk_size": len(APK),
        "apk_sha256": hashlib.sha256(APK).hexdigest(), "launch": False,
    }
    value.update(changes)
    return value


class FakeBackend:
    def __init__(self):
        self.alive = True
        self.closed = False
        self.prepared = []
        self.installed = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_prepare = False
        self.ignore_cancel = False
        self.prepare_error = None
        self.install_error = None
        self.streams = []

    def prepare_agent(self, staged_apk, rpc_token, *, deadline):
        self.entered.set()
        self.prepared.append((staged_apk.fileno(), rpc_token, deadline))
        if self.block_prepare:
            self.release.wait(3)
        if self.prepare_error:
            raise self.prepare_error
        return None

    def install(self, staged_apk, *, package, deadline):
        self.installed.append((staged_apk.fileno(), package, deadline))
        if self.install_error:
            raise self.install_error
        return None

    def open_agent_stream(self, *, deadline):
        stream, peer = socket.socketpair()
        self.streams.extend([stream, peer])
        return stream

    def is_alive(self):
        return self.alive and not self.closed

    def close(self):
        self.closed = True
        self.alive = False
        for stream in self.streams:
            stream.close()
        if not self.ignore_cancel:
            self.release.set()


class GuestSessionTests(unittest.TestCase):
    def setUp(self):
        self.storage_patches = []
        if not sys.platform.startswith("linux"):
            def anonymous_fixture():
                with tempfile.TemporaryFile() as file:
                    return os.dup(file.fileno())
            for target, replacement in (
                    ("runtime.microvm.apk_stream._new_memfd", anonymous_fixture),
                    ("runtime.microvm.apk_stream._seal_memfd", lambda _fd: None)):
                patcher = mock.patch(target, side_effect=replacement)
                patcher.start()
                self.storage_patches.append(patcher)
                self.addCleanup(patcher.stop)
        self.backend = FakeBackend()
        self.session = GuestSession(self.backend)
        self.addCleanup(self.session.close)

    def run_request(self, method, header=None, *, peer_cid=2, payload=APK, deadline=None):
        server, client = socket.socketpair()
        client.settimeout(2)
        operation = getattr(self.session, method)
        result = {}

        def serve():
            try:
                operation(server, peer_cid=peer_cid,
                          deadline=deadline if deadline is not None else time.monotonic() + 3)
            except BaseException as exc:
                result["error"] = exc

        worker = threading.Thread(target=serve)
        worker.start()
        if header is not None:
            try:
                client.sendall(frame(header) + payload)
                client.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        response = b""
        while True:
            try:
                chunk = client.recv(4096)
            except (ConnectionResetError, OSError):
                break
            if not chunk:
                break
            response += chunk
        worker.join(3)
        self.assertFalse(worker.is_alive(), "serve operation did not finish")
        client.close()
        return response, result.get("error")

    def test_prepare_ack_and_success_follow_backend_and_staged_descriptor_is_closed(self):
        response, error = self.run_request("serve_prepare", prepare_header())
        self.assertIsNone(error)
        messages = [json.loads(line) for line in response.splitlines()]
        self.assertEqual(messages[0], {"schema_version": 1, "status": "ready_for_payload"})
        self.assertEqual(messages[1], {"schema_version": 1, "status": "agent_ready", "session_id": SESSION})
        self.assertEqual(self.backend.prepared[0][1], RPC)
        with self.assertRaises(OSError):
            os.fstat(self.backend.prepared[0][0])
        self.assertTrue(self.session.agent_available())
        stream = self.session.open_agent_stream(deadline=time.monotonic() + 1)
        self.assertGreaterEqual(stream.fileno(), 0)
        stream.close()

    def test_wrong_cid_is_rejected_before_header_and_connection_is_closed(self):
        response, _ = self.run_request("serve_prepare", None, peer_cid=7)
        self.assertIn(b"unauthorized", response)
        self.assertFalse(self.backend.prepared)

    def test_prepare_rejects_wrong_operation_and_duplicate_credentials_before_ack(self):
        for header in (install_header(), prepare_header(rpc_token=INSTALL)):
            with self.subTest(header="operation" if header["op"] == "install" else "tokens"):
                response, _ = self.run_request("serve_prepare", header)
                self.assertNotIn(b"ready_for_payload", response)
                self.assertFalse(self.backend.prepared)

    def test_install_requires_ready_exact_uuid_and_constant_time_token_before_ack(self):
        self.run_request("serve_prepare", prepare_header())
        for header in (install_header(session_id=str(uuid.uuid4())),
                       install_header(install_token="c" * 64)):
            response, _ = self.run_request("serve_install", header)
            self.assertNotIn(b"ready_for_payload", response)
        self.assertFalse(self.backend.installed)

    def test_install_succeeds_with_optional_validated_package_launch(self):
        self.run_request("serve_prepare", prepare_header())
        response, error = self.run_request("serve_install", install_header())
        self.assertIsNone(error)
        self.assertEqual(json.loads(response.splitlines()[-1]), {
            "schema_version": 1, "status": "installed", "session_id": SESSION, "launched": False})
        response, error = self.run_request(
            "serve_install", install_header(launch=True, package="com.example.app"))
        self.assertIsNone(error)
        messages = [json.loads(line) for line in response.splitlines()]
        self.assertEqual(messages[-1], {"schema_version": 1, "status": "installed",
                                      "session_id": SESSION, "launched": True})
        self.assertEqual(self.backend.installed[1][1], "com.example.app")

    def test_agent_stream_is_unavailable_before_preparation(self):
        with self.assertRaises(SessionError):
            self.session.open_agent_stream(deadline=time.monotonic() + 1)

    def test_install_is_rejected_while_session_is_not_ready(self):
        response, _ = self.run_request("serve_install", install_header())
        self.assertNotIn(b"ready_for_payload", response)
        self.assertFalse(self.backend.installed)

    def test_malformed_header_is_generic_and_terminal_for_initial_prepare(self):
        server, client = socket.socketpair()
        client.settimeout(2)
        worker = threading.Thread(target=lambda: self.session.serve_prepare(
            server, peer_cid=2, deadline=time.monotonic() + 1))
        worker.start()
        client.sendall(b'{"op":"prepare_agent","rpc_token":"' + RPC.encode() + b'"\n')
        client.shutdown(socket.SHUT_WR)
        response = client.recv(4096)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIn(b"invalid_upload", response)
        self.assertNotIn(RPC.encode(), response)
        self.assertTrue(self.backend.closed)
        client.close()

    def test_close_during_payload_upload_interrupts_and_drains(self):
        server, client = socket.socketpair()
        client.settimeout(2)
        worker = threading.Thread(target=lambda: self.session.serve_prepare(
            server, peer_cid=2, deadline=time.monotonic() + 10))
        worker.start()
        client.sendall(frame(prepare_header()))
        ack = client.recv(4096)
        self.assertIn(b"ready_for_payload", ack)
        self.session.close()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(self.session.drain(timeout=0.05))
        self.assertTrue(self.backend.closed)
        client.close()

    def test_second_prepare_is_rejected_for_live_session(self):
        self.run_request("serve_prepare", prepare_header())
        response, _ = self.run_request("serve_prepare", prepare_header())
        self.assertNotIn(b"ready_for_payload", response)
        self.assertEqual(len(self.backend.prepared), 1)

    def test_zero_byte_probe_does_not_block_or_disturb_subsequent_operations(self):
        # Probing prepare port before real prepare must leave session untouched.
        response, error = self.run_request("serve_prepare", None)
        self.assertIsNone(error)
        self.assertEqual(response, b"")
        self.assertFalse(self.backend.closed)

        # Real prepare succeeds afterward.
        response, error = self.run_request("serve_prepare", prepare_header())
        self.assertIsNone(error)
        self.assertIn(b"agent_ready", response)

        # Hold a zero-byte idle/probe socket open across RelayServer/vsock while
        # a real install connects immediately on another worker.
        probe_server, probe_client = socket.socketpair()
        probe_done = threading.Event()
        probe_worker = threading.Thread(target=lambda: (
            self.session.serve_install(probe_server, peer_cid=2, deadline=time.monotonic() + 3),
            probe_done.set(),
        ))
        probe_worker.start()
        time.sleep(0.02)

        response, error = self.run_request("serve_install", install_header())
        self.assertIsNone(error)
        self.assertNotIn(b"busy", response)
        self.assertIn(b"installed", response)
        self.assertEqual(len(self.backend.installed), 1)

        probe_client.shutdown(socket.SHUT_WR)
        probe_worker.join(1)
        self.assertTrue(probe_done.is_set())
        probe_client.close()

    def test_one_upload_slot_covers_backend_call_and_second_live_prepare_is_refused(self):
        self.backend.block_prepare = True
        first = self.start_request("serve_prepare", prepare_header())
        self.assertTrue(self.backend.entered.wait(1))
        response, _ = self.run_request("serve_prepare", prepare_header(session_id=str(uuid.uuid4())))
        self.assertNotIn(b"ready_for_payload", response)
        self.backend.release.set()
        first["worker"].join(3)
        self.assertFalse(first["worker"].is_alive())
        first["client"].close()

    def start_request(self, method, header):
        server, client = socket.socketpair()
        client.settimeout(2)
        state = {}
        def serve():
            try:
                getattr(self.session, method)(server, peer_cid=2, deadline=time.monotonic() + 3)
            except BaseException as exc:
                state["error"] = exc
        worker = threading.Thread(target=serve)
        worker.start()
        client.sendall(frame(header) + APK)
        # Keep the write side open until ACK to prove the upload slot extends into backend work.
        ack = b""
        while b"ready_for_payload" not in ack:
            ack += client.recv(4096)
        client.shutdown(socket.SHUT_WR)
        return {"client": client, "worker": worker, "state": state, "ack": ack}

    def test_expired_whole_deadline_does_not_ack_or_call_backend(self):
        response, _ = self.run_request("serve_prepare", prepare_header(),
                                       deadline=time.monotonic() - 1)
        self.assertNotIn(b"ready_for_payload", response)
        self.assertFalse(self.backend.prepared)

    def test_truncated_payload_and_backend_failure_close_staged_data_and_terminalize(self):
        response, _ = self.run_request("serve_prepare", prepare_header(), payload=APK[:-1])
        self.assertIn(b"error", response)
        self.assertFalse(self.backend.prepared)
        self.backend.prepare_error = RuntimeError("secret backend text")
        response, _ = self.run_request("serve_prepare", prepare_header(session_id=str(uuid.uuid4())))
        self.assertNotIn(b"secret backend text", response)
        self.assertTrue(self.backend.closed)
        self.assertFalse(self.session.agent_available())

    def test_backend_loss_invalidates_endpoint_and_requires_restart(self):
        self.run_request("serve_prepare", prepare_header())
        self.backend.alive = False
        self.assertFalse(self.session.agent_available())
        response, _ = self.run_request("serve_install", install_header())
        self.assertNotIn(b"ready_for_payload", response)
        self.assertFalse(self.backend.installed)

    def test_close_cancels_backend_concurrently_and_prevents_ready_publication(self):
        self.backend.block_prepare = True
        self.backend.ignore_cancel = True
        started = self.start_request("serve_prepare", prepare_header())
        self.assertTrue(self.backend.entered.wait(1))
        self.session.close()
        with self.assertRaises(SessionError):
            self.session.drain(timeout=0.05)
        self.backend.release.set()
        started["worker"].join(2)
        self.assertFalse(started["worker"].is_alive())
        self.assertTrue(self.backend.closed)
        self.assertNotIn(b"agent_ready", started["ack"])
        tail = b""
        try:
            while True:
                chunk = started["client"].recv(4096)
                if not chunk:
                    break
                tail += chunk
        except (ConnectionResetError, OSError):
            pass
        self.assertNotIn(b"agent_ready", tail)
        started["client"].close()

    def test_close_during_header_read_closes_connection_and_bounded_drain_reports_timeout(self):
        server, client = socket.socketpair()
        worker = threading.Thread(target=lambda: self.session.serve_prepare(
            server, peer_cid=2, deadline=time.monotonic() + 10))
        worker.start()
        time.sleep(0.02)
        self.session.close()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        client.close()
        self.assertTrue(self.session.drain(timeout=0.05))

    def test_drain_waits_for_backend_cleanup_and_reports_failure_without_secret(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_close():
            entered.set()
            release.wait(2)
        self.backend.close = blocked_close
        closer = threading.Thread(target=self.session.close)
        closer.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(SessionError, "timed out"):
                self.session.drain(timeout=0.01)
        finally:
            release.set()
            closer.join(1)
        self.assertFalse(closer.is_alive())
        self.assertTrue(self.session.drain(timeout=0.05))

        failed = GuestSession(FakeBackend())
        failed._backend.close = mock.Mock(side_effect=RuntimeError(INSTALL))
        failed.close()
        with self.assertRaisesRegex(SessionError, "backend cleanup failed") as caught:
            failed.drain(timeout=0.05)
        self.assertNotIn(INSTALL, str(caught.exception))

    def test_close_is_idempotent_and_repr_and_replies_never_contain_tokens(self):
        self.assertNotIn(RPC, repr(self.session))
        self.assertNotIn(INSTALL, repr(self.session))
        response, _ = self.run_request("serve_prepare", prepare_header(rpc_token="c" * 64))
        self.assertNotIn(RPC.encode(), response)
        self.assertNotIn(INSTALL.encode(), response)
        self.session.close()
        self.session.close()

    def test_prepare_deadline_passed_to_backend_is_remaining_absolute_deadline(self):
        end = time.monotonic() + 2
        self.run_request("serve_prepare", prepare_header(), deadline=end)
        self.assertGreater(self.backend.prepared[0][2], time.monotonic())
        self.assertLessEqual(self.backend.prepared[0][2], end)

    def test_transport_header_time_cannot_reset_the_upload_deadline(self):
        now = [125.0]
        backend = FakeBackend()
        session = GuestSession(backend, clock=lambda: now[0])
        server, client = socket.socketpair()
        client.settimeout(1)
        worker = threading.Thread(target=lambda: session.serve_prepare(
            server, peer_cid=2, deadline=1000.0, upload_deadline=130.0))
        worker.start()
        try:
            client.sendall(frame(prepare_header()))
            with client.makefile("rb", buffering=0) as reader:
                ack = reader.readline(4096)
            self.assertIn(b"ready_for_payload", ack)
            now[0] = 131.0
            try:
                client.sendall(APK)
                client.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertFalse(backend.prepared)
            self.assertFalse(session.agent_available())
        finally:
            client.close()
            session.close()
            server.close()
            worker.join(1)

    def test_fake_clock_caps_whole_operation_and_backend_receives_absolute_deadline(self):
        backend = FakeBackend()
        session = GuestSession(backend, clock=lambda: 100.0)
        server, client = socket.socketpair()
        client.settimeout(2)
        worker = threading.Thread(target=lambda: session.serve_prepare(
            server, peer_cid=2, deadline=1000.0))
        worker.start()
        client.sendall(frame(prepare_header()) + APK)
        client.shutdown(socket.SHUT_WR)
        while client.recv(4096):
            pass
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(backend.prepared[0][2], 280.0)
        client.close()
        session.close()


if __name__ == "__main__":
    unittest.main()
