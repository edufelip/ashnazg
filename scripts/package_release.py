#!/usr/bin/env python3
"""Assemble a deterministic Ashnazg release archive from explicit build outputs."""

import argparse
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path

VERSION = "0.3.0"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ARCHIVE_FILES = {
    "host_binary": "bin/ashnazg",
    "agent_apk": "android/agent-instrumentation.apk",
    "fixture_apk": "android/fixture-instrumentation.apk",
    "license_file": "LICENSE",
    "setup_file": "README-SETUP.md",
    "third_party_notices": "THIRD-PARTY-NOTICES.tar.xz",
}


def package_release(*, version, source_sha, platform, output, host_binary,
                    agent_apk, fixture_apk, license_file, setup_file, third_party_notices, vm_helper=None):
    if version != VERSION:
        raise ValueError(f"version must match this package release: {VERSION}")
    if not SHA_RE.fullmatch(source_sha):
        raise ValueError("source must be a full 40-character lowercase commit SHA")
    if platform not in {"linux-x86_64", "linux-aarch64", "macos-x86_64", "macos-aarch64"}:
        raise ValueError("platform must identify the actual supported OS and architecture")
    if platform.startswith("linux-") and vm_helper is not None:
        raise ValueError("Linux packages must not include a VM helper")
    if platform == "macos-x86_64" and vm_helper is not None:
        raise ValueError("VM helper is only supported for macos-aarch64 packages")
    if platform == "macos-aarch64" and vm_helper is None:
        raise ValueError("macos-aarch64 packages require a VM helper")

    output = Path(output)
    payload = {}
    for argument, archive_name in ARCHIVE_FILES.items():
        source = Path(locals()[argument])
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(f"required regular file for {argument}: {source}")
        if source.resolve() == output.resolve():
            raise ValueError(f"output cannot replace input file: {source}")
        payload[archive_name] = source.read_bytes()

    if vm_helper is not None:
        helper = Path(vm_helper)
        if not helper.is_file() or helper.is_symlink():
            raise FileNotFoundError(f"required regular file for vm_helper: {helper}")
        if not helper.stat().st_mode & 0o111:
            raise ValueError(f"VM helper must be executable: {helper}")
        if helper.resolve() == output.resolve():
            raise ValueError(f"output cannot replace input file: {helper}")
        payload["bin/ashnazg-vm"] = helper.read_bytes()

    files = {
        name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for name, content in sorted(payload.items())
    }
    manifest = {
        "name": "ashnazg",
        "version": version,
        "source_sha": source_sha,
        "platform": platform,
        "files": files,
    }
    payload["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    payload["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n"
        for name, content in sorted(payload.items())
    ).encode()

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in sorted(payload.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            executable = name in {"bin/ashnazg", "bin/ashnazg-vm"}
            info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--output", required=True)
    for arg in ARCHIVE_FILES:
        parser.add_argument("--" + arg.replace("_", "-"), required=True)
    parser.add_argument("--vm-helper")
    args = vars(parser.parse_args())
    package_release(**args)


if __name__ == "__main__":
    main()
