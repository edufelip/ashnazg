"""Run the packaged public v0.3.1 CLI through the native acceptance driver.

usage: wrapper.py cold|warm-offline
cold: fresh empty HOME, public acquisition. warm-offline: same HOME, CLI subtree under sandbox-exec.
"""
import contextlib, gzip, hashlib, json, os, re, stat, subprocess, sys, threading, time
from pathlib import Path
from unittest.mock import patch

BASE = Path('/private/tmp/ashnazg-public-0.3.1')
REPO = Path('/private/tmp/ashnazg-wt-public-0.3.1')
SOURCE = '509a74a329529f764f8fef955f45c04eed3e02cc'
FILES = BASE / 'assets'
PKG = BASE / 'pkg'
HOME = BASE / 'home'
STATE = HOME / 'Library/Application Support/ashnazg/native-mcp' / SOURCE
PROFILE = BASE / 'profile.sb'
POLICY_SHA = 'dfc42c68e4c06ddb51dbe133d3496d737f1f20cbe832694669222b94633d1347'
phase = sys.argv[1]
assert phase in ('cold', 'warm-offline')
offline = phase == 'warm-offline'

sys.path.insert(0, str(REPO / 'scripts'))
import native_cli_acceptance as driver

output = BASE / 'evidence' / phase
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
cli = [str(PKG / 'bin/ashnazg'), 'mcp']
args = ['--candidate-dir', str(FILES), '--binary', str(PKG / 'bin/ashnazg'),
        '--fixture-apk', str(PKG / 'android/fixture-instrumentation.apk'),
        '--home', str(HOME), '--output-dir', str(output), '--mode', 'eof', '--source-sha', SOURCE]
stdout, stderr = BASE / (phase + '.stdout'), BASE / (phase + '.stderr')
original_popen = subprocess.Popen
policy_launches = []


def launch(command, *positional, **options):
    if offline and command == cli:
        observed = hashlib.sha256(PROFILE.read_bytes()).hexdigest()
        assert observed == POLICY_SHA
        policy_launches.append({'binary': command[0], 'profile_sha256': observed})
        command = ['/usr/bin/sandbox-exec', '-f', str(PROFILE), *command]
    return original_popen(command, *positional, **options)


with stdout.open('x') as out, stderr.open('x') as err:
    with patch.object(driver.subprocess, 'Popen', side_effect=launch), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = driver.main(args)
if offline:
    assert len(policy_launches) == 1
    assert hashlib.sha256(PROFILE.read_bytes()).hexdigest() == POLICY_SHA
    (output / 'launch-policy.json').write_text(json.dumps(
        {'schema_version': 1, 'cli_launch_count': 1, 'launches': policy_launches,
         'profile_before_after_matches': True, 'controller_outside_policy': True},
        sort_keys=True, indent=2) + '\n')
done.set()
watcher.join(timeout=2)
assert not watcher.is_alive() and len(secrets) == 2
files = sorted(p for p in output.rglob('*') if p.is_file()) + [stdout, stderr]
for name in ('runtime.json', 'serial.log'):
    path = STATE / name
    assert path.is_file() and path.stat().st_size <= 16 * 1024 * 1024
    data = path.read_bytes()
    assert not any(v in data for v in secrets), 'runtime diagnostic contains a live credential'
    target = output / (name + '.gz' if name.endswith('.log') else name)
    target.write_bytes(gzip.compress(data, mtime=0) if name.endswith('.log') else data)
    files.append(target)
scanned = 0
for path in files:
    assert path.is_file() and path.stat().st_size <= 20 * 1024 * 1024
    data = path.read_bytes()
    assert not any(v in data for v in secrets), 'evidence contains a live credential'
    scanned += len(data)
secrets.clear()
(output / 'secret-scan.json').write_text(json.dumps(
    {'schema_version': 1, 'actual_session_tokens_observed': 2, 'tokens_written_or_printed': False,
     'matching_secret_found': False, 'files_scanned': len(files), 'bytes_scanned': scanned},
    sort_keys=True, indent=2) + '\n')
receipt = json.loads((output / 'receipt.json').read_text())
print(json.dumps({'phase': phase, 'exit_code': code, 'status': receipt['status'], 'error': receipt.get('error'),
                  'ready_seconds': receipt.get('ready', {}).get('elapsed_seconds'),
                  'startup_retries': receipt.get('startup_retries'),
                  'tools_called': receipt['tools_called'], 'secret_scan': 'passed'}), flush=True)
assert code == 0 and receipt['status'] == 'passed'
