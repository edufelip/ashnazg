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
