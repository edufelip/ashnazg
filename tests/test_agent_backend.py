import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from runtime.microvm import agent_backend as backend


class Staged:
    def __init__(self, data=b'apk'):
        self.file = tempfile.TemporaryFile()
        self.file.write(data)
        self.file.seek(0)
        self.size = len(data)
        self.closed = False

    def fileno(self):
        return self.file.fileno()

    def close(self):
        self.closed = True
        self.file.close()

    def __del__(self):
        try:
            self.file.close()
        except Exception:
            pass


class FakeProcess:
    next_pid = 1000

    def __init__(self, argv, **kwargs):
        self.args = argv
        self.kwargs = kwargs
        self.returncode = None
        self.stdout_data = b''
        self.stdout = None
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.processes = []
        self.agent_pid = ''
        self.forwards = ''
        self.install_output = 'Success\n'
        self.launch_output = 'Starting: Intent { cmp=example.app/.Main }\nStatus: ok\n'
        self.instrument_alive = True
        self.instrument_args = None
        self.query_output = b'example.app/.Main\n'
        self.resolve_output = b'example.app/.Main\n'
        self.fail_forward = False
        self.ping_server_token = None

        def popen(argv, **kwargs):
            if kwargs.get('stdout') is subprocess.PIPE:
                proc = FakeProcess(argv, **kwargs)
                if 'instrument' in argv:
                    self.instrument_args = argv
                else:
                    self.commands.append(argv)
                    proc.returncode = 0
                    if argv[-1:] == ['get-state']:
                        proc.stdout_data = b'device\n'
                    elif argv[-2:] == ['forward', '--list']:
                        proc.stdout_data = self.forwards.encode()
                    elif argv[-2:] == ['ps', '-A']:
                        proc.stdout_data = (
                            'USER PID PPID VSZ RSS WCHAN ADDR S NAME\nshell 42 1 0 0 0 0 S '
                            'dev.ashnazg.agent\n'
                        ).encode() if self.agent_pid else b''
                    elif argv[-2:] == ['pidof', 'dev.ashnazg.agent']:
                        proc.stdout_data = self.agent_pid.encode()
                        if not self.agent_pid:
                            proc.returncode = 1
                    elif 'install' in argv:
                        proc.stdout_data = self.install_output.encode()
                    elif 'query-activities' in argv:
                        proc.stdout_data = self.query_output
                    elif 'resolve-activity' in argv:
                        proc.stdout_data = self.resolve_output
                    elif 'start' in argv:
                        proc.stdout_data = self.launch_output.encode()
                    elif 'remove' in argv:
                        self.forwards = ''
                    elif 'tcp:17777' in argv and 'forward' in argv:
                        if self.fail_forward:
                            proc.returncode = 1
                        else:
                            self.forwards = '127.0.0.1:5555 tcp:17777 tcp:7777\n'
                read_fd, write_fd = os.pipe()
                if proc.stdout_data:
                    os.write(write_fd, proc.stdout_data)
                os.close(write_fd)
                proc.stdout = os.fdopen(read_fd, 'rb', buffering=0)
                self.processes.append(proc)
                return proc
            proc = FakeProcess(argv, **kwargs)
            self.processes.append(proc)
            if 'instrument' in argv:
                self.instrument_args = argv
            return proc

        self.popen = popen
        self.backend = backend.GuestAdbBackend()
        self.addCleanup(self._safe_close)
        self.patch = mock.patch.object(backend.subprocess, 'Popen', side_effect=popen)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _safe_close(self):
        try:
            self.backend.close()
        except backend.BackendError:
            pass

    def ping_server(self, reply=None, *, delay=0):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 17777))
        listener.listen()
        self.addCleanup(listener.close)
        if reply is None:
            reply = {'jsonrpc': '2.0', 'id': 1, 'result': {'apiLevel': 34}}
        def serve():
            try:
                conn, _ = listener.accept()
                with conn:
                    raw = b''
                    while b'\n' not in raw and len(raw) <= 4096:
                        part = conn.recv(4096)
                        if not part:
                            break
                        raw += part
                    request = json.loads(raw.split(b'\n', 1)[0])
                    self.ping_server_token = request.get('token')
                    if delay:
                        time.sleep(delay)
                    value = dict(reply)
                    value.setdefault('id', request.get('id'))
                    conn.sendall(json.dumps(value).encode() + b'\n')
            except OSError:
                pass
        threading.Thread(target=serve, daemon=True).start()
        return listener

    def test_prepare_uses_fixed_serial_descriptor_forward_and_authenticated_ping(self):
        self.ping_server()
        staged = Staged()
        port = self.backend.prepare_agent(staged, 'a' * 64, deadline=time.monotonic() + 2)
        self.assertEqual(port, 17777)
        install = next(c for c in self.commands if 'install' in c)
        self.assertEqual(install, ['/usr/bin/adb', '-s', '127.0.0.1:5555', 'shell', '-T',
                                   'cmd', 'package', 'install', '-r', '--pkg',
                                   'dev.ashnazg.agent', '-S', '3', '-'])
        proc = next(p for p in self.processes if 'install' in p.args)
        self.assertEqual(proc.kwargs['stdin'], staged.fileno())
        self.assertFalse(staged.closed)
        self.assertIn('forward', self.commands[-1])
        self.assertEqual(self.instrument_args[-4:-1], ['-e', 'token', 'a' * 64])
        self.assertEqual(self.ping_server_token, 'a' * 64)
        self.assertTrue(self.backend.is_alive())

    def test_rejects_occupied_forward_and_foreign_agent_before_mutation(self):
        self.forwards = 'someone_else tcp:17777 tcp:7777\n'
        with self.assertRaises(backend.BackendError):
            self.backend.prepare_agent(Staged(), 'a' * 64, deadline=time.monotonic() + 2)
        self.assertFalse(any('install' in c for c in self.commands))
        self.assertFalse(any('--no-rebind' in c for c in self.commands))
        self.backend.close()

    def test_running_foreign_agent_is_refused_before_install(self):
        self.agent_pid = '42'
        with self.assertRaises(backend.BackendError):
            self.backend.prepare_agent(Staged(), 'a' * 64, deadline=time.monotonic() + 2)
        self.assertFalse(any('install' in c for c in self.commands))
        self.backend.close()

    def test_install_requires_exact_success_and_expected_package(self):
        self.install_output = 'Success\nWarning: ignored\n'
        with self.assertRaises(backend.BackendError):
            self.backend._install(Staged(), package='dev.ashnazg.agent', deadline=time.monotonic() + 2)
        command = next(c for c in self.commands if 'install' in c)
        self.assertIn('--pkg', command)
        self.assertIn('dev.ashnazg.agent', command)

    def test_ordinary_install_omits_expected_package_flag(self):
        self.backend._install(Staged(), package=None, deadline=time.monotonic() + 2)
        command = next(c for c in self.commands if 'install' in c)
        self.assertEqual(command[-3:], ['-S', '3', '-'])
        self.assertNotIn('--pkg', command)

    def test_install_launch_validates_package_and_requires_status_ok(self):
        self.backend._prepared = True
        self.backend._instrumentation = FakeProcess([], stdout=subprocess.DEVNULL)
        self.ping_server()
        self.backend._ready = True
        self.launch_output = 'Status: ok\nError: failed\n'
        with self.assertRaises(backend.BackendError):
            self.backend.install(Staged(), package='example.app', deadline=time.monotonic() + 2)
        self.assertTrue(any('query-activities' in c for c in self.commands))
        with self.assertRaises(backend.BackendError):
            self.backend.install(Staged(), package='example.app;id', deadline=time.monotonic() + 2)

    def test_bad_or_oversized_ping_never_exposes_port(self):
        self.ping_server({'jsonrpc': '2.0', 'id': 999, 'result': {'apiLevel': 34}})
        with self.assertRaises(backend.BackendError):
            self.backend.prepare_agent(Staged(), 'sensitive-token', deadline=time.monotonic() + 2)
        self.assertFalse(self.backend.is_alive())
        self.assertNotIn('sensitive-token', repr(self.backend))
        self.backend.close()

    def test_ping_rejects_error_and_api_below_android_15(self):
        self.ping_server({'jsonrpc': '2.0', 'id': 1, 'error': {'code': -1},
                          'result': {'apiLevel': 34}})
        with self.assertRaises(backend.BackendError):
            self.backend.prepare_agent(Staged(), 'd' * 64, deadline=time.monotonic() + .4)

    def test_ping_is_bounded_by_absolute_deadline_and_frame_limit(self):
        self.ping_server(delay=.2)
        started = time.monotonic()
        with self.assertRaises(backend.BackendError):
            self.backend.prepare_agent(Staged(), 'e' * 64, deadline=started + .08)
        self.assertLess(time.monotonic() - started, .4)

    def test_ping_rejects_oversized_frame(self):
        self.ping_server({'jsonrpc': '2.0', 'id': 1, 'result': {'apiLevel': 34,
                          'padding': 'x' * 5000}})
        with self.assertRaises(backend.BackendError):
            self.backend.prepare_agent(Staged(), 'f' * 64, deadline=time.monotonic() + .3)

    def test_forward_creation_failure_does_not_claim_or_remove_a_forward(self):
        self.fail_forward = True
        with self.assertRaises(backend.BackendError) as caught:
            self.backend.prepare_agent(Staged(), 'b' * 64, deadline=time.monotonic() + 2)
        self.assertEqual(str(caught.exception), 'ADB operation failed')
        self.assertFalse(self.backend._forward_owned)
        self.assertFalse(any('--remove' in command for command in self.commands))
        self.assertFalse(any('force-stop' in command for command in self.commands))

    def test_query_and_resolve_must_agree_on_one_validated_activity(self):
        self.backend._ready = True
        self.backend._prepared = True
        self.backend._instrumentation = FakeProcess([], stdout=subprocess.DEVNULL)
        self.resolve_output = b'example.app/.Other\n'
        with self.assertRaises(backend.BackendError):
            self.backend.install(Staged(), package='example.app', deadline=time.monotonic() + 2)
        self.assertFalse(any('start' in command for command in self.commands))

        self.commands.clear()
        self.resolve_output = b'other.app/.Main\n'
        with self.assertRaises(backend.BackendError):
            self.backend.install(Staged(), package='example.app', deadline=time.monotonic() + 2)
        self.assertFalse(any('start' in command for command in self.commands))

    def test_expired_deadline_fails_before_any_adb_process(self):
        with self.assertRaises(backend.BackendError):
            self.backend.prepare_agent(Staged(), 'c' * 64, deadline=time.monotonic() - 1)
        self.assertEqual(self.processes, [])

    def test_close_preserves_replaced_forward_and_is_idempotent(self):
        self.backend._forward_owned = True
        self.backend._started_agent = True
        self.backend._serial = '127.0.0.1:5555'
        self.forwards = '127.0.0.1:5555 tcp:17777 tcp:9999\n'
        with self.assertRaises(backend.BackendError):
            self.backend.close()
        self.assertFalse(any('remove' in c for c in self.commands))
        self.backend.close()

    def test_rejects_concurrent_operation(self):
        self.backend._operation_lock.acquire()
        try:
            with self.assertRaises(backend.BackendError):
                self.backend.install(Staged(), package=None, deadline=time.monotonic() + 2)
        finally:
            self.backend._operation_lock.release()

    def test_real_runner_bounds_output_and_cancels_hung_child(self):
        self.patch.stop()
        runner = backend.GuestAdbBackend()
        with self.assertRaises(backend.BackendError):
            runner._run([sys.executable, '-c', "import sys;sys.stdout.write('x'*70000)"],
                        deadline=time.monotonic() + 3, cleanup=False)
        started = threading.Event()
        def command():
            started.set()
            with self.assertRaises(backend.BackendError):
                runner._run([sys.executable, '-c', 'import time;time.sleep(20)'],
                            deadline=time.monotonic() + 20, cleanup=False)
        thread = threading.Thread(target=command)
        thread.start()
        self.assertTrue(started.wait(1))
        time.sleep(.05)
        runner.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
