import json
import os
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "redroid_action.sh"


class RedroidActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.capture = self.root / "capture.json"
        python = self.bin / "python3"
        python.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "command = pathlib.Path(args[-1]).read_text()\n"
            "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({'args': args, 'command': command}))\n"
            "if os.environ.get('FAKE_SLEEP'):\n"
            "    import time\n"
            "    time.sleep(30)\n"
            "raise SystemExit(int(os.environ.get('FAKE_EXIT', '0')))\n"
        )
        python.chmod(0o755)

    def run_wrapper(self, **extra):
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.bin}:{env['PATH']}",
            "CAPTURE": str(self.capture),
            "ASHNAZG_REDROID_COMMAND": "printf 'literal $HOME ; *\\n'\nexit 0\n",
            "ASHNAZG_REDROID_OUTPUT_DIRECTORY": "artifacts/redroid",
            "ASHNAZG_REDROID_ADB_PORT": "5555",
            "ASHNAZG_REDROID_BOOT_TIMEOUT": "180",
        })
        env.update(extra)
        return subprocess.run(["bash", str(WRAPPER)], env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)

    def test_passes_inputs_as_separate_arguments_and_preserves_command_text(self):
        marker = self.root / "SHOULD_NOT_EXIST"
        output = f"{self.root}/results ; $(touch {marker}) *"
        result = self.run_wrapper(ASHNAZG_REDROID_OUTPUT_DIRECTORY=output)

        self.assertEqual(result.returncode, 0, result.stderr)
        capture = json.loads(self.capture.read_text())
        self.assertEqual(capture["args"][:2], [str(ROOT / "scripts/redroid.py"), "run"])
        self.assertEqual(capture["args"][2:6], ["--output-dir", output, "--adb-port", "5555"])
        self.assertEqual(capture["args"][6:9], ["--timeout", "180", "--"])
        self.assertEqual(capture["args"][9:14], ["bash", "--noprofile", "--norc", "-e", "-o"])
        self.assertEqual(capture["args"][14], "pipefail")
        command_file = Path(capture["args"][15])
        self.assertEqual(capture["command"], "printf 'literal $HOME ; *\\n'\nexit 0\n")
        self.assertFalse(command_file.exists(), "owned command file must be removed")
        self.assertFalse(marker.exists())

    def test_optional_image_provenance_precedes_command_separator_without_shell_evaluation(self):
        marker = self.root / "SHOULD_NOT_EXIST"
        provenance = f"{self.root}/provenance ; $(touch {marker}) *.json"
        result = self.run_wrapper(ASHNAZG_REDROID_IMAGE_PROVENANCE=provenance)

        self.assertEqual(result.returncode, 0, result.stderr)
        capture = json.loads(self.capture.read_text())
        args = capture["args"]
        self.assertEqual(args[6:11], ["--timeout", "180", "--image-provenance", provenance, "--"])
        self.assertFalse(marker.exists())

    def test_empty_image_provenance_preserves_default_argv(self):
        result = self.run_wrapper(ASHNAZG_REDROID_IMAGE_PROVENANCE="")

        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())["args"]
        self.assertEqual(args[6:9], ["--timeout", "180", "--"])
        self.assertNotIn("--image-provenance", args)

    def test_private_binder_selects_the_pinned_compatible_image(self):
        bootstrap = str(self.root / "private binder-bootstrap")
        result = self.run_wrapper(ASHNAZG_REDROID_BINDER_BOOTSTRAP=bootstrap)

        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.capture.read_text())["args"]
        self.assertEqual(args[8:13], ["--image-profile", "android15-64only", "--binder-bootstrap", bootstrap, "--"])

    def test_private_binder_and_derived_image_are_mutually_exclusive(self):
        result = self.run_wrapper(ASHNAZG_REDROID_BINDER_BOOTSTRAP="bootstrap",
                                  ASHNAZG_REDROID_IMAGE_PROVENANCE="derived.json")

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.capture.exists())

    def test_signal_forwarding_still_removes_command_file(self):
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.bin}:{env['PATH']}",
            "CAPTURE": str(self.capture),
            "FAKE_SLEEP": "1",
            "ASHNAZG_REDROID_COMMAND": "sleep 30\n",
            "ASHNAZG_REDROID_OUTPUT_DIRECTORY": "artifacts/redroid",
            "ASHNAZG_REDROID_ADB_PORT": "5555",
            "ASHNAZG_REDROID_BOOT_TIMEOUT": "180",
        })
        process = subprocess.Popen(["bash", str(WRAPPER)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(100):
                if self.capture.exists():
                    break
                import time
                time.sleep(0.01)
            self.assertTrue(self.capture.exists(), "runtime did not start")
            command_file = Path(json.loads(self.capture.read_text())["args"][-1])
            process.terminate()
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143)
            self.assertFalse(command_file.exists(), "owned command file must be removed after a signal")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_propagates_runtime_exit_and_still_removes_command_file(self):
        result = self.run_wrapper(FAKE_EXIT="47")

        self.assertEqual(result.returncode, 47)
        capture = json.loads(self.capture.read_text())
        self.assertFalse(Path(capture["args"][-1]).exists())

    def test_rejects_invalid_numeric_input_before_runtime(self):
        result = self.run_wrapper(ASHNAZG_REDROID_ADB_PORT="55; touch BAD")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.root / "BAD").exists())

    def test_rejects_noncanonical_decimal_before_runtime(self):
        result = self.run_wrapper(ASHNAZG_REDROID_ADB_PORT="05555")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())

    def test_rejects_root_output_directory(self):
        result = self.run_wrapper(ASHNAZG_REDROID_OUTPUT_DIRECTORY="/")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())


if __name__ == "__main__":
    unittest.main()
