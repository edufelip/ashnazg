import os
import subprocess
import unittest
from unittest.mock import patch
from scripts import dev_viewer


class ViewerTests(unittest.TestCase):
    def test_linux_preflight_checks_real_flags_and_explicit_device(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            output = 'List of devices attached\nphone-1\tdevice\nphone-2\tdevice\n' if args[-1] == 'devices' else 'version\n'
            return subprocess.CompletedProcess(args, 0, output, '')
        with patch.object(dev_viewer.platform, 'system', return_value='Linux'), patch.object(
                dev_viewer.shutil, 'which', return_value='/bin/tool'), patch.object(
                dev_viewer.subprocess, 'run', side_effect=run):
            self.assertEqual(dev_viewer.preflight('phone-1', 'adb', 'scrcpy'), 0)
        self.assertIn(['scrcpy', '--version'], calls)

    def test_offline_device_is_refused(self):
        result = subprocess.CompletedProcess([], 0, 'phone-1\toffline\n', '')
        with patch.object(dev_viewer.platform, 'system', return_value='Linux'), patch.object(
                dev_viewer.shutil, 'which', return_value='/bin/tool'), patch.object(
                dev_viewer.subprocess, 'run', return_value=result):
            self.assertNotEqual(dev_viewer.preflight('phone-1', 'adb', 'scrcpy'), 0)

    def test_launch_uses_selected_adb_and_serial_without_shell(self):
        with patch.object(dev_viewer.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(dev_viewer.launch('phone one', 'scrcpy', '/custom/adb'), 0)
        self.assertEqual(run.call_args.args[0], ['scrcpy', '--serial', 'phone one'])
        self.assertEqual(run.call_args.kwargs['env']['ADB'], '/custom/adb')
        self.assertFalse(run.call_args.kwargs.get('shell', False))
