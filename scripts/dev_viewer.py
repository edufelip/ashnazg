#!/usr/bin/env python3
"""Open an explicitly selected Android device in scrcpy without owning its runtime."""
import argparse
import os
import platform
import shutil
import subprocess
import sys


def _run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, check=False, timeout=15, **kwargs)


def preflight(serial, adb, scrcpy):
    if platform.system() != "Linux":
        print("dev-viewer supports Linux hosts with external ADB; macOS native sessions do not expose host ADB.", file=sys.stderr)
        return 2
    if not serial or serial.startswith("-"):
        print("an explicit device serial is required", file=sys.stderr)
        return 2
    for executable in (adb, scrcpy):
        if not shutil.which(executable):
            print(f"required executable not found: {executable}", file=sys.stderr)
            return 2
    for executable, flag in ((adb, "version"), (scrcpy, "--version")):
        result = _run([executable, flag])
        # adb reports version with `version`; scrcpy prints its version as well.
        if result.returncode != 0 or not result.stdout.strip():
            print(f"could not verify {executable} version: {result.stderr.strip()}", file=sys.stderr)
            return 2
    result = _run([adb, "devices"])
    if result.returncode:
        print(f"adb devices failed: {result.stderr.strip()}", file=sys.stderr)
        return 2
    states = {line.split("\t", 1)[0]: line.split("\t", 1)[1].strip()
              for line in result.stdout.splitlines() if "\t" in line}
    if states.get(serial) != "device":
        print(f"selected serial is not connected and authorized: {serial}", file=sys.stderr)
        return 2
    return 0


def launch(serial, scrcpy, adb="adb"):
    env = os.environ.copy()
    env["ADB"] = adb
    return subprocess.run([scrcpy, "--serial", serial], env=env, check=False).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True, help="exact serial shown by adb devices")
    parser.add_argument("--adb", default="adb", help="external ADB executable")
    parser.add_argument("--scrcpy", default="scrcpy", help="scrcpy executable")
    args = parser.parse_args(argv)
    status = preflight(args.serial, args.adb, args.scrcpy)
    if status:
        return status
    return launch(args.serial, args.scrcpy, args.adb)


if __name__ == "__main__":
    raise SystemExit(main())
