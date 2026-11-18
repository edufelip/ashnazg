import tempfile
import unittest
from pathlib import Path

from scripts import session_data


class SessionDataTests(unittest.TestCase):
    def test_reopen_preserves_data_and_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session'
            with session_data.SessionData(path, {'image': 'pinned', 'architecture': 'amd64'}) as first:
                identity = first.identity
                (first.data / 'sentinel').write_text('committed app data')
            with session_data.SessionData(path, {'image': 'pinned', 'architecture': 'amd64'}) as second:
                self.assertEqual(second.identity, identity)
                self.assertEqual((second.data / 'sentinel').read_text(), 'committed app data')

    def test_second_owner_is_refused_until_release(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session'
            with session_data.SessionData(path, {}) as first:
                with self.assertRaisesRegex(session_data.StorageError, 'in use'):
                    with session_data.SessionData(path, {}):
                        self.fail('second writer acquired data')
            with session_data.SessionData(path, {}):
                pass

    def test_mismatch_or_missing_data_never_resets_state(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session'
            with session_data.SessionData(path, {'image': 'one'}) as data:
                (data.data / 'sentinel').write_text('keep')
            with self.assertRaisesRegex(session_data.StorageError, 'contract'):
                with session_data.SessionData(path, {'image': 'two'}):
                    pass
            (path / 'data' / 'sentinel').unlink()
            (path / 'data').rmdir()
            with self.assertRaisesRegex(session_data.StorageError, 'missing'):
                with session_data.SessionData(path, {'image': 'one'}):
                    pass
            self.assertFalse((path / 'data').exists())

    def test_symlinked_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'session'
            with session_data.SessionData(path, {}):
                pass
            manifest = path / 'manifest.json'
            manifest.rename(path / 'original.json')
            manifest.symlink_to(path / 'original.json')
            with self.assertRaises(session_data.StorageError):
                with session_data.SessionData(path, {}):
                    pass
