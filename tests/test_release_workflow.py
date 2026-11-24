"""Exercise executable workflow snippets without hosted builds or publication."""
import os
import re
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def python_blocks(path):
    text = path.read_text()
    return [textwrap.dedent(match.group(2)) for match in re.finditer(
        r"^([ ]*)python3 - <<'PYTHON'\n(.*?)^\1PYTHON$", text, re.MULTILINE | re.DOTALL)]


class ReleaseWorkflowTests(unittest.TestCase):
    def test_all_embedded_python_compiles(self):
        paths = [ROOT / '.github/workflows/release.yml', ROOT / '.github/workflows/microvm.yml']
        counts = []
        for path in paths:
            blocks = python_blocks(path)
            counts.append(len(blocks))
            for block in blocks:
                compile(block, str(path), 'exec')
        self.assertEqual(counts, [3, 1])

    def run_guest_scope(self, force, paths, base='b' * 40, head='a' * 40):
        block, = python_blocks(ROOT / '.github/workflows/microvm.yml')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            env = {'FORCE_GUEST': force, 'BASE_SHA': base, 'HEAD_SHA': head, 'GITHUB_OUTPUT': str(output)}
            with patch.dict(os.environ, env), patch('subprocess.check_output', return_value='\n'.join(paths)):
                exec(compile(block, 'microvm-scope', 'exec'), {})
            return output.read_text()

    def test_candidate_force_builds_even_with_unrelated_diff(self):
        self.assertEqual(self.run_guest_scope('true', ['README.md']), 'required=true\n')

    def test_ordinary_quality_retains_path_scope(self):
        self.assertEqual(self.run_guest_scope('false', ['README.md']), 'required=false\n')
        self.assertEqual(self.run_guest_scope('false', ['runtime/microvm/sources.lock.json']), 'required=true\n')
        self.assertEqual(self.run_guest_scope('false', ['scripts/session_data.py']), 'required=true\n')

    def test_invalid_source_fails_before_reporting_a_scope(self):
        with self.assertRaises(AssertionError):
            self.run_guest_scope('true', [], head='invalid')


if __name__ == '__main__':
    unittest.main()
