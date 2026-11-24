import hashlib
import errno
import importlib.util
from unittest.mock import patch, Mock, mock_open
import json
import re
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from runtime.microvm.guest_bridge import (
    ConnectionLimiter,
    proxy_connection,
    serve_control_request,
)
from runtime.microvm.manifest import EXPECTED_GUEST, ManifestError, validate_manifest
from scripts import redroid


class GuestImageContractTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parents[1]
        spec = importlib.util.spec_from_file_location('guest_init_contract', root / 'runtime/microvm/guest-init.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        profile = module.SUPERVISOR[module.SUPERVISOR.index('--image-profile') + 1]
        self.runtime_index = redroid.IMAGE_PROFILES[profile]['index']

    def test_runtime_profile_and_bundle_contract_declare_the_same_image(self):
        self.assertEqual(EXPECTED_GUEST['redroid_index'], self.runtime_index)

    def test_builder_records_the_runtime_image_index_in_bundle_provenance(self):
        root = Path(__file__).parents[1]
        build = (root / 'scripts/build_microvm.sh').read_text()
        pin = re.search(r'^readonly REDROID_INDEX="([^"]+)"$', build, re.M)
        self.assertIsNotNone(pin)
        self.assertEqual(pin.group(1), self.runtime_index)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.assets = Path(self.temporary.name)
        (self.assets / "kernel").write_bytes(b"kernel-image")
        (self.assets / "rootfs.ext4").write_bytes(b"ext4-image")
        self.manifest = {
            "schema_version": 1,
            "version": "0.3.1",
            "source_sha": "a" * 40,
            "architecture": "aarch64",
            "files": {
                name: {
                    "sha256": hashlib.sha256((self.assets / name).read_bytes()).hexdigest(),
                    "size": (self.assets / name).stat().st_size,
                }
                for name in ("kernel", "rootfs.ext4")
            },
            "guest": {
                "kernel_version": "6.12.117",
                "base": "alpine-3.22.8-aarch64",
                "redroid_index": "sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee",
            },
        }
        (self.assets / "manifest.json").write_text(json.dumps(self.manifest))

    def tearDown(self):
        self.temporary.cleanup()

    def test_accepts_manifest_when_fixed_artifacts_match(self):
        validated = validate_manifest(self.assets, expected_architecture="aarch64")
        self.assertEqual(validated["guest"]["kernel_version"], "6.12.117")

    def test_rejects_superseded_mixed_bitness_image_pin(self):
        self.manifest['guest']['redroid_index'] = redroid.IMAGE_INDEX
        (self.assets / 'manifest.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ManifestError, 'pinned ReDroid index'):
            validate_manifest(self.assets, expected_architecture='aarch64')

    def test_rejects_digest_or_size_mismatch(self):
        (self.assets / "kernel").write_bytes(b"tampered")
        with self.assertRaisesRegex(ManifestError, "(digest|size)"):
            validate_manifest(self.assets, expected_architecture="aarch64")

    def test_rejects_symlinked_artifact(self):
        (self.assets / "kernel").unlink()
        target = self.assets / "outside"
        target.write_bytes(b"kernel-image")
        (self.assets / "kernel").symlink_to(target)
        with self.assertRaisesRegex(ManifestError, "symlink"):
            validate_manifest(self.assets, expected_architecture="aarch64")

    def test_rejects_wrong_architecture_and_non_commit_source(self):
        with self.assertRaisesRegex(ManifestError, "architecture"):
            validate_manifest(self.assets, expected_architecture="x86_64")
        self.manifest["source_sha"] = "not-a-commit"
        (self.assets / "manifest.json").write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ManifestError, "source_sha"):
            validate_manifest(self.assets, expected_architecture="aarch64")

    def test_rejects_missing_artifact_and_path_outside_fixed_names(self):
        (self.assets / "kernel").unlink()
        with self.assertRaisesRegex(ManifestError, "missing artifact kernel"):
            validate_manifest(self.assets, expected_architecture="aarch64")
        self.manifest["files"]["../kernel"] = self.manifest["files"].pop("kernel")
        (self.assets / "manifest.json").write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ManifestError, "exactly kernel"):
            validate_manifest(self.assets, expected_architecture="aarch64")


class GuestBridgeTests(unittest.TestCase):
    def request(self, payload, *, ready=True, limit=1024, on_shutdown=None):
        server, client = socket.socketpair()
        worker = threading.Thread(
            target=serve_control_request,
            args=(server,),
            kwargs={"is_ready": lambda: ready, "max_request_bytes": limit, "on_shutdown": on_shutdown},
        )
        worker.start()
        client.sendall(payload)
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError as exc:
            # Invalid frames may be rejected and closed before this half-close.
            if exc.errno != errno.ENOTCONN:
                raise
        try:
            response = client.recv(2048)
        except ConnectionResetError:
            # Closing an oversized frame with unread bytes is a reset on Linux.
            # Both reset and EOF reject the request without returning a response.
            response = b''
        client.close()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        server.close()
        return response

    def test_status_returns_contract_only_after_supervisor_readiness(self):
        response = self.request(b'{"method":"status"}\n')
        self.assertEqual(json.loads(response), {
            "schema_version": 1,
            "status": "ready",
            "android_serial": "127.0.0.1:5555",
        })

    def test_shutdown_is_acknowledged_and_calls_only_the_supplied_owner(self):
        requests = []
        response = self.request(b'{"method":"shutdown"}\n', ready=False,
                                on_shutdown=lambda: requests.append('stop'))
        self.assertEqual(json.loads(response), {"schema_version": 1, "status": "stopping"})
        self.assertEqual(requests, ['stop'])
        self.assertEqual(self.request(b'{"method":"shutdown"}\n'), b'')

    def test_status_is_suppressed_until_supervisor_is_ready(self):
        self.assertEqual(self.request(b'{"method":"status"}\n', ready=False), b"")

    def test_rejects_malformed_or_oversized_control_frame(self):
        for payload in (b"not-json\n", b'{"method":"exec"}\n', b"x" * 32,
                        b'{"method":"status"}'):
            with self.subTest(payload=payload[:20]):
                self.assertEqual(self.request(payload, limit=24), b"")

    def test_connection_limiter_never_exceeds_capacity(self):
        limiter = ConnectionLimiter(2)
        self.assertTrue(limiter.acquire())
        self.assertTrue(limiter.acquire())
        self.assertFalse(limiter.acquire())
        limiter.release()
        self.assertTrue(limiter.acquire())
        limiter.release()
        limiter.release()
        with self.assertRaises(ValueError):
            limiter.release()

    def test_raw_proxy_relays_bytes_and_closes_client_on_connect_error(self):
        left, proxy_side = socket.socketpair()
        right, peer = socket.socketpair()
        worker = threading.Thread(target=proxy_connection, args=(proxy_side, lambda: right))
        worker.start()
        left.sendall(b"adb-request")
        self.assertEqual(peer.recv(32), b"adb-request")
        peer.sendall(b"adb-response")
        self.assertEqual(left.recv(32), b"adb-response")
        left.close()
        peer.close()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

        failed_client, server = socket.socketpair()
        proxy_connection(failed_client, lambda: (_ for _ in ()).throw(OSError("unavailable")))
        self.assertEqual(server.recv(1), b"")
        server.close()

    def test_raw_proxy_drains_request_before_propagating_half_close(self):
        client, proxy_side = socket.socketpair()
        upstream, peer = socket.socketpair()
        client.settimeout(2)
        peer.settimeout(2)
        worker = threading.Thread(target=proxy_connection, args=(proxy_side, lambda: upstream))
        worker.start()
        client.sendall(b"adb-request")
        client.shutdown(socket.SHUT_WR)
        self.assertEqual(peer.recv(32), b"adb-request")
        self.assertEqual(peer.recv(1), b"")
        peer.sendall(b"adb-response")
        self.assertEqual(client.recv(32), b"adb-response")
        client.close()
        peer.close()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())


class GuestInitTests(unittest.TestCase):
    def test_diagnostics_kernel_token_is_exact_and_not_a_substring(self):
        path = Path(__file__).parents[1] / 'runtime/microvm/guest-init.py'
        spec = importlib.util.spec_from_file_location('ashnazg_guest_diagnostic_token', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.assertTrue(module.diagnostics_enabled('console=hvc0 ashnazg.diagnostics=1 root=/dev/vda'))
        for cmdline in ('ashnazg.diagnostics=10', 'xashnazg.diagnostics=1',
                        'ashnazg.diagnostics=1x', 'ashnazg.diagnostics=0'):
            self.assertFalse(module.diagnostics_enabled(cmdline))
        opted = module.supervisor_environment('ashnazg.diagnostics=1', {'PATH': '/bin'})
        default = module.supervisor_environment('ashnazg.diagnostics=10', {
            'PATH': '/bin', 'ASHNAZG_CI_DIAGNOSTICS': '1'})
        self.assertEqual(opted['ASHNAZG_CI_DIAGNOSTICS'], '1')
        self.assertNotIn('ASHNAZG_CI_DIAGNOSTICS', default)

    def test_supervisor_selects_explicit_native_guest_compatible_image_profile(self):
        path = Path(__file__).parents[1] / 'runtime/microvm/guest-init.py'
        spec = importlib.util.spec_from_file_location('ashnazg_guest_redroid_profile', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        index = module.SUPERVISOR.index('--image-profile')
        self.assertEqual(module.SUPERVISOR[index + 1], 'android15-64only')

    def test_interfaces_are_brought_up_before_dhcp_starts(self):
        path = Path(__file__).parents[1] / 'runtime/microvm/guest-init.py'
        spec = importlib.util.spec_from_file_location('ashnazg_guest_network_init', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        commands = []
        def run_command(args, **kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0)
        def start_dhcp(args, **kwargs):
            self.assertEqual(args[:3], ['/sbin/udhcpc', '-i', 'eth0'])
            self.assertIn(['/sbin/ip', 'link', 'set', 'dev', 'lo', 'up'], commands)
            self.assertIn(['/sbin/ip', 'link', 'set', 'dev', 'eth0', 'up'], commands)
            raise RuntimeError('stop after checked DHCP start')
        with patch.object(module.os, 'getpid', return_value=1), \
             patch.dict(module.os.environ, {}), \
             patch.object(module, 'kernel_cmdline', return_value=''), \
             patch.object(module, 'mount'), patch.object(module, 'ensure_nodes'), \
             patch.object(module, 'link_nodes'), patch.object(module.os, 'makedirs'), \
             patch.object(module.signal, 'signal'), \
             patch.object(module.subprocess, 'run', side_effect=run_command), \
             patch.object(module.subprocess, 'Popen', side_effect=start_dhcp):
            with self.assertRaisesRegex(RuntimeError, 'stop after checked DHCP start'):
                module.main()

    def test_pid1_supplies_executable_search_paths_to_services(self):
        path = Path(__file__).parents[1] / 'runtime/microvm/guest-init.py'
        spec = importlib.util.spec_from_file_location('ashnazg_guest_environment_init', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        def start_child(args, **kwargs):
            search = module.os.environ.get('PATH', '').split(':')
            self.assertIn('/usr/bin', search)
            self.assertIn('/sbin', search)
            raise RuntimeError('stop after checked service environment')
        with patch.object(module.os, 'getpid', return_value=1), \
             patch.dict(module.os.environ, {'PATH': ''}), \
             patch.object(module, 'kernel_cmdline', return_value=''), \
             patch.object(module, 'mount'), patch.object(module, 'ensure_nodes'), \
             patch.object(module, 'link_nodes'), patch.object(module.os, 'makedirs'), \
             patch.object(module.signal, 'signal'), patch.object(module.subprocess, 'run'), \
             patch.object(module.subprocess, 'Popen', side_effect=start_child):
            with self.assertRaisesRegex(RuntimeError, 'stop after checked service environment'):
                module.main()

    def test_docker_starts_with_the_locked_alpine_proxy_binary(self):
        path = Path(__file__).parents[1] / 'runtime/microvm/guest-init.py'
        spec = importlib.util.spec_from_file_location('ashnazg_guest_docker_init', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        network = Mock(); network.wait.return_value = 0; network.poll.return_value = 0
        def start_child(args, **kwargs):
            if args[0] == '/sbin/udhcpc': return network
            self.assertEqual(args[0], '/usr/bin/dockerd')
            self.assertIn('--userland-proxy-path=/usr/bin/docker-proxy', args)
            raise RuntimeError('stop after checked Docker start')
        with patch.object(module.os, 'getpid', return_value=1), \
             patch.dict(module.os.environ, {}), \
             patch.object(module, 'kernel_cmdline', return_value=''), \
             patch.object(module, 'mount'), patch.object(module, 'ensure_nodes'), \
             patch.object(module, 'link_nodes'), patch.object(module.os, 'makedirs'), \
             patch.object(module.signal, 'signal'), patch('builtins.open', mock_open()), \
             patch.object(module.subprocess, 'run'), \
             patch.object(module.subprocess, 'Popen', side_effect=start_child):
            with self.assertRaisesRegex(RuntimeError, 'stop after checked Docker start'):
                module.main()

    def test_startup_log_tail_is_bounded(self):
        path = Path(__file__).parents[1] / 'runtime/microvm/guest-init.py'
        spec = importlib.util.spec_from_file_location('ashnazg_guest_boot_log', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'dockerd.log'
            log.write_bytes(b'x' * 10000 + b'fatal startup error')
            tail = module.boot_log_tail(str(log), 32)
            self.assertLessEqual(len(tail.encode()), 32)
            self.assertTrue(tail.endswith('fatal startup error'))
            self.assertEqual(module.boot_log_tail(str(log.with_name('absent'))), 'startup log unavailable')

    def test_startup_failure_powers_off_instead_of_exiting_pid_one(self):
        path = Path(__file__).parents[1] / 'runtime/microvm/guest-init.py'
        spec = importlib.util.spec_from_file_location('ashnazg_guest_init', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with patch.object(module.os, 'getpid', return_value=1), \
             patch.object(module, 'main', side_effect=RuntimeError('boot failed')), \
             patch.object(module, 'poweroff', side_effect=SystemExit(0)) as off:
            with self.assertRaises(SystemExit): module.run_init()
            off.assert_called_once()


class KernelConfigTests(unittest.TestCase):
    def test_required_guest_features_are_built_in_without_ashmem(self):
        config = set((Path(__file__).parents[1] / "runtime/microvm/kernel.config").read_text().splitlines())
        required = {
            "CONFIG_ANDROID_BINDER_IPC=y",
            "CONFIG_ANDROID_BINDERFS=y",
            'CONFIG_ANDROID_BINDER_DEVICES="binder,hwbinder,vndbinder"',
            "CONFIG_MEMFD_CREATE=y",
            "CONFIG_PSI=y",
            "CONFIG_PSI_DEFAULT_DISABLED=n",
            "CONFIG_FUSE_FS=y",
            "CONFIG_VIRTIO_PCI=y",
            "CONFIG_VIRTIO_BLK=y",
            "CONFIG_VIRTIO_NET=y",
            "CONFIG_VIRTIO_CONSOLE=y",
            "CONFIG_VSOCKETS=y",
            "CONFIG_VIRTIO_VSOCKETS=y",
            "CONFIG_EXT4_FS=y",
            "CONFIG_OVERLAY_FS=y",
            "CONFIG_SECCOMP=y",
            "CONFIG_SECCOMP_FILTER=y",
            "CONFIG_NF_NAT=y",
            "CONFIG_NF_TABLES=y",
            "CONFIG_NFT_COMPAT=y",
            "CONFIG_NETFILTER_XT_MATCH_ADDRTYPE=y",
            "CONFIG_NETFILTER_XT_MATCH_CONNTRACK=y",
            "CONFIG_IP_NF_TARGET_MASQUERADE=y",
        }
        self.assertTrue(required.issubset(config), f"missing kernel options: {sorted(required - config)}")
        self.assertFalse(any("ASHMEM" in option for option in config))


if __name__ == "__main__":
    unittest.main()
