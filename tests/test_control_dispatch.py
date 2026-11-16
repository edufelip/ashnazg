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

from runtime.microvm.control_dispatch import serve_session_control
from runtime.microvm.agent_session import GuestSession


APK = b"verified agent fixture"
SESSION_ID = str(uuid.uuid4())
RPC_TOKEN = "a" * 64
INSTALL_TOKEN = "b" * 64


def frame(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def prepare_header():
    return {
        "schema_version": 1, "op": "prepare_agent", "session_id": SESSION_ID,
        "rpc_token": RPC_TOKEN, "install_token": INSTALL_TOKEN,
        "apk_size": len(APK), "apk_sha256": hashlib.sha256(APK).hexdigest(),
    }


class FakeBackend:
    def __init__(self):
        self.prepared = []
        self.alive = True
        self.ready = threading.Event()

    def prepare_agent(self, staged_apk, rpc_token, *, deadline):
        self.prepared.append((os.read(staged_apk.fileno(), staged_apk.size), rpc_token, deadline))
        self.ready.set()
        return 17777

    def install(self, staged_apk, *, package, deadline):
        raise AssertionError("install is not part of routed preparation")

    def is_alive(self):
        return self.alive

    def close(self):
        self.alive = False


class ControlDispatchTests(unittest.TestCase):
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

    def _serve(self, payload, *, peer_cid=2, ready=True, shutdown=None, session=None,
               send_payload=True):
        server, client = socket.socketpair()
        client.settimeout(2)
        entered = time.monotonic()
        result = {}

        def serve():
            try:
                serve_session_control(server, session, peer_cid=peer_cid,
                                      is_ready=lambda: ready, on_shutdown=shutdown)
            except BaseException as error:
                result["error"] = error

        worker = threading.Thread(target=serve)
        worker.start()
        if send_payload:
            try:
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        response = bytearray()
        while True:
            try:
                block = client.recv(4096)
            except (ConnectionResetError, OSError):
                break
            if not block:
                break
            response.extend(block)
        worker.join(3)
        self.assertFalse(worker.is_alive(), "dispatcher did not finish")
        client.close()
        return bytes(response), result.get("error"), time.monotonic() - entered

    def test_legacy_status_preserves_response_and_readiness_gate(self):
        response, error, _ = self._serve(b'{"method":"status"}\n')
        self.assertIsNone(error)
        self.assertEqual(json.loads(response), {
            "schema_version": 1, "status": "ready", "android_serial": "127.0.0.1:5555"})
        self.assertEqual(self._serve(b'{"method":"status"}\n', ready=False)[0], b"")

    def test_legacy_shutdown_acknowledges_then_invokes_callback(self):
        called = []
        response, error, _ = self._serve(b'{"method":"shutdown"}\n', ready=False,
                                         shutdown=lambda: called.append(True))
        self.assertIsNone(error)
        self.assertEqual(json.loads(response), {"schema_version": 1, "status": "stopping"})
        self.assertEqual(called, [True])

    def test_wrong_cid_closes_before_reading(self):
        server, client = socket.socketpair()
        serve_session_control(server, None, peer_cid=7, is_ready=lambda: True)
        client.settimeout(.1)
        self.assertEqual(client.recv(1), b"")
        client.close()

    def test_strict_header_rejections_have_no_response_or_side_effects(self):
        for payload in (
                b'{"method":"status","method":"shutdown"}\n',
                b'{"op":"prepare_agent","n":NaN}\n',
                b'{"op":"prepare_agent","n":1e999}\n',
                b'[]\n', b'{}\n', b'{"op":"unknown"}\n',
                b'\xff\n', b"x" * 4096 + b"\n"):
            with self.subTest(payload=payload[:32]):
                response, error, _ = self._serve(payload)
                self.assertIsNone(error)
                self.assertEqual(response, b"")

    def test_dispatcher_replays_only_header_and_session_reads_payload_after_ack(self):
        backend = FakeBackend()
        session = GuestSession(backend)
        self.addCleanup(session.close)
        server, client = socket.socketpair()
        client.settimeout(2)
        result = {}

        def serve():
            try:
                serve_session_control(server, session, peer_cid=2, is_ready=lambda: True)
            except BaseException as error:
                result["error"] = error

        worker = threading.Thread(target=serve)
        worker.start()
        client.sendall(frame(prepare_header()))
        first = client.recv(4096)
        self.assertEqual(json.loads(first), {"schema_version": 1, "status": "ready_for_payload"})
        self.assertEqual(backend.prepared, [])
        client.sendall(APK)
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        second = client.recv(4096)
        self.assertEqual(json.loads(second), {
            "schema_version": 1, "status": "agent_ready", "session_id": SESSION_ID})
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(backend.prepared[0][:2], (APK, RPC_TOKEN))
        self.assertEqual(session.agent_endpoint(), ("127.0.0.1", 17777))
        client.close()

    def test_slow_routed_header_preserves_entry_upload_and_whole_deadlines(self):
        class RecordingSession:
            def __init__(self):
                self.args = None
                self.replayed = b""

            def serve_prepare(self, connection, *, peer_cid, deadline, upload_deadline):
                self.args = (peer_cid, deadline, upload_deadline)
                while True:
                    chunk = connection.recv(1)
                    if not chunk:
                        break
                    self.replayed += chunk
                    if chunk == b"\n":
                        break
                connection.close()

        session = RecordingSession()
        server, client = socket.socketpair()
        class FakeClock:
            def __init__(self):
                self.value = 100.0

            def __call__(self):
                value = self.value
                self.value += .05
                return value

        clock = FakeClock()
        entry = clock.value
        worker = threading.Thread(target=serve_session_control, args=(server, session),
                                  kwargs={"peer_cid": 2, "is_ready": lambda: True,
                                          "clock": clock})
        worker.start()
        header = frame({"op": "prepare_agent"})
        for byte in header:
            client.sendall(bytes([byte]))
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(session.replayed, header)
        self.assertEqual(session.args[0], 2)
        self.assertAlmostEqual(session.args[1], entry + 180, delta=.02)
        self.assertAlmostEqual(session.args[2], entry + 30, delta=.02)
        client.close()

    def test_payload_before_ack_is_not_consumed_by_dispatcher(self):
        server, client = socket.socketpair()
        entered = threading.Event()
        class WaitingSession:
            def serve_prepare(self, connection, *, peer_cid, deadline, upload_deadline):
                entered.set()
                connection.close()
        worker = threading.Thread(target=serve_session_control, args=(server, WaitingSession()),
                                  kwargs={"peer_cid": 2, "is_ready": lambda: True})
        worker.start()
        client.sendall(frame({"op": "prepare_agent"}) + b"BODY")
        self.assertTrue(entered.wait(1))
        worker.join(1)
        self.assertFalse(worker.is_alive())
        client.close()


if __name__ == "__main__":
    unittest.main()
