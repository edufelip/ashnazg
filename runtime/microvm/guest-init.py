#!/usr/bin/python3
"""PID 1: own the guest's services, clean them up, and power off without exiting."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, '/usr/local/lib/ashnazg')
from scripts.linux_binder import ensure_nodes, link_nodes

SUPERVISOR = [
    '/usr/bin/python3', '/usr/local/lib/ashnazg/scripts/redroid.py', 'run',
    '--output-dir', '/var/lib/ashnazg/redroid', '--adb-port', '5555', '--timeout', '600',
    '--command-timeout', '0', '--', '/usr/bin/python3', '/usr/local/bin/guest_bridge.py',
]


class RequestedShutdown(Exception):
    def __init__(self, signum): self.signum = signum


def mount(source: str, target: str, filesystem: str, options: str = '') -> None:
    os.makedirs(target, exist_ok=True)
    args = ['/bin/mount', '-t', filesystem]
    if options: args.extend(('-o', options))
    result = subprocess.run([*args, source, target], check=False, timeout=10)
    if result.returncode and not os.path.ismount(target):
        raise RuntimeError(f'mount failed for {target}')


def stop_child(process, timeout):
    if process is None or process.poll() is not None: return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def boot_log_tail(path: str, max_bytes: int = 8192) -> str:
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - max_bytes))
            return stream.read(max_bytes).decode('utf-8', 'replace').strip()
    except OSError:
        return 'startup log unavailable'


def main() -> int:
    if os.getpid() != 1:
        raise RuntimeError('guest-init must run as PID 1')
    # The kernel gives PID 1 a minimal environment. Service children such as
    # containerd, runc and the Docker/ADB CLIs are resolved through PATH.
    os.environ['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    network = docker = child = None
    def stop(signum, _frame): raise RequestedShutdown(signum)
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM): signal.signal(number, stop)
    try:
        mount('proc', '/proc', 'proc')
        mount('sysfs', '/sys', 'sysfs')
        mount('devtmpfs', '/dev', 'devtmpfs')
        mount('devpts', '/dev/pts', 'devpts', 'gid=5,mode=620')
        mount('tmpfs', '/run', 'tmpfs', 'mode=0755,nosuid,nodev')
        mount('cgroup2', '/sys/fs/cgroup', 'cgroup2', 'nsdelegate')
        mount('binder', '/dev/binderfs', 'binder')
        ensure_nodes(Path('/dev/binderfs'))
        link_nodes(Path('/dev/binderfs'))
        os.makedirs('/var/lib/docker', exist_ok=True)
        # A fresh kernel starts both links administratively down. DHCP does not
        # raise them, and loopback is required by the guest's ADB bridge.
        for interface in ('lo', 'eth0'):
            subprocess.run(['/sbin/ip', 'link', 'set', 'dev', interface, 'up'],
                           check=True, timeout=10)
        network = subprocess.Popen(['/sbin/udhcpc', '-i', 'eth0', '-n', '-q', '-t', '5'])
        if network.wait(timeout=30): raise RuntimeError('guest DHCP failed')
        with open('/var/log/dockerd.log', 'ab') as log:
            docker = subprocess.Popen(['/usr/bin/dockerd', '--host=unix:///run/docker.sock',
                '--storage-driver=overlay2', '--iptables=true', '--bridge=docker0',
                '--userland-proxy-path=/usr/bin/docker-proxy'],
                stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 60
        while True:
            if docker.poll() is not None:
                raise RuntimeError('Docker daemon exited before readiness: ' + boot_log_tail('/var/log/dockerd.log'))
            try:
                result = subprocess.run(['/usr/bin/docker', 'info', '--format', '{{.ServerVersion}}'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False)
                if result.returncode == 0: break
            except subprocess.TimeoutExpired: pass
            if time.monotonic() >= deadline: raise RuntimeError('Docker readiness exceeded 60 seconds')
            time.sleep(0.5)
        environment = os.environ.copy()
        environment['PYTHONPATH'] = '/usr/local/lib/ashnazg'
        child = subprocess.Popen(SUPERVISOR, env=environment)
        while True:
            try:
                return child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if docker.poll() is not None: raise RuntimeError('Docker exited during Android operation')
                while True:
                    try: candidate = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    except ChildProcessError: break
                    if candidate is None or candidate.si_pid == 0 or candidate.si_pid == child.pid: break
                    os.waitpid(candidate.si_pid, os.WNOHANG)
    except RequestedShutdown as request:
        return 128 + request.signum
    finally:
        for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM): signal.signal(number, signal.SIG_IGN)
        try:
            stop_child(child, 30)  # Allow the Android supervisor to remove its owned resources.
        finally:
            try: stop_child(docker, 10)
            finally: stop_child(network, 3)


def poweroff() -> None:
    if os.getpid() != 1: raise RuntimeError('only guest PID 1 may power off')
    os.sync()
    subprocess.run(['/sbin/poweroff', '-f'], check=True, timeout=10)
    # A successful reboot syscall does not return. If shutdown is deferred, PID 1
    # must remain alive rather than panic the kernel by exiting.
    while True: signal.pause()


def run_init() -> int:
    if os.getpid() != 1:
        print('guest-init must run as PID 1', file=sys.stderr)
        return 1
    try:
        code = main()
        print(f'Ashnazg guest services exited with status {code}; powering off', flush=True)
    except BaseException as error:
        print(f'Ashnazg guest startup failed: {error}', file=sys.stderr, flush=True)
    try: poweroff()
    except Exception as error:
        print(f'Guest poweroff failed: {error}; retaining PID 1 for host recovery', file=sys.stderr, flush=True)
        while True: signal.pause()
    return 1  # Unreachable while PID 1 owns the VM.


if __name__ == '__main__':
    raise SystemExit(run_init())
