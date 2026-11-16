import gzip
import contextlib
import sys
from unittest.mock import patch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time

BASE = Path(__file__).resolve().parent
REPO = Path('/Users/eduwaldo/.t3/worktrees/ashnazg/t3-a04e62d5-118037da6f6e517d')
SOURCE = '50434b8a056514fafd9fcb63cc534ce369880184'
FILES = BASE / 'attempt-2-artifact-14252024126/release-files'
HOME = BASE / 'public-home'
STATE = HOME / 'Library/Application Support/ashnazg/native-mcp' / SOURCE
PYTHON = '/opt/homebrew/bin/python3'

sys.path.insert(0, str(REPO / 'scripts'))
import native_cli_acceptance as driver

for mode in ('eof',):
    output = BASE / 'evidence' / (mode + '-public-warm-offline-bound')
    assert not output.exists()
    done = threading.Event()
    secrets = []

    def observe_credentials():
        # Values remain in this process's memory and are never printed/written.
        deadline = time.monotonic() + 350
        while not done.is_set() and time.monotonic() < deadline:
            try:
                fd = os.open(STATE / '.ashnazg-session-v1.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 8192
                    document = json.loads(stream.read(8193))
                values = [document['rpc_token'], document['install_token']]
                assert all(re.fullmatch('[0-9a-f]{64}', value) for value in values)
                secrets.extend(value.encode() for value in values)
                return
            except FileNotFoundError:
                done.wait(0.02)

    watcher = threading.Thread(target=observe_credentials, daemon=True)
    watcher.start()
    args = [PYTHON, str(REPO / 'scripts/native_cli_acceptance.py'), '--candidate-dir', str(FILES),
            '--binary', str(BASE / 'public-mac-package/bin/ashnazg'), '--fixture-apk', str(BASE / 'public-mac-package/android/fixture-instrumentation.apk'),
            '--home', str(HOME), '--output-dir', str(output), '--mode', mode, '--source-sha', SOURCE]
    stdout, stderr = BASE / (mode + '-public-warm-offline-bound.stdout'), BASE / (mode + '-public-warm-offline-bound.stderr')
    original_popen = subprocess.Popen
    policy_launches = []
    expected_policy_sha256 = 'dfc42c68e4c06ddb51dbe133d3496d737f1f20cbe832694669222b94633d1347'

    def launch_only_cli_under_policy(command, *positional, **options):
        if command == [str(BASE / 'public-mac-package/bin/ashnazg'), 'mcp']:
            profile = Path('/private/tmp/ashnazg-offline-root-probe-79c7334p/profile.sb')
            observed = hashlib.sha256(profile.read_bytes()).hexdigest()
            assert observed == expected_policy_sha256
            policy_launches.append({'binary': command[0], 'profile_sha256': observed})
            command = ['/usr/bin/sandbox-exec', '-f', str(profile), *command]
        return original_popen(command, *positional, **options)

    with stdout.open('x') as out, stderr.open('x') as err:
        with patch.object(driver.subprocess, 'Popen', side_effect=launch_only_cli_under_policy), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = subprocess.CompletedProcess(args, driver.main(args[2:]))
    assert len(policy_launches) == 1
    assert hashlib.sha256(Path('/private/tmp/ashnazg-offline-root-probe-79c7334p/profile.sb').read_bytes()).hexdigest() == expected_policy_sha256
    (output / 'launch-policy.json').write_text(json.dumps({'schema_version': 1, 'cli_launch_count': 1, 'launches': policy_launches, 'profile_before_after_matches': True, 'controller_outside_policy': True}, sort_keys=True, indent=2) + '\n')
    done.set()
    watcher.join(timeout=2)
    assert not watcher.is_alive() and len(secrets) == 2
    files = sorted(path for path in output.rglob('*') if path.is_file()) + [stdout, stderr]
    for name in ('runtime.json', 'serial.log'):
        path = STATE / name
        assert path.is_file() and path.stat().st_size <= 16 * 1024 * 1024
        data = path.read_bytes()
        assert not any(value in data for value in secrets), 'runtime diagnostic contains a live credential'
        target = output / (name + '.gz' if name.endswith('.log') else name)
        target.write_bytes(gzip.compress(data, mtime=0) if name.endswith('.log') else data)
        files.append(target)
    scanned = 0
    for path in files:
        assert path.is_file() and path.stat().st_size <= 20 * 1024 * 1024
        data = path.read_bytes()
        assert not any(value in data for value in secrets), 'evidence contains a live credential'
        scanned += len(data)
    secrets.clear()
    scan = {'schema_version': 1, 'actual_session_tokens_observed': 2,
            'tokens_written_or_printed': False, 'matching_secret_found': False,
            'files_scanned': len(files), 'bytes_scanned': scanned}
    (output / 'secret-scan.json').write_text(json.dumps(scan, sort_keys=True, indent=2) + '\n')
    receipt = json.loads((output / 'receipt.json').read_text())
    print(json.dumps({'mode': mode, 'exit_code': result.returncode, 'status': receipt['status'],
                      'error': receipt.get('error'), 'ready_seconds': receipt.get('ready', {}).get('elapsed_seconds'),
                      'tools_called': receipt['tools_called'], 'secret_scan': 'passed'}), flush=True)
    assert result.returncode == 0 and receipt['status'] == 'passed'
