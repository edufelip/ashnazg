"""Measure matched, owned Linux Android sessions through the public MCP tools."""

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from scripts.benchmark import McpProcess, assert_action_result, assert_fixture_count, mcp_snapshot_payload
from scripts.harness import DeviceHarness
from scripts.integration import find_node
from scripts.redroid import RedroidSupervisor, check_port_available
from scripts.ci_emulator import focused_application_window, readiness_status

CGROUP_ROOT = Path("/sys/fs/cgroup")
COUNTER = "dev.ashnazg.fixture:id/increment"
MARKER = "dev.ashnazg.fixture:id/frame_marker"
PACKAGE = "dev.ashnazg.fixture"
STATE_MARKER = "/data/local/tmp/ashnazg-session-benchmark"
MEMORY_LIMIT = 8 * 1024**3
INTERVAL = 0.2
DWELL = 3.0


def run(*args, timeout=15, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True, **kwargs).stdout.strip()


def write_json(path, record):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def cgroup_path(proc_cgroup):
    entries = [line[3:] for line in proc_cgroup.splitlines() if line.startswith("0::/")]
    if len(entries) != 1:
        raise ValueError("expected exactly one cgroup v2 membership")
    location = Path(entries[0])
    if location == Path("/") or ".." in location.parts:
        raise ValueError("runtime must be in an owned non-root cgroup")
    return CGROUP_ROOT / location.relative_to("/")


def memory_sample(group):
    sample = {"errors": {}, "memory_current_bytes": None, "memory_swap_current_bytes": None,
              "memory_stat": None}
    for field, key in (("memory.current", "memory_current_bytes"),
                       ("memory.swap.current", "memory_swap_current_bytes"),
                       ("memory.stat", "memory_stat")):
        try:
            value = (group / field).read_text()
            sample[key] = ({k: int(v) for k, v in (row.split() for row in value.splitlines())}
                           if field == "memory.stat" else int(value))
        except (OSError, ValueError) as exc:
            sample["errors"][field] = type(exc).__name__
    return sample


def scope_controls(group, cpus):
    controls = {name: (group / name).read_text().strip()
                for name in ("cpu.max", "memory.max", "memory.swap.max")}
    if (controls["cpu.max"] != "200000 100000" or controls["memory.max"] != str(MEMORY_LIMIT)
            or controls["memory.swap.max"] != "0"):
        raise RuntimeError("runtime cgroup resource controls did not match")
    allowed = set(map(int, cpus.split(",")))
    tasks = (group / "cgroup.threads").read_text().split()
    observed = set()
    for tid in tasks:
        try:
            affinity = os.sched_getaffinity(int(tid))
        except ProcessLookupError:
            continue
        if not affinity <= allowed:
            raise RuntimeError("runtime thread affinity exceeded the assigned CPUs")
        observed |= affinity
    if observed != allowed:
        raise RuntimeError("could not verify the host CPU availability of the runtime")
    controls["observed_host_cpus"] = sorted(observed)
    return controls


class MemoryWindows:
    def __init__(self, group, epoch_ns, record):
        self.group, self.epoch_ns, self.record = group, epoch_ns, record
        self.stop = threading.Event()

    def sample(self, label):
        elapsed = time.monotonic_ns() - self.epoch_ns
        self.record.append({"phase": label, "elapsed_ns": elapsed, **memory_sample(self.group)})

    @contextmanager
    def window(self, label):
        self.stop.clear()
        self.sample(f"{label}_start")

        def sample_periodically():
            while not self.stop.wait(INTERVAL):
                self.sample(label)

        thread = threading.Thread(target=sample_periodically)
        thread.start()
        try:
            yield
        finally:
            self.stop.set()
            thread.join()
            self.sample(f"{label}_end")


def frame_count(image, snapshot):
    from PIL import Image

    x0, y0, x1, y1 = find_node(snapshot, test_id=MARKER)["rect"]
    with Image.open(io.BytesIO(image), formats=["PNG"]) as png:
        if not (0 <= x0 <= x1 - 32 and 0 <= y0 < y1 and x1 <= png.width and y1 <= png.height):
            raise RuntimeError("invalid fixture marker bounds")
        pixels = png.convert("RGB")
        bits = []
        for cell in range(32):
            x = x0 + (2 * cell + 1) * (x1 - x0) // 64
            rgb = pixels.getpixel((x, (y0 + y1) // 2))
            bit = next((bit for bit, color in enumerate((16, 240))
                        if all(abs(channel - color) <= 12 for channel in rgb)), None)
            if bit is None:
                raise RuntimeError("fixture marker contains an unrecognized pixel")
            bits.append(bit)
    if any(a == b for a, b in zip(bits[:16], bits[16:])):
        raise RuntimeError("fixture marker complement mismatch")
    return sum(bit << index for index, bit in enumerate(bits[:16]))


def exercise_fixture(mcp, snapshot, record, decode=frame_count):
    assert_fixture_count(snapshot, 0, "initial MCP snapshot")
    before = mcp.screenshot("png")
    if decode(before, snapshot) != 0:
        raise RuntimeError("initial image disagrees with the fixture count")
    record["before_png_sha256"] = hashlib.sha256(before).hexdigest()
    started = time.monotonic_ns()
    result = mcp_snapshot_payload(mcp.call_tool("ui_act", {"action": "tap", "target": {"testId": COUNTER}}))
    tap_end = time.monotonic_ns()
    record["tap_ns"] = tap_end - started
    assert_action_result(result, 1, "MCP fixture tap")
    after_snapshot = result["snapshot"]
    if after_snapshot["snapshotId"] <= snapshot["snapshotId"]:
        raise RuntimeError("tap did not return a fresh semantic snapshot")
    captures = record["capture_attempts"] = []
    for _ in range(4):
        started = time.monotonic_ns()
        after = mcp.screenshot("png")
        capture_end = time.monotonic_ns()
        decoded = decode(after, after_snapshot)
        captures.append({"rpc_ns": capture_end - started, "elapsed_since_tap_ns": capture_end - tap_end,
                         "decoded_count": decoded, "sha256": hashlib.sha256(after).hexdigest()})
        if decoded == 1:
            record["tap_to_fresh_png_ns"] = capture_end - tap_end
            record["count_after"] = decoded
            return before, after
        if decoded != 0:
            break
    raise RuntimeError("no fresh fixture screenshot arrived after the single tap")


def probe(args):
    epoch_ns, output, group = args.epoch_ns, args.output, args.cgroup
    result = {"memory": [], "timings_ns": {}, "cgroup": str(group), "errors": []}
    memory = MemoryWindows(group, epoch_ns, result["memory"])
    harness = mcp = None
    stage = "guest_control_readback"
    try:
        result["scope_controls"] = scope_controls(group, args.cpus)
        harness = DeviceHarness(local_port=0)
        adb = harness.adb_cmd
        result["timings_ns"]["runtime_ready"] = args.ready_ns - epoch_ns
        if not args.warm:
            stage = "install"
            harness.install(args.agent, args.fixture)
            adb("shell", "touch", STATE_MARKER)
        else:
            adb("shell", "test", "-f", STATE_MARKER)
            for package in (PACKAGE, "dev.ashnazg.agent"):
                if not adb("shell", "pm", "path", package).stdout.startswith("package:"):
                    raise RuntimeError("warm guest is missing a retained package")
        stage = "workload_controls"
        animations = ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")
        for key in animations:
            adb("shell", "settings", "put", "global", key, "0")
        result["guest"] = {
            "fingerprint": adb("shell", "getprop", "ro.build.fingerprint").stdout.strip(),
            "abi": adb("shell", "getprop", "ro.product.cpu.abi").stdout.strip(),
            "api": adb("shell", "getprop", "ro.build.version.sdk").stdout.strip(),
            "display": adb("shell", "wm", "size").stdout.strip(),
            "density": adb("shell", "wm", "density").stdout.strip(),
            "animation_scales": {key: adb("shell", "settings", "get", "global", key).stdout.strip()
                                 for key in animations},
        }
        if (result["guest"]["api"] != "35" or result["guest"]["abi"] != "x86_64"
                or result["guest"]["display"] != "Physical size: 720x1280"
                or result["guest"]["density"] != "Physical density: 320"
                or any(float(value) != 0 for value in result["guest"]["animation_scales"].values())):
            raise RuntimeError("guest workload controls did not match the benchmark profile")
        stage = "agent_start"
        harness.start(args.agent, args.fixture, install=False)
        adb("shell", "am", "start", "-W", "-n", f"{PACKAGE}/.MainActivity", timeout=60)
        mcp = McpProcess(str(args.binary), harness)
        stage = "first_snapshot"
        first = mcp_snapshot_payload(mcp.call_tool("ui_snapshot", {}))
        snapshot_end = time.monotonic_ns()
        assert_fixture_count(first, 0, "initial MCP snapshot")
        find_node(first, test_id=MARKER)
        result["timings_ns"]["first_usable_snapshot"] = snapshot_end - epoch_ns
        stage = "idle"
        with memory.window("idle"):
            time.sleep(DWELL)
        stage = "interaction"
        with memory.window("interaction"):
            interaction_start = time.monotonic()
            before, after = exercise_fixture(mcp, first, result["timings_ns"])
            remaining = DWELL - (time.monotonic() - interaction_start)
            if remaining < 0:
                raise RuntimeError("fixture interaction exceeded the common memory window")
            time.sleep(remaining)
        (output / "before.png").write_bytes(before)
        (output / "after.png").write_bytes(after)
    except Exception as exc:
        detail = str(exc).replace(harness.token, "[redacted]") if harness else str(exc)
        result["errors"].append({"stage": stage, "type": type(exc).__name__, "detail": detail[:500]})
    finally:
        agent_shutdown_started = time.monotonic_ns()
        for component in (mcp, harness):
            if component is None:
                continue
            try:
                if component is mcp:
                    component.close()
                else:
                    component.cleanup()
            except Exception as exc:
                result["errors"].append({"stage": "probe_cleanup", "type": type(exc).__name__})
        result["agent_shutdown_ns"] = time.monotonic_ns() - agent_shutdown_started
        if not result["errors"]:
            try:
                harness.adb_cmd("shell", "sync", timeout=20)
            except (OSError, subprocess.SubprocessError) as exc:
                result["errors"].append({"stage": "guest_sync", "type": type(exc).__name__})
            with memory.window("post_agent"):
                time.sleep(DWELL)
            try:
                events = {key: int(value) for key, value in
                          (row.split() for row in (group / "memory.events").read_text().splitlines())}
                result["memory_events"] = events
                if any(events[name] for name in ("max", "oom", "oom_kill")):
                    result["errors"].append({"stage": "memory", "detail": "runtime hit the memory cap"})
            except (OSError, ValueError, KeyError) as exc:
                result["errors"].append({"stage": "memory", "type": type(exc).__name__})
        result["shutdown_started_ns"] = time.monotonic_ns()
        write_json(output / "probe.json", result)
    return int(bool(result["errors"]))


class BenchRedroid(RedroidSupervisor):
    def __init__(self, args, output, state, warm, epoch_ns):
        super().__init__(output_dir=output / "supervisor", adb_port=args.redroid_port, timeout=args.timeout,
                         command_timeout=240,
                         state_dir=state, fps=60, image_profile="android15-64only",
                         binder_bootstrap=args.binder_bootstrap)
        self.args, self.output, self.warm, self.epoch_ns = args, output, warm, epoch_ns

    def _command(self, args, **kwargs):
        if args[:3] == [self.docker_path, "run", "--detach"] and self.container_name in args:
            args = [*args[:3], "--cpus", "2", "--cpuset-cpus", self.args.cpus,
                    "--memory", str(MEMORY_LIMIT), "--memory-swap", str(MEMORY_LIMIT), *args[3:]]
        return super()._command(args, **kwargs)

    def _run_child(self, command):
        ready = time.monotonic_ns()
        cid = self.receipt["container_id"]
        identity = json.loads(run(self.docker_path, "inspect", cid))[0]
        if (identity["Id"] != cid or identity["Config"]["Labels"].get("org.ashnazg.redroid.run-id") != self.run_id
                or identity["HostConfig"]["Memory"] != MEMORY_LIMIT):
            raise RuntimeError("Docker benchmark scope identity or limit mismatch")
        group = cgroup_path(Path(f"/proc/{identity['State']['Pid']}/cgroup").read_text())
        if cid not in group.name:
            raise RuntimeError("Docker cgroup does not identify the owned container")
        write_json(self.output / "scope.json", {"path": str(group), "container_id": cid})
        return super()._run_child(probe_command(self.args, self.output, self.warm, self.epoch_ns, ready, group))


def probe_command(args, output, warm, epoch_ns, ready_ns, group):
    return [sys.executable, "-m", "scripts.agent_session_benchmark", "_probe",
            "--binary", str(args.binary), "--agent", str(args.agent), "--fixture", str(args.fixture),
            "--output", str(output), "--cgroup", str(group), "--epoch-ns", str(epoch_ns),
            "--ready-ns", str(ready_ns), "--cpus", args.cpus, *(["--warm"] if warm else [])]


def prepare_avd(template, directory):
    name = "AshnazgAgentBenchmark"
    target = directory / f"{name}.avd"
    target.mkdir(parents=True)
    config = (template / "config.ini").read_text().splitlines()
    controlled = {
        "AvdId": name, "avd.ini.displayname": name,
        "hw.cpu.ncore": "2", "hw.lcd.width": "720", "hw.lcd.height": "1280",
        "hw.lcd.density": "320", "hw.lcd.vsync": "60",
    }
    omitted = {"snapshot.present", "skin.path", "disk.dataPartition.path", "sdcard.path"}
    keep = [line for line in config if line.partition("=")[0].strip() not in controlled.keys() | omitted]
    (target / "config.ini").write_text("\n".join([*keep, *(f"{k}={v}" for k, v in controlled.items())]) + "\n")
    (directory / f"{name}.ini").write_text(f"avd.ini.encoding=UTF-8\npath={target}\n")
    return name


def avd_ready(process, adb, serial, timeout):
    deadline = time.monotonic() + timeout
    previous = None
    unlocked = False
    while time.monotonic() < deadline and process.poll() is None:
        try:
            if run(adb, "-s", serial, "shell", "getprop", "sys.boot_completed", timeout=3) == "1":
                if not unlocked:
                    run(adb, "-s", serial, "shell", "wm", "dismiss-keyguard", timeout=5)
                    unlocked = True
                input_state = run(adb, "-s", serial, "shell", "dumpsys", "input", timeout=5)
                windows = run(adb, "-s", serial, "shell", "dumpsys", "window", "-a", "windows", timeout=5)
                anr = run(adb, "-s", serial, "shell", "dumpsys", "activity", "lastanr", timeout=5)
                ready, _ = readiness_status(input_state, windows, anr)
                current = focused_application_window(input_state) if ready else None
                if current and current == previous:
                    return time.monotonic_ns()
                previous = current
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            previous = None
        time.sleep(1)
    if process.poll() is not None:
        raise RuntimeError(f"owned AVD exited before readiness (systemd-run exit {process.returncode})")
    raise TimeoutError("owned AVD did not reach the focused and drawn Android readiness gate")


def run_avd(args, output, directory, name, warm, epoch_ns):
    check_port_available(args.avd_port)
    check_port_available(args.avd_port + 1)
    serial = f"emulator-{args.avd_port}"
    unit = f"ashnazg-agent-benchmark-{uuid.uuid4().hex}.scope"
    env = {**os.environ, "ANDROID_AVD_HOME": str(directory)}
    ready = None
    with (output / "emulator.log").open("wb") as log:
        process = subprocess.Popen(
            ["systemd-run", "--user", "--scope", "--quiet", f"--unit={unit}",
             "-p", "MemoryAccounting=yes", "-p", f"MemoryMax={MEMORY_LIMIT}",
             "-p", "MemorySwapMax=0", "-p", "CPUQuota=200%",
             str(args.emulator), "-avd", name, "-port", str(args.avd_port), "-no-window", "-no-audio",
             "-no-snapshot", "-gpu", "swiftshader_indirect", "-accel", "on"],
            env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            ready = avd_ready(process, args.adb, serial, args.timeout)
            unit_path = run("systemctl", "--user", "show", unit, "--property=ControlGroup", "--value")
            group = cgroup_path(f"0::{unit_path}")
            if group.name != unit or (group / "memory.max").read_text().strip() != str(MEMORY_LIMIT):
                raise RuntimeError("AVD cgroup did not match the owned scope and memory limit")
            write_json(output / "scope.json", {"path": str(group), "unit": unit})
            return subprocess.run(probe_command(args, output, warm, epoch_ns, ready, group),
                                  env={**env, "ANDROID_SERIAL": serial}, timeout=240, check=False).returncode
        finally:
            if process.poll() is None:
                try:
                    if ready is not None:
                        run(args.adb, "-s", serial, "emu", "kill", timeout=5)
                        process.wait(timeout=20)
                finally:
                    if process.poll() is None:
                        # Stop only the unique scope created for this trial.
                        run("systemctl", "--user", "stop", unit, timeout=30)
                        process.wait(timeout=10)


def trial(args, parent, runtime, warm, avd_name):
    output = parent / runtime / ("warm" if warm else "cold")
    output.mkdir(parents=True)
    epoch = time.monotonic_ns()
    record = {"runtime": runtime, "warm": warm, "status": "failed", "memory_after_teardown": []}
    print(f"{parent.name} {runtime} {'warm' if warm else 'cold'} starting", flush=True)
    try:
        if runtime == "redroid":
            launch = BenchRedroid(args, output, parent / "redroid-data", warm, epoch)
            code = launch.run(["benchmark-fixture"])
        else:
            code = run_avd(args, output, parent / "avds", avd_name, warm, epoch)
        record["exit_code"] = code
        if code in (128 + signal.SIGHUP, 128 + signal.SIGINT, 128 + signal.SIGTERM):
            raise KeyboardInterrupt("runtime supervisor was interrupted")
    except (ValueError, OSError, subprocess.SubprocessError, RuntimeError) as exc:
        record["error"] = {"type": type(exc).__name__, "detail": str(exc)[:500]}
    finally:
        ended = time.monotonic_ns()
        record["elapsed_ns"] = ended - epoch
        if (output / "probe.json").is_file():
            result = record["probe"] = json.loads((output / "probe.json").read_text())
            record["runtime_shutdown_ns"] = ended - result["shutdown_started_ns"]
            record["shutdown_ns"] = result["agent_shutdown_ns"] + record["runtime_shutdown_ns"]
        if (output / "scope.json").is_file():
            scope = record["scope"] = json.loads((output / "scope.json").read_text())
            group = Path(scope["path"])
            for wait in (0, 1, 2):
                delay = max(0, wait - (time.monotonic_ns() - ended) / 1e9)
                time.sleep(delay)
                entry = {"offset_ns": time.monotonic_ns() - ended, "scope_removed": not group.exists()}
                if not entry["scope_removed"]:
                    entry.update(memory_sample(group))
                record["memory_after_teardown"].append(entry)
        if (record.get("exit_code") == 0 and "probe" in record
                and record["memory_after_teardown"] and record["memory_after_teardown"][-1]["scope_removed"]
                and not any(row["errors"] for row in record["probe"]["memory"])):
            record["status"] = "passed"
        write_json(output / "trial.json", record)
        print(f"{parent.name} {runtime} {'warm' if warm else 'cold'} {record['status']}", flush=True)
    return record["status"] == "passed"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    pair = sub.add_parser("pair")
    guest = sub.add_parser("_probe")
    for command in (pair, guest):
        for argument in ("binary", "agent", "fixture", "output"):
            command.add_argument(f"--{argument}", type=lambda value: Path(value).resolve(), required=True)
    for argument in ("emulator", "avd-template", "binder-bootstrap"):
        pair.add_argument(f"--{argument}", type=lambda value: Path(value).resolve(), required=True)
    pair.add_argument("--redroid-port", type=int, default=15583)
    pair.add_argument("--avd-port", type=int, default=5570)
    pair.add_argument("--pairs", type=int, choices=range(1, 11), default=2)
    pair.add_argument("--first", choices=("redroid", "avd"), default="redroid")
    pair.add_argument("--timeout", type=int, default=240)
    guest.add_argument("--epoch-ns", type=int, required=True)
    guest.add_argument("--ready-ns", type=int, required=True)
    guest.add_argument("--cgroup", type=Path, required=True)
    guest.add_argument("--cpus", required=True)
    guest.add_argument("--warm", action="store_true")
    args = parser.parse_args()
    if args.mode == "_probe":
        return probe(args)
    from PIL import Image  # Fail before allocating a guest if pixel verification is unavailable.
    del Image
    args.cpus = ",".join(map(str, sorted(os.sched_getaffinity(0))))
    if not (1 <= args.redroid_port <= 65535 and 5554 <= args.avd_port <= 5682 and args.avd_port % 2 == 0):
        parser.error("invalid loopback ports")
    args.adb = shutil.which("adb")
    if not args.adb or not (CGROUP_ROOT / "cgroup.controllers").is_file():
        parser.error("ADB and host cgroup v2 are required")
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / "manifest.json", {
        "host": platform.uname()._asdict(), "cpus": args.cpus, "memory_limit_bytes": MEMORY_LIMIT,
        "artifacts": {key: digest(getattr(args, key)) for key in ("binary", "agent", "fixture", "binder_bootstrap")},
        "avd_config_sha256": digest(args.avd_template / "config.ini"),
        "memory_interval_seconds": INTERVAL, "window_seconds": DWELL, "pairs_requested": args.pairs,
        "first_runtime": args.first,
    })
    # Preserve raw failures and clean up the owned runtime before returning on SIGTERM.
    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    succeeded = True
    for index in range(args.pairs):
        parent = args.output / f"pair-{index + 1}"
        name = prepare_avd(args.avd_template, parent / "avds")
        order = (args.first, "avd" if args.first == "redroid" else "redroid")
        if index % 2:
            order = order[::-1]
        for runtime in order:
            cold = trial(args, parent, runtime, False, name)
            warm = trial(args, parent, runtime, True, name) if cold else False
            succeeded = succeeded and warm
    return int(not succeeded)


if __name__ == "__main__":
    sys.exit(main())
