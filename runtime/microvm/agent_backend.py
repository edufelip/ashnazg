"""Fixed, owner-scoped ADB backend for the guest Android agent."""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import socket
import subprocess
import threading
import time


ADB = "/usr/bin/adb"
SERIAL = "127.0.0.1:5555"
LOCAL_FORWARD = "tcp:17777"
REMOTE_FORWARD = "tcp:7777"
AGENT_PACKAGE = "dev.ashnazg.agent"
INSTRUMENTATION = "dev.ashnazg.agent/dev.ashnazg.agent.AgentInstrumentation"
MAX_COMMAND_OUTPUT = 64 * 1024
MAX_PING_FRAME = 4096
COMMAND_TERMINATE_SECONDS = 0.5
CLOSE_SECONDS = 10.0
_PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\Z", re.ASCII)
_JAVA_CLASS = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*\Z", re.ASCII)


class BackendError(RuntimeError):
    """A safe, generic backend failure with no subprocess details."""


def _deadline_value(deadline, clock):
    try:
        value = float(deadline)
        now = float(clock())
    except (TypeError, ValueError, OverflowError, OSError):
        raise BackendError("ADB operation failed") from None
    if not math.isfinite(value) or not math.isfinite(now) or value <= now:
        raise BackendError("ADB operation failed")
    return value


class GuestAdbBackend:
    """Own one fixed guest ADB forward and its authenticated instrumentation."""

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._operation_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._cancel = threading.Event()
        self._closed = False
        self._closing = False
        self._close_done = threading.Event()
        self._close_failed = False
        self._processes = set()
        self._instrumentation = None
        self._prepared = False
        self._ready = False
        self._forward_owned = False
        self._started_agent = False

    def __repr__(self):
        with self._state_lock:
            return ("GuestAdbBackend(prepared=%s, ready=%s, closed=%s)" %
                    (self._prepared, self._ready, self._closed))

    def _remaining(self, deadline, *, cleanup=False):
        try:
            clock = time.monotonic if cleanup else self._clock
            remaining = deadline - float(clock())
        except (TypeError, ValueError, OverflowError, OSError):
            raise BackendError("ADB operation failed") from None
        if not math.isfinite(remaining) or remaining <= 0:
            raise BackendError("ADB operation failed")
        return remaining

    def _track(self, process, *, cleanup=False):
        with self._state_lock:
            if (self._closed or self._cancel.is_set()) and not cleanup:
                self._stop_process(process)
                raise BackendError("ADB operation failed")
            self._processes.add(process)

    def _untrack(self, process):
        with self._state_lock:
            self._processes.discard(process)

    def _stop_process(self, process):
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=COMMAND_TERMINATE_SECONDS)
                except (subprocess.TimeoutExpired, TimeoutError, OSError):
                    process.kill()
                    try:
                        process.wait(timeout=COMMAND_TERMINATE_SECONDS)
                    except (subprocess.TimeoutExpired, TimeoutError, OSError):
                        return process.poll() is not None
        except (OSError, ProcessLookupError):
            return process.poll() is not None
        try:
            return process.poll() is not None
        except (OSError, ProcessLookupError):
            return False

    def _run(self, argv, *, deadline, stdin=None, cleanup=False):
        """Run one finite command, reading a single bounded combined output pipe."""
        if not cleanup and self._cancel.is_set():
            raise BackendError("ADB operation failed")
        self._remaining(deadline, cleanup=cleanup)
        try:
            process = subprocess.Popen(
                argv, stdin=(stdin if stdin is not None else subprocess.DEVNULL),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True,
                shell=False,
            )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise BackendError("ADB operation failed") from None
        try:
            self._track(process, cleanup=cleanup)
        except BackendError:
            try:
                process.stdout.close()
            except (AttributeError, OSError):
                pass
            raise
        output = bytearray()
        selector = selectors.DefaultSelector()
        try:
            pipe = process.stdout
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ)
            eof = False
            while not eof or process.poll() is None:
                if not cleanup and self._cancel.is_set():
                    raise BackendError("ADB operation failed")
                remaining = self._remaining(deadline, cleanup=cleanup)
                events = selector.select(min(remaining, 0.05))
                for key, _ in events:
                    chunk = os.read(key.fd, min(8192, MAX_COMMAND_OUTPUT + 1 - len(output)))
                    if not chunk:
                        eof = True
                        try:
                            selector.unregister(key.fileobj)
                        except Exception:
                            pass
                        break
                    output.extend(chunk)
                    if len(output) > MAX_COMMAND_OUTPUT:
                        raise BackendError("ADB operation failed")
                if process.poll() is not None and not events and not eof:
                    # Drain the pipe in bounded chunks after process exit.
                    try:
                        chunk = os.read(pipe.fileno(), min(8192, MAX_COMMAND_OUTPUT + 1 - len(output)))
                    except BlockingIOError:
                        chunk = None
                    if chunk == b"":
                        eof = True
                    elif chunk:
                        output.extend(chunk)
                        if len(output) > MAX_COMMAND_OUTPUT:
                            raise BackendError("ADB operation failed")
            code = process.wait(timeout=max(0.01, min(self._remaining(deadline, cleanup=cleanup), 0.2)))
            if code != 0:
                raise BackendError("ADB operation failed")
            return bytes(output)
        except BackendError:
            self._stop_process(process)
            raise
        except (OSError, TimeoutError, subprocess.TimeoutExpired, ValueError):
            self._stop_process(process)
            raise BackendError("ADB operation failed") from None
        finally:
            selector.close()
            try:
                process.stdout.close()
            except (AttributeError, OSError):
                pass
            self._untrack(process)

    def _adb(self, *args, deadline, stdin=None, cleanup=False):
        return self._run([ADB, "-s", SERIAL, *args], deadline=deadline,
                         stdin=stdin, cleanup=cleanup)

    def _acquire_operation(self, deadline):
        _deadline_value(deadline, self._clock)
        if not self._operation_lock.acquire(blocking=False):
            raise BackendError("ADB operation failed")
        with self._state_lock:
            if self._closed or self._cancel.is_set():
                self._operation_lock.release()
                raise BackendError("ADB operation failed")

    def _release_operation(self):
        self._operation_lock.release()

    def _forward_rows(self, deadline, *, cleanup=False):
        raw = self._adb("forward", "--list", deadline=deadline, cleanup=cleanup)
        rows = []
        try:
            for line in raw.decode("ascii", "strict").splitlines():
                fields = line.split()
                if len(fields) != 3:
                    raise ValueError()
                rows.append(tuple(fields))
        except (UnicodeError, ValueError):
            raise BackendError("ADB operation failed") from None
        return rows

    def _check_unmutated_guest(self, deadline):
        state = self._adb("get-state", deadline=deadline).decode("ascii", "strict").strip()
        if state != "device":
            raise BackendError("ADB operation failed")
        rows = self._forward_rows(deadline)
        if any(local == LOCAL_FORWARD for _, local, _ in rows):
            raise BackendError("ADB operation failed")
        raw = self._adb("shell", "-T", "ps", "-A", deadline=deadline).decode("utf-8", "strict")
        if any(any(field == AGENT_PACKAGE or field.startswith(AGENT_PACKAGE + ":")
                       for field in line.split()) for line in raw.splitlines()):
            raise BackendError("ADB operation failed")

    @staticmethod
    def _install_args(package, size):
        args = ["shell", "-T", "cmd", "package", "install", "-r"]
        if package is not None:
            args.extend(["--pkg", package])
        args.extend(["-S", str(size), "-"])
        return args

    def _install(self, staged_apk, *, package, deadline):
        if type(getattr(staged_apk, "size", None)) is not int or staged_apk.size <= 0:
            raise BackendError("ADB operation failed")
        output = self._adb(*self._install_args(package, staged_apk.size),
                           deadline=deadline, stdin=staged_apk.fileno())
        try:
            text = output.decode("utf-8", "strict")
        except UnicodeError:
            raise BackendError("ADB operation failed") from None
        if text not in ("Success", "Success\n", "Success\r\n"):
            raise BackendError("ADB operation failed")

    def prepare_agent(self, staged_apk, rpc_token, *, deadline):
        self._acquire_operation(deadline)
        failed = False
        try:
            if (not isinstance(rpc_token, str) or not re.fullmatch(r"[0-9a-f]{64}", rpc_token)
                    or self._prepared):
                raise BackendError("ADB operation failed")
            self._check_unmutated_guest(deadline)
            self._install(staged_apk, package=AGENT_PACKAGE, deadline=deadline)
            self._adb("forward", "--no-rebind", LOCAL_FORWARD, REMOTE_FORWARD, deadline=deadline)
            self._forward_owned = True
            try:
                process = subprocess.Popen(
                    [ADB, "-s", SERIAL, "shell", "-T", "am", "instrument", "-w", "-r",
                     "-e", "token", rpc_token, INSTRUMENTATION],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True, shell=False,
                )
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    raise
                raise BackendError("ADB operation failed") from None
            self._started_agent = True
            self._instrumentation = process
            self._track(process)
            self._prepared = True
            self._ping(rpc_token, deadline)
            self._ready = True
            return 17777
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            self._ready = False
            failed = True
        finally:
            self._release_operation()
        if failed:
            try:
                self.close()
            except BackendError:
                pass
            raise BackendError("ADB operation failed") from None

    def _ping(self, token, deadline):
        request = {"jsonrpc": "2.0", "id": 1, "method": "ping", "token": token}
        frame = json.dumps(request, separators=(",", ":")).encode("ascii") + b"\n"
        if len(frame) > MAX_PING_FRAME:
            raise BackendError("ADB operation failed")
        while True:
            if self._cancel.is_set() or self._closed:
                raise BackendError("ADB operation failed")
            process = self._instrumentation
            if process is None or process.poll() is not None:
                raise BackendError("ADB operation failed")
            remaining = self._remaining(deadline)
            connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                connection.settimeout(remaining)
                connection.connect(("127.0.0.1", 17777))
                connection.settimeout(self._remaining(deadline))
                connection.sendall(frame)
                response = bytearray()
                while len(response) <= MAX_PING_FRAME:
                    connection.settimeout(self._remaining(deadline))
                    part = connection.recv(min(1024, MAX_PING_FRAME + 1 - len(response)))
                    if not part:
                        break
                    response.extend(part)
                    if b"\n" in response:
                        break
                if len(response) > MAX_PING_FRAME or b"\n" not in response:
                    raise ValueError()
                raw, trailing = bytes(response).split(b"\n", 1)
                if trailing:
                    raise ValueError()
                value = json.loads(raw.decode("utf-8", "strict"))
                result = value.get("result") if isinstance(value, dict) else None
                if (value.get("jsonrpc") != "2.0" or value.get("id") != 1 or
                        "error" in value or not isinstance(result, dict) or
                        type(result.get("apiLevel")) is not int or result["apiLevel"] < 34):
                    raise ValueError()
                return
            except (OSError, TimeoutError, UnicodeError, ValueError, json.JSONDecodeError):
                pass
            finally:
                connection.close()
            if self._remaining(deadline) <= 0:
                raise BackendError("ADB operation failed") from None
            if self._cancel.wait(min(0.05, self._remaining(deadline))):
                raise BackendError("ADB operation failed")

    def _resolve_launcher(self, package, deadline):
        if not isinstance(package, str) or len(package) > 255 or not package.isascii() or not _PACKAGE.fullmatch(package):
            raise BackendError("ADB operation failed")
        common = ["shell", "-T", "cmd", "package"]
        action = ["-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER",
                  "-p", package]
        query = self._adb(*common, "query-activities", "--components", *action,
                          deadline=deadline).decode("utf-8", "strict")
        resolve = self._adb(*common, "resolve-activity", "--brief", *action,
                            deadline=deadline).decode("utf-8", "strict")
        def components(output):
            found = []
            for line in output.splitlines():
                line = line.strip()
                if not line or line.startswith("priority=") or line.startswith("Non-Data"):
                    continue
                if "/" not in line:
                    raise BackendError("ADB operation failed")
                found.append(line)
            return found
        def normalize(component):
            comp_package, class_name = component.split("/", 1)
            if comp_package != package:
                raise BackendError("ADB operation failed")
            if class_name.startswith("."):
                class_name = package + class_name
            if not _JAVA_CLASS.fullmatch(class_name) or not class_name.startswith(package + "."):
                raise BackendError("ADB operation failed")
            return package + "/" + class_name

        matches = components(query)
        resolved = components(resolve)
        if len(matches) != 1 or len(resolved) != 1:
            raise BackendError("ADB operation failed")
        normalized = normalize(matches[0])
        if normalize(resolved[0]) != normalized:
            raise BackendError("ADB operation failed")
        return normalized

    def install(self, staged_apk, *, package, deadline):
        self._acquire_operation(deadline)
        try:
            if not self._ready or not self.is_alive():
                raise BackendError("ADB operation failed")
            if package is not None and (not isinstance(package, str) or len(package) > 255 or
                                        not package.isascii() or not _PACKAGE.fullmatch(package)):
                raise BackendError("ADB operation failed")
            self._install(staged_apk, package=package, deadline=deadline)
            if package is not None:
                component = self._resolve_launcher(package, deadline)
                output = self._adb("shell", "-T", "am", "start", "-W", "-n", component,
                                   deadline=deadline).decode("utf-8", "strict")
                statuses = re.findall(r"^Status:\s*(.*?)\s*$", output, re.MULTILINE)
                if len(statuses) != 1 or statuses[0] != "ok" or re.search(
                        r"(?im)^\s*(?:error|exception)\b|^Status:\s*(?!ok\s*$)", output):
                    raise BackendError("ADB operation failed")
            if not self.is_alive():
                raise BackendError("ADB operation failed")
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise BackendError("ADB operation failed") from None
        finally:
            self._release_operation()

    def is_alive(self):
        with self._state_lock:
            try:
                return bool(self._prepared and self._ready and not self._closed and
                            self._instrumentation is not None and self._instrumentation.poll() is None)
            except (OSError, ProcessLookupError):
                return False

    def close(self):
        with self._state_lock:
            if self._closing:
                waiter = True
            elif self._closed:
                return
            else:
                self._closing = True
                self._closed = True
                self._ready = False
                self._cancel.set()
                waiter = False
        if waiter:
            if not self._close_done.wait(CLOSE_SECONDS):
                raise BackendError("ADB cleanup failed") from None
            return

        failed = False
        end = time.monotonic() + CLOSE_SECONDS
        try:
            with self._state_lock:
                processes = list(self._processes)
            for process in processes:
                if not self._stop_process(process):
                    failed = True
            # Let the cancelled owner finish recording any mutation that raced with
            # shutdown before deciding which exact resources this close may remove.
            remaining = end - time.monotonic()
            if remaining <= 0:
                failed = True
            elif not self._operation_lock.acquire(timeout=min(remaining, 2.0)):
                failed = True
            else:
                self._operation_lock.release()
            with self._state_lock:
                processes = list(self._processes)
                instrumentation = self._instrumentation
            for process in processes:
                if not self._stop_process(process):
                    failed = True
            if instrumentation is not None:
                with self._state_lock:
                    self._processes.discard(instrumentation)
                if self._started_agent and time.monotonic() < end:
                    cleanup_deadline = min(end, time.monotonic() + 2.0)
                    try:
                        self._adb("shell", "-T", "am", "force-stop", AGENT_PACKAGE,
                                  deadline=cleanup_deadline, cleanup=True)
                    except BackendError:
                        failed = True
            if self._forward_owned and time.monotonic() < end:
                cleanup_deadline = min(end, time.monotonic() + 2.0)
                try:
                    rows = self._forward_rows(cleanup_deadline, cleanup=True)
                    exact = [row for row in rows if row == (SERIAL, LOCAL_FORWARD, REMOTE_FORWARD)]
                    if exact:
                        self._adb("forward", "--remove", LOCAL_FORWARD,
                                  deadline=cleanup_deadline, cleanup=True)
                    elif any(row[1] == LOCAL_FORWARD for row in rows):
                        failed = True
                except BackendError:
                    failed = True
        finally:
            with self._state_lock:
                self._closing = False
                self._close_failed = failed
                self._close_done.set()
        if failed:
            raise BackendError("ADB cleanup failed") from None
