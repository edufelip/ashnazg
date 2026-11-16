import gzip
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
HOME = BASE / 'prepublication-home'
STATE = HOME / 'Library/Application Support/ashnazg/native-mcp' / SOURCE
PYTHON = '/opt/homebrew/bin/python3'

for mode in ('eof', 'sigterm', 'sigterm-inflight'):
    output = BASE / 'evidence' / (mode + '-verified')
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
            '--binary', str(BASE / 'mac-package/bin/ashnazg'), '--fixture-apk', str(FILES / 'ashnazg-fixture-v0.3.0.apk'),
            '--home', str(HOME), '--output-dir', str(output), '--mode', mode, '--source-sha', SOURCE]
    stdout, stderr = BASE / (mode + '-verified.stdout'), BASE / (mode + '-verified.stderr')
    with stdout.open('xb') as out, stderr.open('xb') as err:
        result = subprocess.run(args, stdout=out, stderr=err, timeout=1600)
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
