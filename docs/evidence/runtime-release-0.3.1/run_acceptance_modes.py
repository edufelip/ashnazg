import gzip
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import time

BASE = Path('/private/tmp/ashnazg-v0.3.1-run')
REPO = Path('/private/tmp/ashnazg-v0.3.1-acceptance')
SOURCE = '509a74a329529f764f8fef955f45c04eed3e02cc'
FILES = BASE / 'candidate'
HOME = BASE / 'prepublication-home'
STATE = HOME / 'Library/Application Support/ashnazg/native-mcp' / SOURCE
PYTHON = '/opt/homebrew/bin/python3'
BINARY = BASE / 'mac-package' / 'bin' / 'ashnazg'
FIXTURE_APK = FILES / 'ashnazg-fixture-v0.3.1.apk'
DIAG = BASE / 'diagnostics'
MODES = ('eof', 'sigterm', 'sigterm-inflight')
SETTLE_SECONDS = 30
MAX_ATTEMPTS = 4

results = []
for mode in MODES:
    summary = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            print(json.dumps({'mode': mode, 'attempt': attempt, 'settling': SETTLE_SECONDS}), flush=True)
            time.sleep(SETTLE_SECONDS)
        output = BASE / 'evidence' / mode
        if output.exists():
            if summary is None:
                shutil.move(str(output), str(DIAG / f'{mode}-failed-attempt-{attempt - 1}'))
            else:
                shutil.rmtree(output)
        stdout, stderr = BASE / (mode + '.stdout'), BASE / (mode + '.stderr')
        for path in (stdout, stderr):
            if path.exists():
                path.unlink()
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
                '--binary', str(BINARY), '--fixture-apk', str(FIXTURE_APK),
                '--home', str(HOME), '--output-dir', str(output), '--mode', mode, '--source-sha', SOURCE]
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
        runtime = json.loads((output / 'runtime.json').read_text())
        summary = {'mode': mode, 'attempt': attempt, 'exit_code': result.returncode,
                   'status': receipt['status'], 'error': receipt.get('error'),
                   'ready_seconds': receipt.get('ready', {}).get('elapsed_seconds'),
                   'handshake_seconds': receipt.get('handshake', {}).get('elapsed_seconds'),
                   'helper_startup_ms': runtime.get('timings', {}).get('total_startup_ms'),
                   'tools_called': receipt['tools_called'], 'secret_scan': 'passed'}
        print(json.dumps(summary), flush=True)
        if result.returncode == 0 and receipt['status'] == 'passed':
            break
        summary = None
        shutil.move(str(output), str(DIAG / f'{mode}-failed-attempt-{attempt}'))
    assert summary is not None, f'mode {mode} never passed in {MAX_ATTEMPTS} attempts'
    results.append(summary)

print(json.dumps({'all_modes_passed': True, 'results': results}))
