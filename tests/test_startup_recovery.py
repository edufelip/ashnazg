import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import redroid, dev_session


class StartupRecoveryTests(unittest.TestCase):
    def run_case(self, failures, cleanup_failure=False, attempts=2, host_code=0, diagnostic_skipped=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        s = redroid.RedroidSupervisor(output_dir=Path(temp.name), timeout=10,
                                     startup_attempts=attempts, docker_path='docker', adb_path='adb')
        identities = []
        def launch():
            identities.append(s.run_id)
            s.container_started = True
        def cleanup():
            if cleanup_failure:
                s.receipt['cleanup_error'] = 'owned container removal unverified'
        def diagnostics(**kwargs):
            if diagnostic_skipped:
                s.receipt['diagnostics_skipped'] = 'startup recovery reserved remaining time'
        with patch.object(s, '_preflight'), patch.object(s, '_check_endpoint'), patch.object(s, '_launch_container', side_effect=launch), \
             patch.object(s, '_wait_ready', side_effect=failures), patch.object(s, '_collect_diagnostics', side_effect=diagnostics), \
             patch.object(s, '_cleanup', side_effect=cleanup) as clean, patch.object(s, '_sample_resources'), \
             patch.object(s, '_run_child', return_value=host_code) as host:
            code = s.run(['host-command'])
        receipt = json.loads((s.output_dir/'receipt.json').read_text())
        return code, receipt, identities, host.call_count, clean.call_count

    def timeout(self):
        error = redroid.RedroidError('command timed out: adb')
        error.__cause__ = subprocess.TimeoutExpired(['adb'], 1)
        return error

    def test_transient_boot_retry_uses_new_identity_and_runs_host_once(self):
        code, receipt, ids, host, clean = self.run_case([self.timeout(), None])
        self.assertEqual(code, 0)
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(host, 1)
        self.assertEqual(clean, 2)
        self.assertEqual([a['status'] for a in receipt['startup_attempts']], ['retryable_failure', 'ready'])

    def test_unverified_cleanup_prevents_retry_and_host_launch(self):
        code, receipt, ids, host, clean = self.run_case([self.timeout()], cleanup_failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(ids), 1)
        self.assertEqual(host, 0)
        self.assertIn('cleanup', receipt['failure'])

    def test_skipped_recovery_diagnostics_belong_to_failed_attempt(self):
        code, receipt, ids, host, clean = self.run_case([self.timeout(), None], diagnostic_skipped=True)
        self.assertEqual(code, 0)
        self.assertNotIn('diagnostics_skipped', receipt)
        self.assertIn('diagnostics_skipped', receipt['startup_attempts'][0])
        self.assertNotIn('diagnostics_skipped', receipt['startup_attempts'][1])

    def test_permanent_failure_and_host_failure_are_not_retried(self):
        code, receipt, ids, host, clean = self.run_case([redroid.RedroidError('invalid image policy')])
        self.assertEqual(code, 1)
        self.assertEqual(len(ids), 1)
        code, receipt, ids, host, clean = self.run_case([None], host_code=7)
        self.assertEqual(code, 7)
        self.assertEqual(host, 1)
        self.assertEqual(len(ids), 1)

    def test_attempts_exhausted_preserves_diagnostics(self):
        code, receipt, ids, host, clean = self.run_case([self.timeout(), self.timeout()])
        self.assertEqual(code, 1)
        self.assertEqual(host, 0)
        self.assertEqual(len(receipt['startup_attempts']), 2)

    def test_attempt_limit_and_dev_cli(self):
        for value in (0, 4):
            with self.assertRaises(redroid.RedroidError):
                redroid.RedroidSupervisor(output_dir=Path('/unused'), startup_attempts=value)
        args = dev_session.build_parser().parse_args(['start', '--host-command', 'ashnazg'])
        parsed = redroid.build_parser().parse_args(dev_session.redroid_command(args, '/tmp/out')[2:])
        self.assertEqual(parsed.startup_attempts, 2)

    def test_attempt_deadlines_reserve_time_without_extending_original_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            s = redroid.RedroidSupervisor(output_dir=Path(temp), timeout=10, startup_attempts=2)
            seen = []
            def ready():
                seen.append(s.deadline)
                if len(seen) == 1: raise self.timeout()
            started = redroid.time.monotonic()
            with patch.object(s, '_preflight'), patch.object(s, '_launch_container'), \
                 patch.object(s, '_wait_ready', side_effect=ready), patch.object(s, '_check_endpoint'), \
                 patch.object(s, '_cleanup'), patch.object(s, '_collect_diagnostics'), \
                 patch.object(s, '_sample_resources'), patch.object(s, '_run_child', return_value=0):
                self.assertEqual(s.run(['host']), 0)
            self.assertLess(seen[0], seen[1])
            self.assertLessEqual(seen[1], started + 10.1)

    def test_failed_attempt_diagnostic_write_does_not_hide_original_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            s = redroid.RedroidSupervisor(output_dir=Path(temp), timeout=10, startup_attempts=1)
            s.deadline = redroid.time.monotonic() + 10
            s.receipt['setup_timings_seconds'] = {}
            original = self.timeout()
            with patch.object(s, '_launch_container', side_effect=original), \
                 patch.object(redroid, '_atomic_json', side_effect=OSError('disk full')):
                with self.assertRaises(redroid.RedroidError) as caught:
                    s._boot_with_recovery()
            self.assertIs(caught.exception, original)
            self.assertIn('attempt_record_error', s.receipt)

    def test_intermediate_diagnostics_reserve_cleanup_and_second_attempt_time(self):
        with tempfile.TemporaryDirectory() as temp:
            s = redroid.RedroidSupervisor(output_dir=Path(temp), timeout=120, startup_attempts=2)
            s.deadline = 120.0
            s.receipt['setup_timings_seconds'] = {}
            clock = [0.0]
            launches = []
            budgets = []
            def launch():
                launches.append(clock[0])
                s._remaining()
            def ready():
                if len(launches) == 1:
                    clock[0] = s.deadline
                    raise self.timeout()
            def diagnostics(budget_seconds=45.0):
                budgets.append(budget_seconds)
                clock[0] += budget_seconds
            def cleanup():
                clock[0] += 25.0
            with patch.object(redroid.time, 'monotonic', side_effect=lambda: clock[0]), \
                 patch.object(redroid.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                 patch.object(s, '_launch_container', side_effect=launch), \
                 patch.object(s, '_wait_ready', side_effect=ready), \
                 patch.object(s, '_collect_diagnostics', side_effect=diagnostics), \
                 patch.object(s, '_cleanup', side_effect=cleanup), patch.object(s, '_check_endpoint'):
                s._boot_with_recovery()
            self.assertEqual(len(launches), 2)
            self.assertLessEqual(budgets[0], 5.0)
            self.assertGreaterEqual(s.deadline - launches[1], 29.0)
            self.assertEqual(s.deadline, 120.0)

    def test_reclaimed_port_with_time_wait_can_restart_but_live_listener_is_refused(self):
        import socket
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            listener.listen()
            with self.assertRaises(redroid.RedroidError):
                redroid.check_port_available(port)
            with socket.create_connection(('127.0.0.1', port)) as client:
                accepted, _ = listener.accept()
                accepted.close() # Server actively closes: leaves its port in TIME_WAIT.
                self.assertEqual(client.recv(1), b'')
        redroid.check_port_available(port)
