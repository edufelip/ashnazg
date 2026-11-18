import unittest
from unittest.mock import patch

from scripts import dev_session


class DevSessionTests(unittest.TestCase):
    def test_validates_host_prerequisites_without_installing_or_mutating(self):
        with patch.object(dev_session.platform, 'system', return_value='Linux'), \
             patch.object(dev_session.platform, 'machine', return_value='x86_64'), \
             patch.object(dev_session.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name), \
             patch.object(dev_session.Path, 'exists', return_value=True), \
             patch.dict(dev_session.os.environ, {'ANDROID_HOME': '/sdk'}), \
             patch.object(dev_session.Path, 'is_dir', return_value=True), \
             patch.object(dev_session.subprocess, 'run'):
            result = dev_session.validate_prerequisites()
        self.assertEqual(result['architecture'], 'amd64')
        self.assertEqual(result['docker'], '/usr/bin/docker')

    def test_rejects_non_linux_and_missing_binder(self):
        with patch.object(dev_session.platform, 'system', return_value='Darwin'):
            with self.assertRaisesRegex(dev_session.DevSessionError, 'Linux'):
                dev_session.validate_prerequisites()
        with patch.object(dev_session.platform, 'system', return_value='Linux'), \
             patch.object(dev_session.platform, 'machine', return_value='x86_64'), \
             patch.object(dev_session.shutil, 'which', return_value='/bin/tool'), \
             patch.object(dev_session.Path, 'exists', return_value=False):
            with self.assertRaisesRegex(dev_session.DevSessionError, 'binder'):
                dev_session.validate_prerequisites()

    def test_rejects_invalid_external_command_and_port(self):
        with self.assertRaises(SystemExit):
            dev_session.main(['start', '--host-command', '', '--', 'mcp'])
        with self.assertRaises(SystemExit):
            dev_session.main(['start', '--adb-port', '70000', '--host-command', 'mcp'])

    def test_session_builds_scoped_redroid_supervisor_command(self):
        args = dev_session.build_parser().parse_args(['start', '--host-command', 'ashnazg', '--', 'mcp'])
        command = dev_session.redroid_command(args, '/tmp/session')
        self.assertIn('redroid.py', command[1])
        self.assertIn('--', command)
        self.assertEqual(command[-2:], ['ashnazg', 'mcp'])


if __name__ == '__main__':
    unittest.main()

class SessionLifecycleTests(unittest.TestCase):
    def test_direct_script_inside_resolves_harness_import(self):
        import subprocess
        import sys
        import tempfile
        script = str(dev_session.ROOT / 'scripts/dev_session.py')
        code = ("import runpy,sys,types; sys.path.insert(0, " + repr(str(dev_session.ROOT / 'scripts')) + "); "
                "m=runpy.run_path(" + repr(script) + "); "
                "m['subprocess'].run=lambda *a,**k: types.SimpleNamespace(returncode=42); "
                "sys.exit(m['_inside'](5555,['/missing-host']))")
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-c', code], cwd=directory,
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 42, result.stderr)

    def test_readiness_failure_cleans_harness_and_never_launches_host(self):
        from scripts import harness
        with patch.dict(dev_session.os.environ, {}, clear=False), patch.object(
                dev_session.subprocess, 'run', return_value=type('Result', (), {'returncode': 0})()), patch.object(
                harness, 'DeviceHarness') as factory, patch.object(dev_session.subprocess, 'Popen') as launch:
            factory.return_value.start.side_effect = RuntimeError('not ready')
            with self.assertRaisesRegex(RuntimeError, 'not ready'):
                dev_session._inside(5555, ['ashnazg', 'mcp'])
            factory.return_value.cleanup.assert_called_once()
            launch.assert_not_called()

    def test_android15_profile_is_explicit(self):
        from scripts import redroid
        args = dev_session.build_parser().parse_args(['start', '--host-command', 'ashnazg', '--', 'mcp'])
        command = dev_session.redroid_command(args, '/tmp/session')
        parsed = redroid.build_parser().parse_args(command[2:])
        self.assertEqual(parsed.image_profile, 'android15-64only')

    def test_build_output_is_kept_off_mcp_stdout(self):
        from scripts import harness
        with patch.object(dev_session.subprocess, 'run', return_value=type('Result', (), {'returncode': 42})()) as build:
            self.assertEqual(dev_session._inside(5555, ['ashnazg', 'mcp']), 42)
        self.assertIs(build.call_args.kwargs.get('stdout'), dev_session.sys.stderr)

    def test_sigterm_is_forwarded_to_owned_supervisor(self):
        import os
        import signal
        import subprocess
        import sys
        import tempfile
        import time
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / 'ready'
            done = Path(directory) / 'done'
            child_code = ("import signal,sys,time; from pathlib import Path; "
                          "signal.signal(signal.SIGTERM, lambda *a: (Path(" + repr(str(done)) + ").touch(), sys.exit(0))); "
                          "Path(" + repr(str(ready)) + ").touch(); time.sleep(20)")
            driver = "from scripts.dev_session import run_supervisor; import sys; sys.exit(run_supervisor(" + repr([sys.executable, '-c', child_code]) + "))"
            process = subprocess.Popen([sys.executable, '-c', driver], cwd=dev_session.ROOT)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertTrue(done.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_missing_buildx_fails_preflight_before_guest_start(self):
        import subprocess
        with patch.object(dev_session.platform, 'system', return_value='Linux'), patch.object(
                dev_session.platform, 'machine', return_value='x86_64'), patch.object(
                dev_session.shutil, 'which', return_value='/bin/tool'), patch.object(
                dev_session.Path, 'exists', return_value=True), patch.dict(
                dev_session.os.environ, {'ANDROID_HOME': '/sdk'}), patch.object(
                dev_session.Path, 'is_dir', return_value=True), patch.object(
                dev_session.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['docker', 'buildx'])):
            with self.assertRaisesRegex(dev_session.DevSessionError, 'Docker'):
                dev_session.validate_prerequisites()


class PackagedSessionTests(unittest.TestCase):
    def test_requires_package_bytes_to_match_manifest(self):
        import hashlib
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["bin/ashnazg", "android/agent-instrumentation.apk",
                     "android/fixture-instrumentation.apk"]
            files = {}
            for name in names:
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(name.encode())
                path.chmod(0o755)
                files[name] = {"size": len(name), "sha256": hashlib.sha256(name.encode()).hexdigest()}
            (root / "manifest.json").write_text(json.dumps({
                "name": "ashnazg", "platform": "linux-x86_64", "files": files,
            }))
            self.assertEqual(dev_session.package_artifacts(root), tuple(root / name for name in names))
            (root / names[1]).write_bytes(b"incorrect agent")
            with self.assertRaisesRegex(dev_session.DevSessionError, "integrity"):
                dev_session.package_artifacts(root)

    def test_session_carries_guest_owned_binder_and_release_directory(self):
        from pathlib import Path
        args = dev_session.build_parser().parse_args([
            "start", "--release-dir", "/tmp/pkg", "--binder-bootstrap", "/tmp/binder",
            "--adb-port", "15561", "--", "mcp",
        ])
        args.release_dir = Path("/tmp/pkg")
        self.assertIn("--release-dir", dev_session.redroid_command(args, "/tmp/session"))
        self.assertIn("--binder-bootstrap", dev_session.redroid_command(args, "/tmp/session"))

    def test_source_host_command_can_have_no_arguments(self):
        args = dev_session.build_parser().parse_args(["start", "--host-command", "/bin/bash"])
        command = dev_session.redroid_command(args, "/tmp/session")
        self.assertEqual(command[-2:], ["--", "/bin/bash"])
