import socket
import threading
import time
import unittest
from unittest import mock

from runtime.microvm import adb_agent_stream as stream


class AdbAgentStreamTests(unittest.TestCase):
    def server(self, handler):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        listener.settimeout(0.1)
        self.addCleanup(listener.close)
        self.patch_host = mock.patch.object(stream, "ADB_SERVER_PORT", listener.getsockname()[1])
        self.patch_host.start()
        self.addCleanup(self.patch_host.stop)
        errors = []
        stopping = threading.Event()

        def run():
            while not stopping.is_set():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                thread = threading.Thread(target=lambda: self._serve(conn, handler, errors), daemon=True)
                thread.start()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        def stop_server():
            stopping.set()
            try:
                listener.close()
            except OSError:
                pass
            worker.join(1)
        self.addCleanup(stop_server)
        self.addCleanup(lambda: self.assertFalse(errors, repr(errors)))
        return errors

    @staticmethod
    def _serve(conn, handler, errors):
        try:
            with conn:
                handler(conn)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except BaseException as exc:
            errors.append(exc)

    @staticmethod
    def _read_request(conn):
        header = b""
        while len(header) < 4:
            part = conn.recv(4 - len(header))
            if not part:
                raise AssertionError("truncated request length")
            header += part
        size = int(header, 16)
        body = b""
        while len(body) < size:
            part = conn.recv(size - len(body))
            if not part:
                raise AssertionError("truncated request body")
            body += part
        return header + body

    def test_fixed_transport_and_service_handshake_then_same_raw_duplex_stream(self):
        seen = []

        def handler(conn):
            seen.append(self._read_request(conn))
            conn.sendall(b"O")
            conn.sendall(b"KAY")
            seen.append(self._read_request(conn))
            conn.sendall(b"OKAY")
            self.assertEqual(conn.recv(4), b"ping")
            conn.sendall(b"pong")
            self.assertEqual(conn.recv(4), b"half")
            self.assertEqual(conn.recv(1), b"")
            conn.sendall(b"tail")
            conn.shutdown(socket.SHUT_WR)

        self.server(handler)
        pool = stream.AgentStreamPool()
        owned = pool.open(deadline=time.monotonic() + 2)
        self.addCleanup(pool.close)
        self.assertEqual(seen, [b"001dhost:transport:127.0.0.1:5555",
                                b"0008tcp:7777"])
        owned.sendall(b"ping")
        self.assertEqual(owned.recv(4), b"pong")
        owned.sendall(b"half")
        owned.shutdown(socket.SHUT_WR)
        self.assertEqual(owned.recv(4), b"tail")
        self.assertEqual(owned.recv(1), b"")
        owned.close()
        owned.close()

    def test_fail_frame_is_discarded_and_secret_text_never_escapes(self):
        secret = b"password=do-not-leak"

        def handler(conn):
            self._read_request(conn)
            conn.sendall(b"FAIL" + f"{len(secret):04x}".encode() + secret)

        self.server(handler)
        pool = stream.AgentStreamPool()
        with self.assertRaises(stream.AgentStreamError) as caught:
            pool.open(deadline=time.monotonic() + 2)
        self.assertNotIn(secret.decode(), str(caught.exception))
        self.assertNotIn(secret.decode(), repr(pool))
        pool.close()

    def test_bad_status_length_and_oversized_failure_are_generic(self):
        for reply in (b"NOPE", b"FAILzzzz", b"FAIL1001"):
            with self.subTest(reply=reply):
                def handler(conn, reply=reply):
                    self._read_request(conn)
                    conn.sendall(reply)

                self.server(handler)
                pool = stream.AgentStreamPool()
                with self.assertRaises(stream.AgentStreamError) as caught:
                    pool.open(deadline=time.monotonic() + 1)
                self.assertEqual(str(caught.exception), "agent stream unavailable")
                pool.close()

    def test_truncated_failure_body_and_truncated_status_fail(self):
        for reply in (b"F", b"FAIL0004xy", b"OK"):
            with self.subTest(reply=reply):
                def handler(conn, reply=reply):
                    self._read_request(conn)
                    conn.sendall(reply)
                    conn.shutdown(socket.SHUT_WR)

                self.server(handler)
                pool = stream.AgentStreamPool()
                with self.assertRaises(stream.AgentStreamError):
                    pool.open(deadline=time.monotonic() + 1)
                pool.close()

    def test_absolute_five_second_cap_does_not_reset_on_progress(self):
        def handler(conn):
            self._read_request(conn)
            for byte in b"OKAY":
                try:
                    conn.sendall(bytes([byte]))
                except OSError:
                    return
                time.sleep(0.035)
            self._read_request(conn)
            for _ in range(4):
                try:
                    conn.sendall(b"O")
                except OSError:
                    return
                time.sleep(1.35)

        self.server(handler)
        pool = stream.AgentStreamPool()
        started = time.monotonic()
        with self.assertRaises(stream.AgentStreamError):
            pool.open(deadline=started + 30)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.7)
        pool.close()

    def test_eight_owned_connections_are_admitted_before_connect_and_reused(self):
        waiting = threading.Event()
        release = threading.Event()
        accepted = []

        def handler(conn):
            self._read_request(conn)
            conn.sendall(b"OKAY")
            self._read_request(conn)
            conn.sendall(b"OKAY")
            accepted.append(conn)
            waiting.set()
            release.wait(2)

        self.server(handler)
        pool = stream.AgentStreamPool()
        opened = [pool.open(deadline=time.monotonic() + 2) for _ in range(8)]
        self.assertTrue(waiting.wait(1))
        with self.assertRaises(stream.AgentStreamError):
            pool.open(deadline=time.monotonic() + 1)
        opened[0].close()
        replacement = pool.open(deadline=time.monotonic() + 2)
        opened[1].close()
        replacement.close()
        release.set()
        for item in opened:
            item.close()
        pool.close()

    def test_cancel_interrupts_pending_handshake_and_pool_close_interrupts_traffic(self):
        cancel = threading.Event()

        def handler(conn):
            self._read_request(conn)
            time.sleep(1)

        self.server(handler)
        pool = stream.AgentStreamPool(cancel=cancel)
        result = []
        worker = threading.Thread(target=lambda: self._capture_open(pool, result))
        worker.start()
        time.sleep(0.1)
        cancel.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["failed"])
        pool.close()

        def raw_handler(conn):
            self._read_request(conn)
            conn.sendall(b"OKAY")
            self._read_request(conn)
            conn.sendall(b"OKAY")
            conn.recv(1)

        self.server(raw_handler)
        pool2 = stream.AgentStreamPool()
        owned = pool2.open(deadline=time.monotonic() + 2)
        pool2.close()
        self.assertEqual(owned.fileno(), -1)
        with self.assertRaises((OSError, stream.AgentStreamError)):
            owned.send(b"x")
        with self.assertRaises(stream.AgentStreamError):
            pool2.open(deadline=time.monotonic() + 1)

    @staticmethod
    def _capture_open(pool, result):
        try:
            pool.open(deadline=time.monotonic() + 4)
            result.append("opened")
        except stream.AgentStreamError:
            result.append("failed")

    def test_pool_close_rejects_new_opens_and_repr_is_generic(self):
        pool = stream.AgentStreamPool()
        self.assertEqual(repr(pool), "AgentStreamPool(active=0, closed=False)")
        pool.close()
        pool.close()
        with self.assertRaises(stream.AgentStreamError):
            pool.open(deadline=time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
