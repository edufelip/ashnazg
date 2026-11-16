import json
import socket
import threading
import time
import unittest
from unittest import mock

from runtime.microvm.guest_bridge import BridgeServer
from runtime.microvm import guest_bridge


class Listener:
    def __init__(self, accepted=()):
        self.pending = list(accepted)
        self.closed = False

    def accept(self):
        if self.closed or not self.pending:
            raise OSError('listener ended')
        return self.pending.pop(0)

    def close(self):
        self.closed = True


class Session:
    def __init__(self):
        self.ready = True
        self.calls = []
        self.closed = False
        self.fail_cleanup = False

    def serve_install(self, connection, *, peer_cid, deadline):
        self.calls.append((peer_cid, deadline))
        connection.sendall(b'{"status":"installed"}\n')
        connection.close()

    def open_agent_stream(self, *, deadline):
        if not self.ready:
            raise RuntimeError('agent stream unavailable')
        raise AssertionError('ready stream fixture not configured')

    def close(self):
        self.closed = True
        if self.fail_cleanup:
            raise RuntimeError('secret cleanup text')

    def drain(self, *, timeout):
        assert 0 <= timeout <= 10
        return True


class SessionBridgeTests(unittest.TestCase):
    def bridge(self, session=None, accepted=(), index=0, handler=None, allowed_cid=2):
        listeners = [Listener() for _ in range(4)]
        listeners[index] = Listener(accepted)
        session = session or Session()
        server = BridgeServer(listeners[0], listeners[1], allowed_cid=allowed_cid,
                              agent_listener=listeners[2], install_listener=listeners[3],
                              session=session, session_control=handler or mock.Mock())
        self.addCleanup(lambda: self.shutdown(server))
        return server, session, listeners

    def shutdown(self, server):
        try:
            server.close()
        except RuntimeError:
            pass
        for executor in server.executors:
            executor.shutdown(wait=True, cancel_futures=True)

    def pair(self):
        server, client = socket.socketpair()
        client.settimeout(1)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        return server, client

    def test_session_mode_requires_observed_host_cid_policy(self):
        with self.assertRaises(ValueError):
            self.bridge(allowed_cid=None)

    def test_control_dispatch_receives_actual_peer_cid(self):
        connection, client = self.pair()
        called = threading.Event()
        def control(conn, session, **options):
            self.assertEqual(options['peer_cid'], 2)
            conn.sendall(b'control\n')
            called.set()
        server, _, _ = self.bridge(accepted=[(connection, (2, 101))], handler=control)
        server._serve(0)
        self.assertTrue(called.wait(1))
        self.assertEqual(client.recv(100), b'control\n')

    def test_installer_channel_passes_cid_and_finite_deadline(self):
        connection, client = self.pair()
        server, session, _ = self.bridge(accepted=[(connection, (2, 55))], index=3)
        before = time.monotonic()
        server._serve(3)
        self.assertEqual(json.loads(client.recv(100)), {'status': 'installed'})
        self.assertEqual(session.calls[0][0], 2)
        self.assertLessEqual(session.calls[0][1], before + 181)
        self.assertGreater(session.calls[0][1], before)

    def test_wrong_peer_is_closed_before_session_dispatch(self):
        connection, client = self.pair()
        server, session, _ = self.bridge(accepted=[(connection, (4, 55))], index=3)
        server._serve(3)
        self.assertEqual(client.recv(100), b'')
        self.assertEqual(session.calls, [])

    def test_unready_agent_proxy_never_connects_upstream(self):
        connection, client = self.pair()
        session = Session()
        session.ready = False
        server, _, _ = self.bridge(session=session, accepted=[(connection, (2, 55))], index=2)
        with mock.patch('runtime.microvm.guest_bridge.socket.create_connection') as connect:
            server._serve(2)
            self.assertEqual(client.recv(100), b'')
            connect.assert_not_called()

    def test_close_prevents_new_admissions_and_closes_all_listeners(self):
        connection, client = self.pair()
        server, session, listeners = self.bridge(accepted=[(connection, (2, 55))], index=3)
        server.close()
        server._serve(3)
        self.assertTrue(all(l.closed for l in listeners))
        self.assertTrue(session.closed)
        self.assertFalse(session.calls)
        self.assertFalse(server._active)

    def test_shutdown_between_accept_and_registration_refuses_connection(self):
        connection, client = self.pair()
        server, session, listeners = self.bridge(index=3)
        def accept_then_shutdown():
            server.close()
            return connection, (2, 55)
        listeners[3].accept = accept_then_shutdown
        server._serve(3)
        self.assertEqual(client.recv(100), b'')
        self.assertFalse(session.calls)
        self.assertFalse(server._active)

    def test_legacy_two_listener_constructor_remains_usable(self):
        listeners = [Listener(), Listener()]
        server = BridgeServer(*listeners, allowed_cid=2)
        self.addCleanup(lambda: self.shutdown(server))
        self.assertEqual(len(server.listeners), 2)
        server.close()
        self.assertTrue(all(listener.closed for listener in listeners))

    def test_factory_binds_all_fixed_guest_channels(self):
        ports = []
        session = Session()
        def listener(port):
            ports.append(port)
            return Listener()
        server = guest_bridge.make_session_bridge(
            on_shutdown=lambda: None, listener_factory=listener,
            session=session, session_control=mock.Mock())
        self.addCleanup(lambda: self.shutdown(server))
        self.assertEqual(ports, [10777, 15555, 17777, 17778])
        self.assertEqual(server.allowed_cid, 2)

    def test_partial_listener_startup_closes_owned_listeners_and_session(self):
        listeners = []
        session = Session()
        def listener(port):
            if port == 17777:
                raise OSError('startup failed')
            value = Listener()
            listeners.append(value)
            return value
        with self.assertRaises(RuntimeError):
            guest_bridge.make_session_bridge(
                on_shutdown=lambda: None, listener_factory=listener,
                session=session, session_control=mock.Mock())
        self.assertTrue(listeners)
        self.assertTrue(all(value.closed for value in listeners))
        self.assertTrue(session.closed)

    def test_cleanup_failure_is_reported_without_exception_text(self):
        session = Session()
        session.fail_cleanup = True
        server, _, _ = self.bridge(session=session)
        with self.assertRaisesRegex(RuntimeError, 'session cleanup failed') as caught:
            server.close()
        self.assertNotIn('secret cleanup text', str(caught.exception))
