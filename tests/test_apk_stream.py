import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from runtime.microvm.apk_stream import (
    MAX_APK_SIZE,
    MAX_HEADER_SIZE,
    UploadHeader,
    UploadError,
    read_upload_header,
    receive_apk,
)


RPC = "a" * 64
INSTALL = "b" * 64
SESSION = "12345678-1234-4234-8234-123456789abc"
APK = b"small test apk payload"


def header_object(**changes):
    value = {
        "schema_version": 1,
        "op": "prepare_agent",
        "session_id": SESSION,
        "rpc_token": RPC,
        "install_token": INSTALL,
        "apk_size": len(APK),
        "apk_sha256": hashlib.sha256(APK).hexdigest(),
    }
    value.update(changes)
    return value


def frame(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


class ApkStreamTests(unittest.TestCase):
    def parse(self, raw, *, timeout=0.5):
        receiver, sender = socket.socketpair()
        receiver.settimeout(timeout)
        sender.sendall(raw)
        sender.shutdown(socket.SHUT_WR)
        try:
            return read_upload_header(receiver, deadline=time.monotonic() + timeout)
        finally:
            receiver.close()
            sender.close()

    def test_reads_valid_header_without_consuming_payload(self):
        receiver, sender = socket.socketpair()
        deadline = time.monotonic() + 2
        sender.sendall(frame(header_object()) + APK)
        sender.shutdown(socket.SHUT_WR)
        parsed = read_upload_header(receiver, deadline=deadline)
        self.assertEqual(parsed.op, "prepare_agent")
        self.assertEqual(parsed.session_id, SESSION)
        self.assertEqual(parsed.apk_size, len(APK))
        self.assertEqual(receiver.recv(len(APK)), APK)
        receiver.close()
        sender.close()

    def test_valid_install_schema_requires_package_only_for_launch(self):
        base = {
            "schema_version": 1, "op": "install", "session_id": SESSION,
            "install_token": INSTALL, "apk_size": len(APK),
            "apk_sha256": hashlib.sha256(APK).hexdigest(), "launch": False,
        }
        parsed = self.parse(frame(base))
        self.assertFalse(parsed.launch)
        parsed = self.parse(frame({**base, "launch": True, "package": "dev.ashnazg_agent"}))
        self.assertTrue(parsed.launch)

    def test_rejects_bad_json_duplicate_keys_extra_fields_and_schema_values(self):
        malformed = [
            b"{\"schema_version\":1,\"schema_version\":1}\n",
            frame({**header_object(), "extra": "x"}),
            frame([header_object()]),
            frame({**header_object(), "schema_version": True}),
            frame({**header_object(), "apk_size": True}),
            b'{"schema_version":NaN}\n',
            b"{\n",
        ]
        for raw in malformed:
            with self.subTest(raw=raw[:30]), self.assertRaises(UploadError):
                self.parse(raw)

    def test_rejects_invalid_utf8_and_header_boundaries(self):
        for raw in (b"\xff\n", b" " * MAX_HEADER_SIZE + b"\n", b"{}" + b" " * MAX_HEADER_SIZE):
            with self.subTest(length=len(raw)), self.assertRaises(UploadError):
                self.parse(raw)

    def test_token_values_are_not_in_header_repr_or_validation_errors(self):
        parsed = self.parse(frame(header_object()))
        self.assertNotIn(RPC, repr(parsed))
        self.assertNotIn(INSTALL, repr(parsed))
        bad = frame(header_object(rpc_token="secret-value"))
        with self.assertRaises(UploadError) as caught:
            self.parse(bad)
        self.assertNotIn("secret-value", str(caught.exception))

    def test_rejects_bad_uuid_tokens_package_and_size(self):
        cases = [
            header_object(session_id=SESSION.upper()),
            header_object(rpc_token="A" * 64),
            header_object(install_token="x" * 63),
            header_object(apk_size=0),
            header_object(apk_size=MAX_APK_SIZE + 1),
        ]
        for value in cases:
            with self.subTest(value={k: v for k, v in value.items() if "token" not in k}), self.assertRaises(UploadError):
                self.parse(frame(value))
        install = {
            "schema_version": 1, "op": "install", "session_id": SESSION,
            "install_token": INSTALL, "apk_size": len(APK),
            "apk_sha256": hashlib.sha256(APK).hexdigest(), "launch": True,
        }
        for package in (None, "one", "a." + "x" * 254, "x.é", "x.y;cmd"):
            value = dict(install)
            value["package"] = package
            with self.subTest(package=package), self.assertRaises(UploadError):
                self.parse(frame(value))
        with self.assertRaises(UploadError):
            self.parse(frame({**install, "launch": False, "package": "dev.app"}))

    def upload(self, payload, *, declared=APK, digest=None, staging_dir=None, send_extra=False,
               half_close=True, send_delay=0, deadline=None, clock=time.monotonic):
        receiver, sender = socket.socketpair()
        value = header_object(apk_size=len(declared), apk_sha256=digest or hashlib.sha256(declared).hexdigest())
        sender.sendall(frame(value))
        parsed = read_upload_header(receiver, deadline=deadline or clock() + 2, clock=clock)
        errors = []

        def send():
            try:
                for offset in range(0, len(payload), 2):
                    sender.sendall(payload[offset:offset + 2])
                    if send_delay:
                        time.sleep(send_delay)
                if send_extra:
                    sender.sendall(b"!")
                if half_close:
                    sender.shutdown(socket.SHUT_WR)
            except OSError as exc:
                errors.append(exc)

        worker = threading.Thread(target=send, daemon=True)
        worker.start()
        result = None
        error = None
        try:
            result = receive_apk(receiver, parsed, staging_dir=staging_dir, deadline=deadline or clock() + 2, clock=clock)
        except Exception as exc:
            error = exc
        finally:
            receiver.close()
            sender.close()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
        return result, error, errors

    def test_upload_stages_exact_payload_and_context_removes_it(self):
        with tempfile.TemporaryDirectory() as temp:
            result, error, _ = self.upload(APK, staging_dir=temp)
            self.assertIsNone(error)
            self.assertEqual(Path(result.path).read_bytes(), APK)
            self.assertEqual(result.size, len(APK))
            self.assertEqual(result.sha256, hashlib.sha256(APK).hexdigest())
            path = Path(result.path)
            with result:
                self.assertTrue(path.exists())
            self.assertFalse(path.exists())

    def test_early_eof_hash_mismatch_and_extra_bytes_remove_partial_file(self):
        with tempfile.TemporaryDirectory() as temp:
            cases = [
                (APK[:-1], len(APK), hashlib.sha256(APK).hexdigest(), False),
                (b"wrong payload!", 14, hashlib.sha256(b"expected bytes").hexdigest(), False),
                (APK, len(APK), hashlib.sha256(APK).hexdigest(), True),
            ]
            for payload, size, digest, extra in cases:
                value = header_object(apk_size=size, apk_sha256=digest)
                value_raw = frame(value)
                receiver, sender = socket.socketpair()
                sender.sendall(value_raw)
                parsed = read_upload_header(receiver, deadline=time.monotonic() + 1)
                sender.sendall(payload + (b"x" if extra else b""))
                sender.shutdown(socket.SHUT_WR)
                with self.assertRaises(UploadError):
                    receive_apk(receiver, parsed, staging_dir=temp, deadline=time.monotonic() + 1)
                receiver.close()
                sender.close()
                self.assertEqual(list(Path(temp).iterdir()), [])

    def test_requires_finite_deadline_and_enforces_absolute_slow_progress_budget(self):
        receiver, sender = socket.socketpair()
        with self.assertRaises(UploadError):
            read_upload_header(receiver, deadline=float("inf"))
        receiver.close()
        sender.close()

        with tempfile.TemporaryDirectory() as temp:
            result, error, _ = self.upload(APK, staging_dir=temp, send_delay=0.06,
                                           deadline=time.monotonic() + 0.11)
            self.assertIsNone(result)
            self.assertIsInstance(error, UploadError)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_fake_clock_progress_cannot_extend_header_deadline(self):
        raw = frame(header_object())

        class FakeClock:
            value = 10.0

            def __call__(self):
                return self.value

        class SlowSocket:
            offset = 0

            def settimeout(self, timeout):
                self.assert_timeout = timeout

            def recv(self, count):
                chunk = raw[self.offset:self.offset + count]
                self.offset += len(chunk)
                fake_clock.value += 0.02
                return chunk

        fake_clock = FakeClock()
        with self.assertRaises(UploadError):
            read_upload_header(SlowSocket(), deadline=10.1, clock=fake_clock)

    def test_receive_revalidates_publicly_constructed_header(self):
        invalid = UploadHeader(True, "prepare_agent", SESSION, len(APK),
                               hashlib.sha256(APK).hexdigest(), RPC, INSTALL)
        with tempfile.TemporaryDirectory() as temp:
            receiver, sender = socket.socketpair()
            with self.assertRaises(UploadError):
                receive_apk(receiver, invalid, staging_dir=temp, deadline=time.monotonic() + 1)
            self.assertEqual(list(Path(temp).iterdir()), [])
            receiver.close()
            sender.close()

    def test_missing_half_close_is_bounded_by_upload_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            receiver, sender = socket.socketpair()
            sender.sendall(frame(header_object()))
            parsed = read_upload_header(receiver, deadline=time.monotonic() + 1)
            sender.sendall(APK)
            started = time.monotonic()
            with self.assertRaises(UploadError):
                receive_apk(receiver, parsed, staging_dir=temp, deadline=started + 0.12)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(list(Path(temp).iterdir()), [])
            receiver.close()
            sender.close()

    def test_refuses_symlink_nonprivate_and_nonowned_staging_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            private = base / "private"
            private.mkdir(mode=0o700)
            link = base / "link"
            link.symlink_to(private)
            loose = base / "loose"
            loose.mkdir(mode=0o755)
            for path in (link, loose):
                with self.subTest(path=path):
                    result, error, _ = self.upload(APK, staging_dir=path)
                    self.assertIsNone(result)
                    self.assertIsInstance(error, UploadError)
            receiver, sender = socket.socketpair()
            sender.sendall(frame(header_object()))
            parsed = read_upload_header(receiver, deadline=time.monotonic() + 1)
            with patch_owner_uid(123456):
                with self.assertRaises(UploadError):
                    receive_apk(receiver, parsed, staging_dir=private, deadline=time.monotonic() + 1)
            receiver.close()
            sender.close()

    def test_close_preserves_replacement_and_reports_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            result, error, _ = self.upload(APK, staging_dir=temp)
            self.assertIsNone(error)
            path = Path(result.path)
            path.unlink()
            path.symlink_to("missing-target")
            with self.assertRaises(OSError):
                result.close()
            self.assertTrue(path.is_symlink())

    def test_context_exception_removes_completed_file(self):
        with tempfile.TemporaryDirectory() as temp:
            result, error, _ = self.upload(APK, staging_dir=temp)
            self.assertIsNone(error)
            path = Path(result.path)
            with self.assertRaisesRegex(RuntimeError, "sentinel"):
                with result:
                    raise RuntimeError("sentinel")
            self.assertFalse(path.exists())


class patch_owner_uid:
    """Patch os.getuid without adding a dependency on mock helpers."""

    def __init__(self, uid):
        self.uid = uid

    def __enter__(self):
        self.old = os.getuid
        os.getuid = lambda: self.uid

    def __exit__(self, *args):
        os.getuid = self.old


if __name__ == "__main__":
    unittest.main()
