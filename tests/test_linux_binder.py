import importlib.util
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from scripts import linux_binder

class BinderFixtureTests(unittest.TestCase):
    def test_missing_devices_are_created_with_kernel_binderfs_ioctl(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'binder-control').touch()
            def kernel_allocate(fd, code, payload, mutate):
                self.assertEqual(code, 0xc1086201)
                self.assertEqual(len(payload), 264)
                name = struct.unpack('256sII', payload)[0].split(b'\0', 1)[0].decode()
                (root/name).symlink_to('/dev/null')
            with patch.object(linux_binder.fcntl, 'ioctl', side_effect=kernel_allocate) as ioctl, \
                 patch.object(Path, 'chmod') as chmod:
                linux_binder.ensure_nodes(root)
                self.assertEqual(ioctl.call_count, 3)
                self.assertEqual(chmod.call_count, 3)

    def test_alias_creation_rolls_back_without_replacing_foreign_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); dev=root/'dev'; dev.mkdir(); mount=root/'mount'; mount.mkdir()
            for name in linux_binder.NAMES: (mount/name).symlink_to('/dev/null')
            (dev/'hwbinder').write_text('foreign data')
            with self.assertRaisesRegex(RuntimeError, 'foreign'):
                linux_binder.link_nodes(mount, dev)
            self.assertFalse((dev/'binder').exists())
            self.assertEqual((dev/'hwbinder').read_text(), 'foreign data')

    def test_existing_character_devices_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            dev=Path(temp)
            for name in linux_binder.NAMES: (dev/name).symlink_to('/dev/null')
            self.assertEqual(linux_binder.link_nodes(Path('/unused'), dev), [])

    def test_cleanup_rejects_foreign_mount_identity(self):
        with self.assertRaisesRegex(RuntimeError, 'ownership'):
            linux_binder.cleanup({'mountpoint':'/dev/some-other-device', 'run_id':'0'*32})

    def test_existing_regular_entry_is_not_admitted_as_binder(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'binder').touch()
            with self.assertRaisesRegex(RuntimeError, 'non-character'):
                linux_binder.ensure_nodes(root)

    def test_local_host_invocation_is_refused_before_changes(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS':'false'}), \
             patch('sys.argv', ['linux_binder.py', '--output-dir', '/never-created']):
            with self.assertRaises(SystemExit): linux_binder.main()
