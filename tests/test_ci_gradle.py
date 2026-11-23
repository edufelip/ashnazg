import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_gradle


FAKE_GRADLE = """#!{python}
import sys
from pathlib import Path

count_file = Path({count_file!r})
attempt = int(count_file.read_text()) + 1 if count_file.exists() else 1
count_file.write_text(str(attempt))
Path({args_file!r}).write_text(" ".join(sys.argv[1:]))

mode = {mode!r}
if mode == "fail_compile":
    print("> Task :agent:compileDebugKotlin FAILED", file=sys.stderr)
    print("e: Unresolved reference: nope", file=sys.stderr)
    sys.exit(1)
if mode == "retry_then_pass":
    if attempt == 1:
        print("> Could not get 'https://repo.maven.apache.org/maven2/foo.pom'. Received status code 429 from server: Too Many Requests", file=sys.stderr)
        sys.exit(1)
    print("BUILD SUCCESSFUL")
    sys.exit(0)
if mode == "always_429":
    print("Received status code 429 from server: Too Many Requests", file=sys.stderr)
    sys.exit(2)
sys.exit(0)
"""


class CiGradleTests(unittest.TestCase):
    def _write_fake(self, root: Path, mode: str) -> tuple[Path, Path, Path]:
        count_file = root / "attempts.txt"
        args_file = root / "args.txt"
        wrapper = root / "gradlew"
        wrapper.write_text(
            FAKE_GRADLE.format(
                python=sys.executable,
                count_file=str(count_file),
                args_file=str(args_file),
                mode=mode,
            )
        )
        wrapper.chmod(0o755)
        return wrapper, count_file, args_file

    def test_retries_transient_429_and_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, count_file, args_file = self._write_fake(root, "retry_then_pass")
            with patch.object(ci_gradle.time, "sleep") as sleep_mock:
                code = ci_gradle.run_gradle_with_retries(
                    workdir=root,
                    gradle_args=["test", "assembleDebug"],
                    max_attempts=3,
                    backoff_seconds=[1.5, 3.0],
                )
            self.assertEqual(code, 0)
            self.assertEqual(count_file.read_text(), "2")
            self.assertEqual(args_file.read_text(), "test assembleDebug")
            sleep_mock.assert_called_once_with(1.5)

    def test_non_transient_build_failure_stops_after_first_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, count_file, _ = self._write_fake(root, "fail_compile")
            with patch.object(ci_gradle.time, "sleep") as sleep_mock:
                code = ci_gradle.run_gradle_with_retries(
                    workdir=root,
                    gradle_args=["test"],
                    max_attempts=3,
                    backoff_seconds=[1.5, 3.0],
                )
            self.assertEqual(code, 1)
            self.assertEqual(count_file.read_text(), "1")
            sleep_mock.assert_not_called()

    def test_stops_after_max_attempts_when_429_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, count_file, _ = self._write_fake(root, "always_429")
            with patch.object(ci_gradle.time, "sleep") as sleep_mock:
                code = ci_gradle.run_gradle_with_retries(
                    workdir=root,
                    gradle_args=["assembleDebug"],
                    max_attempts=3,
                    backoff_seconds=[2.0, 4.0],
                )
            self.assertEqual(code, 2)
            self.assertEqual(count_file.read_text(), "3")
            self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [2.0, 4.0])

    def test_cli_entry_point_respects_relative_workdir_and_double_dash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / "agent-android"
            workdir.mkdir()
            wrapper, count_file, args_file = self._write_fake(workdir, "retry_then_pass")
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                code = ci_gradle.main([
                    "--workdir", "agent-android",
                    "--max-attempts", "2",
                    "--backoff-seconds", "0",
                    "--", ":agent:assembleDebug",
                ])
            finally:
                os.chdir(previous_cwd)
            self.assertEqual(code, 0)
            self.assertEqual(count_file.read_text(), "2")
            self.assertEqual(args_file.read_text(), ":agent:assembleDebug")

    def test_cli_entry_point_respects_env_override_and_double_dash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            override_dir = root / "bin"
            override_dir.mkdir()
            wrapper, count_file, args_file = self._write_fake(override_dir, "retry_then_pass")
            with patch.dict(os.environ, {"ASHNAZG_CI_GRADLE_BIN": str(wrapper)}):
                code = ci_gradle.main([
                    "--workdir", str(root),
                    "--max-attempts", "2",
                    "--backoff-seconds", "0",
                    "--", ":agent:assembleDebug",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(count_file.read_text(), "2")
            self.assertEqual(args_file.read_text(), ":agent:assembleDebug")


if __name__ == "__main__":
    unittest.main()
