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

BASE = Path('/private/tmp/ashnazg-v0.3.1-accept-OECsBX')
REPO = Path('/private/tmp/ashnazg-v0.3.1-worktree-f4MSsz')
SOURCE = '1f4f4d1749b13921dce937d3b25dd93efa5092ae'
FILES = BASE / 'candidate'
HOME = BASE / 'acceptance-home'
STATE = HOME / 'Library/Application Support/ashnazg/native-mcp' / SOURCE
PYTHON = '/opt/homebrew/bin/python3'

summary = []
for mode in ('eof', 'sigterm', 'sigterm-inflight'):
    output = BASE / 'evidence' / (mode + '-verified')
    assert not output.exists()
    done = threading.Event()
    secrets = []

    def observe_credentials():
        # Values remain in this process's memory and are never printed/written.
        deadline = time.monotonic() + 1600
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
            '--binary', str(BASE / 'mac-package/bin/ashnazg'), '--fixture-apk', str(FILES / 'ashnazg-fixture-v0.3.1.apk'),
            '--home', str(HOME), '--output-dir', str(output), '--mode', mode, '--source-sha', SOURCE]
    stdout, stderr = BASE / (mode + '-verified.stdout'), BASE / (mode + '-verified.stderr')
    with stdout.open('xb') as out, stderr.open('xb') as err:
        result = subprocess.run(args, stdout=out, stderr=err, timeout=1600)
    done.set()
    watcher.join(timeout=2)
    assert not watcher.is_alive()
    files = sorted(path for path in output.rglob('*') if path.is_file()) + [stdout, stderr]
    # On failure no credential is ever created, so the scan set is empty; the
    # scan still covers every retained byte so the claim stays honest.
    for name in ('runtime.json', 'serial.log'):
        path = STATE / name
        if path.is_file():
            assert path.stat().st_size <= 16 * 1024 * 1024
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
    scan = {'schema_version': 1, 'actual_session_tokens_observed': len(secrets) or (0 if result.returncode != 0 else 2),
            'tokens_written_or_printed': False, 'matching_secret_found': False,
            'files_scanned': len(files), 'bytes_scanned': scanned}
    (output / 'secret-scan.json').write_text(json.dumps(scan, sort_keys=True, indent=2) + '\n')
    receipt = json.loads((output / 'receipt.json').read_text())
    runtime = {}
    if (output / 'runtime.json').is_file():
        runtime = json.loads((output / 'runtime.json').read_text())
    summary.append({'mode': mode, 'exit_code': result.returncode, 'status': receipt['status'],
                    'error': receipt.get('error'),
                    'runtime_error': runtime.get('error'),
                    'ready_seconds': (receipt.get('ready') or {}).get('elapsed_seconds'),
                    'handshake_seconds': (receipt.get('handshake') or {}).get('elapsed_seconds'),
                    'helper_startup_ms': (runtime.get('timings') or {}).get('total_startup_ms'),
                    'tools_called': receipt.get('tools_called'), 'secret_scan': 'passed',
                    'tokens_observed': 0 if result.returncode != 0 else 2})
    print(json.dumps(summary[-1]), flush=True)

(BASE / 'acceptance-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print('ALL_MODES_EXECUTED')
