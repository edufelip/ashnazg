#!/usr/bin/env python3
"""Prepare an explicitly selected Android device for real-app development."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


class DevAppError(RuntimeError):
    pass


def run_adb(adb: str, serial: str, arguments: list[str]) -> str:
    result = subprocess.run([adb, "-s", serial, *arguments], check=True,
                            text=True, capture_output=True, timeout=180)
    return result.stdout


def require_online_serial(adb: str, serial: str) -> None:
    if not serial or not serial.strip():
        raise DevAppError("an explicit --serial or ANDROID_SERIAL is required")
    listing = subprocess.run([adb, "devices", "-l"], check=True, text=True,
                             capture_output=True, timeout=15).stdout
    online = {line.split()[0] for line in listing.splitlines()[1:]
              if len(line.split()) > 1 and line.split()[1] == "device"}
    if serial not in online:
        raise DevAppError(f"selected serial {serial!r} is not online")


def install_apk(adb: str, serial: str, apk: str) -> None:
    path = Path(apk)
    if not path.is_file():
        raise DevAppError(f"APK does not exist: {path}")
    require_online_serial(adb, serial)
    run_adb(adb, serial, ["install", "-r", str(path.resolve())])


def setup_reverses(adb: str, serial: str, mappings: list[tuple[str, str]]) -> list[str]:
    require_online_serial(adb, serial)
    created: list[str] = []
    try:
        existing = subprocess.run([adb, "-s", serial, "reverse", "--list"], check=True,
                                  text=True, capture_output=True, timeout=15).stdout
        for host, device in mappings:
            if any(not value.startswith("tcp:") or not value[4:].isdigit() or not 1 <= int(value[4:]) <= 65535 for value in (host, device)):
                raise DevAppError("reverse mappings must use tcp:<port>")
            if any(line.split()[-2:] == [host, device] for line in existing.splitlines() if len(line.split()) >= 2):
                continue
            if any(line.split()[-2:-1] == [host] for line in existing.splitlines()):
                raise DevAppError(f"reverse endpoint {host} is already mapped to another port")
            run_adb(adb, serial, ["reverse", "--no-rebind", host, device])
            created.append(host)
            existing += f"\n{serial} {host} {device}\n"
        return created
    except Exception:
        for host in reversed(created):
            subprocess.run([adb, "-s", serial, "reverse", "--remove", host], check=False,
                           text=True, capture_output=True, timeout=15)
        raise


def acceptance_checklist(apk: str, serial: str) -> str:
    return f"""# Real app development acceptance

- app_selection: pending
- apk: {Path(apk).expanduser()}
- selected_serial: {serial}
- preserve_app_data: yes
- real_app_proof: not_claimed

## Preparation
- [ ] Confirm selected app/package, build variant, and expected launch behavior.
- [ ] Confirm APK provenance and record SHA-256 before installation.
- [ ] Confirm the explicitly selected ADB serial is the intended device.
- [ ] Install/update with `adb -s {serial} install -r <apk>`; do not uninstall or clear data.
- [ ] Start the app's developer server and record its command, host, port, and readiness.
- [ ] Add only required `adb reverse tcp:<port> tcp:<port>` mappings; record pre-existing mappings.

## Evidence
- [ ] Record device model, Android version, serial, app version/package, APK hash, and timestamp.
- [ ] Capture launch and core daily usability journey evidence, including observed failures.
- [ ] Record server logs and app/device logs with secrets removed.
- [ ] Verify app data remains present after update and record the result.
- [ ] Remove only reverse mappings created for this session; retain evidence and cleanup result.
- [ ] Report gaps explicitly. Do not describe fixture/native MCP checks as real-app acceptance.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", required=True)
    parser.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL"))
    parser.add_argument("--adb", default=os.environ.get("ASHNAZG_ADB") or shutil.which("adb") or "adb")
    parser.add_argument("--checklist", type=Path, help="write a new run checklist file (default: stdout)")
    parser.add_argument("--server-port", action="append", type=int, default=[], help="host and device localhost port to reverse")
    args = parser.parse_args(argv)
    if any(not 1 <= port <= 65535 for port in args.server_port):
        parser.error("--server-port must be between 1 and 65535")
    created = []
    try:
        if args.checklist and args.checklist.exists():
            raise DevAppError("checklist already exists; choose a new run-specific path")
        require_online_serial(args.adb, args.serial)
        install_apk(args.adb, args.serial, args.apk)
        if args.server_port:
            created = setup_reverses(args.adb, args.serial, [(f"tcp:{port}", f"tcp:{port}") for port in args.server_port])
        checklist = acceptance_checklist(args.apk, args.serial)
        checklist += "\ncreated_reverse_endpoints: " + (", ".join(created) or "none") + "\n"
        if args.checklist:
            args.checklist.parent.mkdir(parents=True, exist_ok=True)
            with args.checklist.open("x", encoding="utf-8") as output:
                output.write(checklist)
        else:
            print(checklist)
        return 0
    except (DevAppError, OSError, subprocess.SubprocessError) as exc:
        for endpoint in reversed(created):
            try:
                run_adb(args.adb, args.serial, ["reverse", "--remove", endpoint])
            except (OSError, subprocess.SubprocessError):
                pass
        print(f"dev-app: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
