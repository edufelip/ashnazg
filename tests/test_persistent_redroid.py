import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import redroid, dev_session


class PersistentRedroidTests(unittest.TestCase):
    def supervisor(self, temp):
        s = redroid.RedroidSupervisor(output_dir=Path(temp) / 'output',
                                      state_dir=Path(temp) / 'state')
        s.architecture = 'amd64'
        s.docker_path = 'docker'
        s.deadline = redroid.time.monotonic() + 30
        return s

    def test_persistent_launch_and_cleanup_retain_data(self):
        with tempfile.TemporaryDirectory() as temp:
            s = self.supervisor(temp)
            with patch.object(s, '_command', return_value=redroid.CommandResult(0, '')) as command:
                s._prepare_storage()
                s._launch_container()
                launch = command.call_args.args[0]
                self.assertIn('--mount', launch)
                self.assertIn('target=/data', launch[launch.index('--mount') + 1])
                with patch.object(s, '_owned_container', return_value=True):
                    s._cleanup()
                self.assertNotIn('--volumes', command.call_args.args[0])
            s.storage.close()
            self.assertTrue((Path(temp) / 'state/data').is_dir())

    def test_orphan_cleanup_requires_matching_data_identity_and_mount(self):
        with tempfile.TemporaryDirectory() as temp:
            s = self.supervisor(temp)
            with patch.object(s, '_command', return_value=redroid.CommandResult(0, '')):
                s._prepare_storage()
            storage = s.storage
            storage.close()
            labels = {'org.ashnazg.redroid.data-id': storage.identity,
                      'org.ashnazg.redroid.run-id': 'old-run'}
            inspect = [{'Id': 'a'*64, 'Config': {'Labels': labels},
                        'Mounts': [{'Source': str(storage.data), 'Destination': '/data', 'Type': 'bind'}]}]
            for owned in (False, True):
                if not owned:
                    labels['org.ashnazg.redroid.data-id'] = 'other'
                else:
                    labels['org.ashnazg.redroid.data-id'] = storage.identity
                with patch.object(s, '_command', side_effect=[redroid.CommandResult(0, 'a'*64),
                        redroid.CommandResult(0, json.dumps(inspect)), redroid.CommandResult(0, '')]) as command:
                    if owned:
                        s._prepare_storage()
                        self.assertEqual(command.call_args.args[0], ['docker', 'rm', '--force', 'a'*64])
                    else:
                        with self.assertRaisesRegex(redroid.RedroidError, 'ownership'):
                            s._prepare_storage()
                        self.assertEqual(command.call_count, 2)
                if s.storage:
                    s.storage.close()

    def test_dev_command_passes_explicit_storage_and_60hz(self):
        args = dev_session.build_parser().parse_args(['start', '--state-dir', '/tmp/my-session',
                                                     '--host-command', 'ashnazg', '--', 'mcp'])
        command = dev_session.redroid_command(args, '/tmp/output')
        parsed = redroid.build_parser().parse_args(command[2:])
        self.assertEqual(parsed.state_dir, Path('/tmp/my-session'))
        self.assertEqual(parsed.fps, 60)

    def test_failed_boot_releases_lock_and_records_failed_phase(self):
        with tempfile.TemporaryDirectory() as temp:
            s = self.supervisor(temp)
            with patch.object(s, '_preflight'), patch.object(s, '_check_endpoint'), patch.object(s, '_launch_container'), \
                 patch.object(s, '_wait_ready', side_effect=redroid.RedroidError('boot timed out')), \
                 patch.object(s, '_command', return_value=redroid.CommandResult(0, '')):
                self.assertEqual(s.run(['unused-command']), 1)
            receipt = json.loads((s.output_dir / 'receipt.json').read_text())
            self.assertEqual(receipt['failed_phase'], 'android_readiness')
            self.assertIsNone(s.storage.lock)
            with redroid.SessionData(s.state_dir, s.storage.contract):
                pass

    def test_persistent_preflight_does_not_reject_owned_orphan_port_before_fencing(self):
        with tempfile.TemporaryDirectory() as temp:
            s = redroid.RedroidSupervisor(output_dir=Path(temp)/'output', state_dir=Path(temp)/'state',
                    docker_path='docker', adb_path='adb', host_system='Linux', host_arch='x86_64',
                    binder_available=True)
            s.deadline = redroid.time.monotonic() + 30
            index = {'manifests': [{'platform': {'os': 'linux', 'architecture': 'amd64'},
                                    'digest': redroid.PLATFORM_DIGESTS['amd64']}]}
            def command(args, **kwargs):
                if args[1] == 'context': return redroid.CommandResult(0, 'unix:///docker.sock')
                return redroid.CommandResult(0, json.dumps(index))
            with patch.object(s, '_command', side_effect=command), patch.object(
                    redroid, 'check_port_available', side_effect=AssertionError('checked before fencing')):
                s._preflight()

    def test_recovered_guest_disconnects_only_its_published_loopback_adb(self):
        with tempfile.TemporaryDirectory() as temp:
            s = self.supervisor(temp)
            s.adb_path = 'adb'
            with patch.object(s, '_command', return_value=redroid.CommandResult(0, '')):
                s._prepare_storage()
            identity = s.storage.identity
            source = str(s.storage.data)
            s.storage.close()
            inspect = [{'Id': 'b'*64, 'Config': {'Labels': {
                'org.ashnazg.redroid.data-id': identity, 'org.ashnazg.redroid.run-id': 'old'}},
                'Mounts': [{'Source': source, 'Destination': '/data', 'Type': 'bind'}],
                'NetworkSettings': {'Ports': {'5555/tcp': [
                    {'HostIp': '127.0.0.1', 'HostPort': '15555'},
                    {'HostIp': '0.0.0.0', 'HostPort': '25555'}]}}}]
            try:
                with patch.object(s, '_command', side_effect=[redroid.CommandResult(0, 'b'*64),
                        redroid.CommandResult(0, json.dumps(inspect)), redroid.CommandResult(0, ''),
                        redroid.CommandResult(0, '')]) as command:
                    s._prepare_storage()
                    self.assertEqual(command.call_args.args[0], ['adb', 'disconnect', '127.0.0.1:15555'])
            finally:
                s.storage.close()

    def test_stopped_guest_uses_retained_port_bindings_to_disconnect_stale_adb(self):
        with tempfile.TemporaryDirectory() as temp:
            s = self.supervisor(temp)
            s.adb_path = 'adb'
            with patch.object(s, '_command', return_value=redroid.CommandResult(0, '')):
                s._prepare_storage()
            identity = s.storage.identity
            source = str(s.storage.data)
            s.storage.close()
            inspect = [{'Id': 'b'*64, 'Config': {'Labels': {
                'org.ashnazg.redroid.data-id': identity, 'org.ashnazg.redroid.run-id': 'old'}},
                'Mounts': [{'Source': source, 'Destination': '/data', 'Type': 'bind'}],
                'HostConfig': {'PortBindings': {'5555/tcp': [
                    {'HostIp': '127.0.0.1', 'HostPort': '15555'},
                    {'HostIp': '0.0.0.0', 'HostPort': '25555'}]}}}]
            try:
                with patch.object(s, '_command', side_effect=[redroid.CommandResult(0, 'b'*64),
                        redroid.CommandResult(0, json.dumps(inspect)), redroid.CommandResult(0, ''),
                        redroid.CommandResult(0, '')]) as command:
                    s._prepare_storage()
                    self.assertEqual(command.call_args.args[0], ['adb', 'disconnect', '127.0.0.1:15555'])
            finally:
                s.storage.close()
