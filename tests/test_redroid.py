import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import redroid


class RedroidValidationTests(unittest.TestCase):
    def test_rejects_unsupported_os_and_architecture(self):
        with self.assertRaisesRegex(redroid.RedroidError, "Linux"):
            redroid.validate_host("Darwin", "x86_64", True)
        with self.assertRaisesRegex(redroid.RedroidError, "architecture"):
            redroid.validate_host("Linux", "s390x", True)

    def test_regular_file_cannot_satisfy_binder_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            binder = Path(temp) / "binder"
            binder.touch()
            supervisor = redroid.RedroidSupervisor(output_dir=Path(temp), host_system="Linux",
                host_arch="x86_64", binder_path=binder, docker_path="docker", adb_path="adb")
            supervisor.deadline = redroid.time.monotonic() + 10
            with patch.object(supervisor, "_command") as command:
                with self.assertRaisesRegex(redroid.RedroidError, "Binder"):
                    supervisor._preflight()
                command.assert_not_called()

    def test_requires_binder_device_without_loading_modules(self):
        with self.assertRaisesRegex(redroid.RedroidError, "Binder"):
            redroid.validate_host("Linux", "x86_64", False)

    def test_digest_check_requires_index_and_expected_platform_manifest(self):
        index = "sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad"
        wanted = redroid.PLATFORM_DIGESTS["amd64"]
        raw = json.dumps({"manifests": [
            {"digest": wanted, "platform": {"os": "linux", "architecture": "amd64"}}
        ]})
        self.assertEqual(redroid.verify_image_index(raw, index, "amd64"), wanted)
        for malformed in (raw.replace(wanted, "sha256:" + "0" * 64), "{}"):
            with self.assertRaises(redroid.RedroidError):
                redroid.verify_image_index(malformed, index, "amd64")
        with self.assertRaises(redroid.RedroidError):
            redroid.verify_image_index(raw, "sha256:" + "0" * 64, "amd64")

    def test_port_check_binds_loopback_only_and_reports_busy_port(self):
        with patch.object(redroid.socket, "socket") as create_socket:
            sock = create_socket.return_value.__enter__.return_value
            redroid.check_port_available(5555)
            sock.bind.assert_called_once_with(("127.0.0.1", 5555))
            sock.bind.side_effect = OSError("busy")
            with self.assertRaisesRegex(redroid.RedroidError, "5555"):
                redroid.check_port_available(5555)


class RedroidLifecycleTests(unittest.TestCase):
    def test_anr_during_boot_fails_closed(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(list(args))
            if args[-2:] == ["connect", "127.0.0.1:5555"]:
                return redroid.CommandResult(0, "connected", "")
            if args[-1:] == ["get-state"]:
                return redroid.CommandResult(0, "device", "")
            if args[-2:] == ["getprop", "sys.boot_completed"]:
                return redroid.CommandResult(0, "1", "")
            if args[-2:] == ["activity", "lastanr"]:
                return redroid.CommandResult(0, "ANR time: Nov 15", "")
            return redroid.CommandResult(0, "", "")

        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(
                output_dir=Path(temp), adb_port=5555, timeout=1, runner=runner,
                host_system="Linux", host_arch="x86_64", binder_available=True,
                docker_path="docker", adb_path="adb",
            )
            supervisor.deadline = redroid.time.monotonic() + 1
            with self.assertRaisesRegex(redroid.RedroidError, "ANR"):
                supervisor._wait_ready()
        self.assertFalse(any("input" in command for command in calls))

    def test_child_receives_explicit_serial_and_preserves_exit_code(self):
        seen = {}

        class Child:
            def __init__(self, command, **kwargs):
                seen.update(kwargs)
                self.pid = 987654321

            def wait(self, timeout=None):
                return 23

            def poll(self):
                return 23

        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(
                output_dir=Path(temp), adb_port=5555, timeout=10,
            )
            supervisor.deadline = redroid.time.monotonic() + 10
            with patch.object(redroid.subprocess, "Popen", Child), patch.object(redroid, "terminate_process_group") as cleanup:
                self.assertEqual(supervisor._run_child(["some-command", "arg with spaces"]), 23)
                cleanup.assert_called_once()
        self.assertEqual(seen["env"]["ANDROID_SERIAL"], "127.0.0.1:5555")
        self.assertNotIn("stdout", seen)

    def test_cleanup_refuses_container_with_foreign_ownership_label(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(list(args))
            return redroid.CommandResult(0, "different-run-id", "")

        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(
                output_dir=Path(temp), adb_port=5555, timeout=5, runner=runner,
                docker_path="docker",
            )
            supervisor.container_started = True
            supervisor._cleanup()
        self.assertEqual(calls[0][1], "inspect")
        self.assertEqual(len(calls), 1)
        self.assertIn("cleanup_refused", supervisor.receipt)

    def test_cli_passes_command_arguments_through_without_shell(self):
        calls = []

        class FakeSupervisor:
            def __init__(self, **kwargs):
                calls.append(("init", kwargs))

            def run(self, command):
                calls.append(("run", command))
                return 17

        with patch.object(redroid, "RedroidSupervisor", FakeSupervisor):
            code = redroid.main(["run", "--output-dir", "out", "--adb-port", "5556",
                                 "--timeout", "180", "--", "python", "-c", "print('x y')"])
        self.assertEqual(code, 17)
        self.assertEqual(calls[1][1], ["python", "-c", "print('x y')"])
        self.assertEqual(calls[0][1]["adb_port"], 5556)

    def test_startup_failure_cleans_only_owned_container(self):
        commands = []

        def runner(args, **kwargs):
            commands.append(list(args))
            cmd = args[1:]
            if cmd[:2] == ["context", "inspect"]:
                return redroid.CommandResult(0, "unix:///var/run/docker.sock", "")
            if cmd[:3] == ["buildx", "imagetools", "inspect"]:
                return redroid.CommandResult(0, json.dumps({"manifests": [
                    {"digest": redroid.PLATFORM_DIGESTS["amd64"],
                     "platform": {"os": "linux", "architecture": "amd64"}}
                ]}), "")
            if cmd[:1] == ["run"]:
                return redroid.CommandResult(0, "owned-container-id", "")
            if cmd[:1] == ["inspect"]:
                return redroid.CommandResult(0, cmd[-1].removeprefix("ashnazg-redroid-"), "")
            return redroid.CommandResult(0, "", "")

        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(
                output_dir=Path(temp), adb_port=5555, timeout=1, runner=runner,
                host_system="Linux", host_arch="x86_64", binder_path=Path("/dev/binder"),
                docker_path="/usr/bin/docker", adb_path="/usr/bin/adb", binder_available=True,
            )
            with patch.object(redroid, "check_port_available"):
                self.assertEqual(supervisor.run(["true"]), 1)
        run = next(command for command in commands if command[1:2] == ["run"])
        cleanup = next(command for command in commands if command[1:2] == ["rm"])
        self.assertIn("--label", run)
        self.assertIn("androidboot.use_memfd=true", run)
        self.assertIn("androidboot.redroid_gpu_mode=guest", run)
        self.assertNotIn("ro.config.ashmem=memfd", run)
        self.assertEqual(cleanup[-1], run[run.index("--name") + 1])
        self.assertNotIn("--all", cleanup)

    def test_busy_adb_serial_fails_before_container_creation(self):
        commands = []

        def runner(args, **kwargs):
            commands.append(list(args))
            if args[1:3] == ["context", "inspect"]:
                return redroid.CommandResult(0, "unix:///var/run/docker.sock", "")
            if args[1:2] == ["devices"]:
                return redroid.CommandResult(0, "List of devices attached\n127.0.0.1:5555\tdevice\n", "")
            return redroid.CommandResult(0, "", "")

        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(
                output_dir=Path(temp), adb_port=5555, timeout=2, runner=runner,
                host_system="Linux", host_arch="x86_64", binder_path=Path("/dev/binder"),
                docker_path="/usr/bin/docker", adb_path="/usr/bin/adb", binder_available=True,
            )
            with patch.object(redroid, "check_port_available"):
                self.assertEqual(supervisor.run(["true"]), 1)
            receipt = json.loads((Path(temp) / "receipt.json").read_text())
            self.assertIn("ADB serial", receipt["failure"])
        self.assertFalse(any(command[1:2] == ["run"] for command in commands))

    @patch.dict(os.environ, {"ASHNAZG_CI_DIAGNOSTICS": "1"})
    def test_readiness_failure_records_bounded_diagnostics_and_cleans_container(self):
        commands = []

        def runner(args, **kwargs):
            commands.append(list(args))
            if args[1:3] == ["context", "inspect"]:
                return redroid.CommandResult(0, "unix:///var/run/docker.sock", "")
            if args[1:4] == ["buildx", "imagetools", "inspect"]:
                return redroid.CommandResult(0, json.dumps({"manifests": [
                    {"digest": redroid.PLATFORM_DIGESTS["amd64"],
                     "platform": {"os": "linux", "architecture": "amd64"}}
                ]}), "")
            if args[1:2] == ["devices"]:
                return redroid.CommandResult(0, "List of devices attached\n", "")
            if args[1:2] == ["connect"]:
                return redroid.CommandResult(0, "connected", "")
            if args[1:2] == ["-s"]:
                return redroid.CommandResult(1, "", "device offline")
            if args[1:2] == ["inspect"]:
                return redroid.CommandResult(0, args[-1].removeprefix("ashnazg-redroid-"), "")
            return redroid.CommandResult(0, "diagnostic", "")

        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(
                output_dir=Path(temp), adb_port=5555, timeout=0.5, runner=runner,
                host_system="Linux", host_arch="x86_64", binder_path=Path("/dev/binder"),
                docker_path="/usr/bin/docker", adb_path="/usr/bin/adb", binder_available=True,
            )
            with patch.object(redroid, "check_port_available"):
                self.assertEqual(supervisor.run(["true"]), 1)
            receipt = json.loads((Path(temp) / "receipt.json").read_text())
            self.assertNotEqual(receipt["status"], "success")
            self.assertIn("failure", receipt)
            self.assertIn("diagnostics", receipt)
        self.assertTrue(any(command[1:2] == ["rm"] for command in commands))

class RedroidRegressionTests(unittest.TestCase):
    def test_direct_cli_entrypoint_imports_without_pythonpath(self):
        result = subprocess.run([sys.executable, str(Path(redroid.__file__)), '--help'],
                                cwd=tempfile.gettempdir(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_ownership_probe_never_removes_container(self):
        commands = []
        def runner(args, **kwargs):
            commands.append(args)
            raise subprocess.TimeoutExpired(args, 1)
        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(output_dir=Path(temp), runner=runner)
            supervisor.container_started = True
            supervisor._cleanup()
            self.assertIn('cleanup_refused', supervisor.receipt)
        self.assertFalse(any('rm' in command for command in commands))

    def test_docker_client_timeout_still_checks_owned_container_for_cleanup(self):
        commands = []
        def runner(args, **kwargs):
            commands.append(args)
            if args[1] == 'run':
                raise subprocess.TimeoutExpired(args, 1)
            if args[1] == 'inspect':
                return redroid.CommandResult(0, supervisor.run_id)
            return redroid.CommandResult(0)
        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(output_dir=Path(temp), runner=runner, docker_path='docker')
            supervisor.architecture = 'amd64'
            supervisor.deadline = redroid.time.monotonic() + 10
            with self.assertRaises(redroid.RedroidError):
                supervisor._launch_container()
            supervisor._cleanup()
        self.assertTrue(any(command[1:2] == ['rm'] and '--volumes' in command for command in commands))

    def test_command_timeout_is_independent_of_expired_boot_deadline(self):
        waits = []
        class Child:
            pid = 987654321
            def __init__(self, *args, **kwargs): pass
            def wait(self, timeout=None):
                waits.append(timeout)
                return 0
        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(output_dir=Path(temp), command_timeout=1200)
            supervisor.deadline = redroid.time.monotonic() - 1
            with patch.object(redroid.subprocess, 'Popen', Child), patch.object(redroid, 'terminate_process_group') as reap:
                self.assertEqual(supervisor._run_child(['true']), 0)
                reap.assert_called_once()
        self.assertEqual(waits, [1200])

    @patch.dict(os.environ, {'ASHNAZG_CI_DIAGNOSTICS': '0'})
    def test_local_failures_do_not_collect_device_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(output_dir=Path(temp))
            supervisor.container_started = True
            with patch.object(supervisor, '_command') as command:
                supervisor._collect_diagnostics()
                command.assert_not_called()

    def test_structured_window_output_is_not_silently_truncated(self):
        content = 'Window #0 focused application\n' + ('x' * 100_000)
        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(output_dir=Path(temp),
                runner=lambda *a, **kw: redroid.CommandResult(0, content))
            supervisor.deadline = redroid.time.monotonic() + 10
            self.assertEqual(supervisor._command(['dumpsys']).stdout, content)

    def test_remote_docker_is_refused_before_container_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(output_dir=Path(temp), host_system='Linux',
                host_arch='x86_64', binder_available=True, docker_path='docker', adb_path='adb',
                runner=lambda *a, **kw: redroid.CommandResult(0, 'ssh://other-host'))
            supervisor.deadline = redroid.time.monotonic() + 10
            with patch.dict(os.environ, {'DOCKER_HOST': ''}), self.assertRaisesRegex(redroid.RedroidError, 'local'):
                supervisor._preflight()

    def test_owned_adb_connection_and_anonymous_data_are_cleaned(self):
        commands = []
        def runner(args, **kwargs):
            commands.append(args)
            return redroid.CommandResult(0, supervisor.run_id if args[1] == 'inspect' else '')
        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(output_dir=Path(temp), runner=runner)
            supervisor.container_started = True
            supervisor.adb_connected = True
            supervisor._cleanup()
        self.assertTrue(any(command[1:] == ['disconnect', supervisor.serial] for command in commands))
        self.assertTrue(any(command[1:] == ['rm', '--force', '--volumes', supervisor.container_name] for command in commands))

    def test_failed_cleanup_cannot_report_a_successful_run(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(output_dir=Path(temp))
            def cleanup(): supervisor.receipt['cleanup_error'] = 'Docker removal failed'
            with patch.object(supervisor, '_preflight'), patch.object(supervisor, '_launch_container'), \
                 patch.object(supervisor, '_wait_ready'), patch.object(supervisor, '_sample_resources'), \
                 patch.object(supervisor, '_run_child', return_value=0), patch.object(supervisor, '_cleanup', cleanup):
                self.assertEqual(supervisor.run(['true']), 1)
            self.assertEqual(json.loads((Path(temp)/'receipt.json').read_text())['status'], 'cleanup_failure')


if __name__ == "__main__":
    unittest.main()
