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
    def test_local_image_provenance_requires_matching_clean_checkout_and_inspect(self):
        descriptor = {
            "schema": 1, "recipe_version": "0.2.0", "source_sha": "a" * 40,
            "source_dirty": False,
            "upstream_base": {"image": redroid.IMAGE, "index_digest": redroid.IMAGE_INDEX,
                              "platform_digest": redroid.PLATFORM_DIGESTS["amd64"]},
            "platform": "linux/amd64",
            "apk_inputs": {"agent": {"sha256": "b" * 64}, "fixture": {"sha256": "c" * 64}},
            "notices": "runtime/redroid-image/NOTICES.md",
            "qualification": "recipe build only",
            "image_inspect": {"id": "sha256:" + "d" * 64, "repo_tags": ["ashnazg-redroid:0.2.0"],
                              "os": "linux", "architecture": "amd64", "variant": None,
                              "rootfs_layers": ["sha256:" + "f" * 64],
                              "config": {"entrypoint": ["/init", "qemu=1", "androidboot.hardware=redroid"],
                                                  "user": "", "working_dir": "",
                                                  "cmd": ["androidboot.redroid_width=720", "androidboot.redroid_height=1280",
                                                  "androidboot.redroid_dpi=320", "androidboot.redroid_fps=60",
                                                  "androidboot.redroid_gpu_mode=guest", "androidboot.use_memfd=1"], "labels": {
                                  "org.ashnazg.base-image": redroid.IMAGE,
                                  "org.ashnazg.version": "0.2.0",
                                  "org.ashnazg.source-sha": "a" * 40,
                                  "org.ashnazg.platform": "linux/amd64",
                                  "org.ashnazg.agent-sha256": "b" * 64,
                                  "org.ashnazg.fixture-sha256": "c" * 64}}}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "provenance.json"
            path.write_text(json.dumps(descriptor))
            fresh = {
                "Id": descriptor["image_inspect"]["id"], "Os": "linux", "Architecture": "amd64",
                "RepoTags": ["ashnazg-redroid:0.2.0"],
                "RootFS": {"Layers": descriptor["image_inspect"]["rootfs_layers"]},
                "Config": {"Cmd": descriptor["image_inspect"]["config"]["cmd"],
                           "Entrypoint": descriptor["image_inspect"]["config"]["entrypoint"],
                           "User": "", "WorkingDir": "",
                           "Labels": descriptor["image_inspect"]["config"]["labels"]}}
            result = redroid.validate_image_provenance(path, architecture="amd64", checkout_sha="a" * 40,
                                                        checkout_dirty=False, inspect_raw=json.dumps([fresh]))
            for section, key, value in (
                ("Config", "Entrypoint", ["/unexpected"]),
                ("Config", "User", "unexpected"),
                ("Config", "WorkingDir", "/unexpected"),
                ("RootFS", "Layers", ["sha256:" + "e" * 64]),
            ):
                changed = json.loads(json.dumps(fresh))
                changed[section][key] = value
                with self.subTest(key=key), self.assertRaises(redroid.RedroidError):
                    redroid.validate_image_provenance(path, architecture="amd64", checkout_sha="a" * 40,
                                                     checkout_dirty=False, inspect_raw=json.dumps([changed]))
            self.assertEqual(result, "sha256:" + "d" * 64)
            supervisor = redroid.RedroidSupervisor(
                output_dir=Path(temp), host_system="Linux", host_arch="x86_64",
                binder_available=True, docker_path="docker", adb_path="adb", image_provenance=path)
            supervisor.deadline = redroid.time.monotonic() + 30
            supervisor._write_metadata()
            def command(args, **kwargs):
                if args[1:3] == ["context", "inspect"]:
                    return redroid.CommandResult(0, "unix:///var/run/docker.sock", "")
                if args[1:3] == ["devices", "-l"]:
                    return redroid.CommandResult(0, "List of devices attached", "")
                return redroid.CommandResult(0, json.dumps([fresh]), "")
            with patch.object(supervisor, "_command", command), patch.object(redroid, "check_port_available"), \
                    patch.object(redroid.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], 0, "a" * 40, ""),
                        subprocess.CompletedProcess([], 0, "", "")]):
                supervisor._preflight()
            self.assertEqual(supervisor.receipt["image"], descriptor["image_inspect"]["id"])
            self.assertEqual(supervisor.receipt["requested_display"]["fps"], 60)
            self.assertIsNone(supervisor.receipt["image_index"])
            descriptor["source_sha"] = "e" * 40
            path.write_text(json.dumps(descriptor))
            with self.assertRaisesRegex(redroid.RedroidError, "source"):
                redroid.validate_image_provenance(path, architecture="amd64", checkout_sha="a" * 40,
                                                   checkout_dirty=False, inspect_raw="[]")
            descriptor["source_sha"] = "a" * 40
            path.write_text(json.dumps(descriptor))
            wrong_image = json.loads(json.dumps([{"Id": "sha256:" + "e" * 64, "Os": "linux",
                                                  "Architecture": "amd64", "Config": {
                                                      "Cmd": descriptor["image_inspect"]["config"]["cmd"],
                                                      "Labels": descriptor["image_inspect"]["config"]["labels"]}}]))
            with self.assertRaises(redroid.RedroidError):
                redroid.validate_image_provenance(path, architecture="amd64", checkout_sha="a" * 40,
                                                   checkout_dirty=False, inspect_raw=json.dumps(wrong_image))
            with self.assertRaisesRegex(redroid.RedroidError, "clean checkout"):
                redroid.validate_image_provenance(path, architecture="amd64", checkout_sha="a" * 40,
                                                   checkout_dirty=True, inspect_raw="[]")

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

    def test_android15_64only_index_and_platform_digests_are_pinned(self):
        profile = "android15-64only"
        index = "sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee"
        for architecture, digest in {
            "amd64": "sha256:58e548d78c4854f032912757dc0782d7a86b9d602ffc10b2f09fb9e86731adf3",
            "arm64": "sha256:dc2024a999dd0acb1112a23cddc9537ec8cb60a0dbe5a87ce9fbd75a2f5e94d2",
        }.items():
            raw = json.dumps({"manifests": [
                {"digest": digest, "platform": {"os": "linux", "architecture": architecture}}
            ]})
            self.assertEqual(redroid.verify_image_index(raw, index, architecture, profile=profile), digest)
            with self.assertRaises(redroid.RedroidError):
                redroid.verify_image_index(raw, redroid.IMAGE_INDEX, architecture, profile=profile)

    def test_android15_64only_profile_selects_verified_pin_in_receipt(self):
        index = "sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee"
        digest = "sha256:dc2024a999dd0acb1112a23cddc9537ec8cb60a0dbe5a87ce9fbd75a2f5e94d2"
        supervisor = redroid.RedroidSupervisor(output_dir=Path("unused"), image_profile="android15-64only")
        supervisor.architecture = "arm64"
        supervisor.deadline = redroid.time.monotonic() + 30
        commands = []

        def command(args, **kwargs):
            commands.append(args)
            if args[1:3] == ["context", "inspect"]:
                return redroid.CommandResult(0, "unix:///var/run/docker.sock", "")
            if args[1:3] == ["devices", "-l"]:
                return redroid.CommandResult(0, "List of devices attached", "")
            if "--raw" in args:
                return redroid.CommandResult(0, json.dumps({"manifests": [
                    {"digest": digest, "platform": {"os": "linux", "architecture": "arm64"}}
                ]}), "")
            return redroid.CommandResult(0, "", "")

        supervisor.docker_path = "docker"
        supervisor.adb_path = "adb"
        supervisor.host_system = "Linux"
        supervisor.host_arch = "aarch64"
        supervisor.binder_available = True
        with patch.object(supervisor, "_command", command), patch.object(redroid, "check_port_available"):
            # Exercise the exact preflight contract after the non-Docker host checks.
            supervisor._preflight()
        self.assertEqual(supervisor.image, f"redroid/redroid@{index}")
        self.assertEqual(supervisor.receipt["image_index"], index)
        self.assertEqual(supervisor.receipt["platform_digest"], digest)
        self.assertEqual(supervisor.receipt["image"], f"redroid/redroid@{index}")
        self.assertTrue(any(args[-1] == f"redroid/redroid@{index}" for args in commands if "--raw" in args))

    def test_64only_profile_rejects_derived_image_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(redroid.RedroidError, "provenance"):
                redroid.RedroidSupervisor(output_dir=Path(temp), image_profile="android15-64only",
                                          image_provenance=Path(temp) / "provenance.json")

    def test_default_profile_keeps_existing_pins(self):
        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(output_dir=Path(temp))
            self.assertEqual(supervisor.image_profile, "default")
            self.assertEqual(supervisor.image, redroid.IMAGE)
            self.assertEqual(supervisor.image_index, redroid.IMAGE_INDEX)
            self.assertIs(supervisor.platform_digests, redroid.PLATFORM_DIGESTS)
            supervisor._write_metadata()
            self.assertEqual(supervisor.receipt["image_index"], redroid.IMAGE_INDEX)
            self.assertEqual(supervisor.receipt["image"], redroid.IMAGE)
            self.assertEqual(supervisor.receipt["image_profile"], "default")

    def test_port_check_binds_loopback_only_and_reports_busy_port(self):
        with patch.object(redroid.socket, "socket") as create_socket:
            sock = create_socket.return_value.__enter__.return_value
            redroid.check_port_available(5555)
            sock.bind.assert_called_once_with(("127.0.0.1", 5555))
            sock.bind.side_effect = OSError("busy")
            with self.assertRaisesRegex(redroid.RedroidError, "5555"):
                redroid.check_port_available(5555)


class RedroidLifecycleTests(unittest.TestCase):
    def test_initial_adb_timeout_retries_within_the_boot_deadline(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(args, kwargs['timeout'])
            return redroid.CommandResult(0, 'device' if args[-1] == 'get-state' else '1', '')
        supervisor = redroid.RedroidSupervisor(output_dir=Path('unused'), runner=runner, adb_path='adb')
        supervisor.deadline = redroid.time.monotonic() + 10
        with patch.object(redroid.time, 'sleep'), \
             patch.object(redroid, 'last_anr_status', return_value=False), \
             patch.object(redroid, 'readiness_status', return_value=(True, 'ready')), \
             patch.object(redroid, 'focused_application_window', return_value='owned-window'):
            supervisor._wait_ready()
        self.assertEqual(sum(args[1:2] == ['connect'] for args in calls), 2)
        self.assertEqual(supervisor.receipt['readiness'], 'ready')

    def test_repeated_adb_timeouts_still_stop_at_the_original_boot_deadline(self):
        now = [10.0]
        def runner(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs['timeout'])
        supervisor = redroid.RedroidSupervisor(output_dir=Path('unused'), runner=runner, adb_path='adb')
        supervisor.deadline = 12.0
        with patch.object(redroid.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(redroid.time, 'sleep', side_effect=lambda interval: now.__setitem__(0, now[0] + interval)):
            with self.assertRaisesRegex(redroid.RedroidError, 'timeout'):
                supervisor._wait_ready()
        self.assertEqual(now[0], 12.0)
        self.assertIn('probe timed out', supervisor.receipt['last_readiness'])

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

    def test_cli_forwards_explicit_image_profile(self):
        calls = []

        class FakeSupervisor:
            def __init__(self, **kwargs): calls.append(("init", kwargs))
            def run(self, command):
                calls.append(("run", command))
                return 17

        with patch.object(redroid, "RedroidSupervisor", FakeSupervisor):
            code = redroid.main(["run", "--output-dir", "out", "--image-profile", "android15-64only",
                                 "--", "true"])
        self.assertEqual(code, 17)
        self.assertEqual(calls[0][1]["image_profile"], "android15-64only")

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

    @patch.dict(os.environ, {}, clear=True)
    def test_diagnostics_are_not_collected_when_opt_in_is_unset(self):
        supervisor = redroid.RedroidSupervisor(output_dir=Path('unused'))
        supervisor.container_started = True
        with patch.object(supervisor, '_command') as command:
            supervisor._collect_diagnostics()
            command.assert_not_called()

    @patch.dict(os.environ, {'ASHNAZG_CI_DIAGNOSTICS': '1'})
    def test_diagnostic_collection_verifies_ownership_before_docker_exec(self):
        calls = []
        def command(args, **kwargs):
            calls.append((args, kwargs))
            return redroid.CommandResult(0, 'foreign-run-id')
        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(output_dir=Path(temp), runner=lambda *a, **k: None,
                docker_path='docker')
            supervisor.container_started = True
            with patch.object(supervisor, '_command', side_effect=command):
                supervisor._collect_diagnostics()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][1], 'inspect')
        self.assertNotIn('diagnostics', supervisor.receipt)

    @patch.dict(os.environ, {'ASHNAZG_CI_DIAGNOSTICS': '1'})
    def test_diagnostic_probes_are_static_bounded_and_failures_are_unavailable(self):
        calls = []
        def command(args, **kwargs):
            calls.append((args, kwargs))
            if args[1] == 'inspect':
                return redroid.CommandResult(0, supervisor.run_id)
            if any(arg.endswith('/getprop') for arg in args):
                raise redroid.RedroidError('probe failed')
            return redroid.CommandResult(0, 'ok')
        with tempfile.TemporaryDirectory() as temp:
            supervisor = redroid.RedroidSupervisor(output_dir=Path(temp), runner=lambda *a, **k: None,
                docker_path='docker')
            supervisor.container_started = True
            with patch.object(supervisor, '_command', side_effect=command):
                supervisor._collect_diagnostics()
        execs = [(args, kwargs) for args, kwargs in calls if args[1:2] == ['exec']]
        self.assertTrue(execs)
        self.assertTrue(all(kwargs['timeout'] == 5 and kwargs['use_deadline'] is False for _, kwargs in execs))
        expected = {
            ('/system/bin/logcat', '-b', 'all', '-d', '-t', '250'),
            ('/system/bin/getprop', 'sys.boot_completed'),
            ('/system/bin/getprop', 'ro.lmk.use_psi'),
            ('/system/bin/getprop', 'ro.lmk.use_minfree_levels'),
            ('/system/bin/getprop', 'ro.config.low_ram'),
            ('/system/bin/pidof', 'lmkd', 'system_server', 'surfaceflinger'),
            *(('/system/bin/cat', path) for path in (
                '/proc/pressure/memory', '/proc/cgroups', '/sys/fs/cgroup/cgroup.controllers',
                '/proc/self/mountinfo', '/system/etc/cgroups.json', '/vendor/etc/cgroups.json')),
        }
        self.assertEqual({tuple(args[3:]) for args, _ in execs}, expected)
        self.assertEqual(supervisor.receipt['diagnostics']['lmk_use_psi']['unavailable'], 'probe failed')
        self.assertTrue(all('/system/bin/sh' not in args for args, _ in execs))
        self.assertTrue({'boot_completed', 'last_anr', 'input', 'windows', 'logcat'}.issubset(
            supervisor.receipt['diagnostics']))

    @patch.dict(os.environ, {'ASHNAZG_CI_DIAGNOSTICS': '1'})
    def test_slow_container_probes_leave_time_for_every_retained_adb_probe(self):
        now = [0.0]
        calls = []
        supervisor = redroid.RedroidSupervisor(output_dir=Path('unused'), docker_path='docker', adb_path='adb')
        supervisor.container_started = True
        def command(args, **kwargs):
            calls.append(args)
            now[0] += kwargs['timeout']
            if args[0] == 'docker':
                raise redroid.RedroidError('container probe timed out')
            return redroid.CommandResult(0, 'ADB evidence')
        with patch.object(supervisor, '_owned_container', return_value=True), \
             patch.object(supervisor, '_command', side_effect=command), \
             patch.object(redroid.time, 'monotonic', side_effect=lambda: now[0]):
            supervisor._collect_diagnostics()
        self.assertEqual(len([args for args in calls if args[0] == 'adb']), 5)
        self.assertLessEqual(now[0], 45.0)
        for name in ('boot_completed', 'last_anr', 'input', 'windows', 'logcat'):
            self.assertEqual(supervisor.receipt['diagnostics'][name]['output'], 'ADB evidence')

    @patch.dict(os.environ, {'ASHNAZG_CI_DIAGNOSTICS': '1'})
    def test_diagnostics_stop_probing_when_the_collection_budget_is_exhausted(self):
        supervisor = redroid.RedroidSupervisor(output_dir=Path('unused'))
        supervisor.container_started = True
        with patch.object(supervisor, '_owned_container', return_value=True), \
             patch.object(supervisor, '_command') as command, \
             patch.object(redroid.time, 'monotonic', side_effect=[0.0] + [46.0] * 17):
            supervisor._collect_diagnostics()
        command.assert_not_called()
        self.assertTrue(supervisor.receipt['diagnostics'])
        self.assertTrue(all('budget exhausted' in probe['unavailable']
                            for probe in supervisor.receipt['diagnostics'].values()))

    def test_diagnostic_redaction_covers_json_and_flag_token_formats(self):
        output = redroid._bounded('{"refresh_token": "json-secret", --access-token=flag-secret, token plain-secret}')
        for secret in ('json-secret', 'flag-secret', 'plain-secret'):
            self.assertNotIn(secret, output)
        self.assertEqual(output.count('[REDACTED]'), 3)

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
