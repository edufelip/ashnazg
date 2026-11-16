#!/usr/bin/env python3
"""Build a deterministic archive of Rust dependency license notices."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

if __package__:
    from scripts import runtime_sources
else:  # Support direct execution as `python scripts/rust_notices.py`.
    import runtime_sources


MAX_NOTICE_FILE_BYTES = 16 * 1024 * 1024
MAX_NOTICE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_NOTICE_FILES = 10_000
NOTICE_PREFIXES = ("license", "copying", "copyright", "notice", "authors")


def _safe_relative_path(value):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("unsafe Cargo license-file path")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("unsafe Cargo license-file path")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError("unsafe Cargo license-file path")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in part) for part in path.parts):
        raise ValueError("unsafe Cargo license-file path")
    return path.as_posix()


def _toml_string(value):
    value = value.strip()
    if len(value) < 2 or value[0] not in "\"'" or value[-1] != value[0]:
        raise ValueError("Cargo package license metadata must be a TOML string")
    if value[0] == "'":
        return value[1:-1]
    try:
        result = ast.literal_eval(value)
    except (SyntaxError, ValueError) as error:
        raise ValueError("invalid TOML string in Cargo package metadata") from error
    if not isinstance(result, str):
        raise ValueError("Cargo package license metadata must be a TOML string")
    return result


def _package_metadata(cargo_toml):
    """Read the normalized [package] string fields without requiring tomllib."""
    try:
        text = cargo_toml.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise ValueError("Cargo.toml is not UTF-8") from error
    section = None
    metadata = {"name": None, "version": None, "license": None, "license_file": None}
    keys = {"name": "name", "version": "version", "license": "license",
            "license-file": "license_file"}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "package" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in keys:
            metadata[keys[key]] = _toml_string(value)
    if not metadata["name"] or not metadata["version"]:
        raise ValueError("Cargo.toml [package] is missing name or version")
    if metadata["license_file"] is not None:
        metadata["license_file"] = _safe_relative_path(metadata["license_file"])
    return metadata


def _file_digest(path, expected_hash):
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError("notice candidate disappeared during collection") from error
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("notice candidate is not a regular file")
    if info.st_size > MAX_NOTICE_FILE_BYTES:
        raise ValueError("Rust notice candidate exceeds per-file size bound")
    data = path.read_bytes()
    if len(data) != info.st_size:
        raise ValueError("notice candidate changed while being read")
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_hash:
        raise ValueError("Cargo vendor file hash mismatch: " + path.name)
    return data, digest


def _notice_candidates(package_dir, metadata, checksum_data):
    checksums = json.loads(checksum_data.decode("utf-8"))["files"]
    selected = set()
    for root, dirs, files in os.walk(package_dir, followlinks=False):
        dirs.sort()
        files.sort()
        for dirname in dirs:
            child = Path(root) / dirname
            if child.is_symlink() or not child.is_dir():
                raise ValueError("Cargo vendor contains a link or non-directory")
        for filename in files:
            path = Path(root) / filename
            if path.is_symlink() or not path.is_file():
                raise ValueError("Cargo vendor contains a link or non-regular file")
            relative = path.relative_to(package_dir).as_posix()
            if filename.casefold().startswith(NOTICE_PREFIXES):
                selected.add(_safe_relative_path(relative))
    explicit = metadata["license_file"]
    if explicit is not None:
        candidate = package_dir.joinpath(*PurePosixPath(explicit).parts)
        selected.add(explicit)
        if not candidate.exists() and not candidate.is_symlink():
            raise ValueError("Cargo license-file does not exist: " + explicit)
    result = []
    total = 0
    for relative in sorted(selected):
        candidate = package_dir.joinpath(*PurePosixPath(relative).parts)
        if relative not in checksums:
            raise ValueError("Cargo license-file is absent from vendor checksum manifest: " + relative)
        expected_hash = checksums[relative]
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("invalid hash for Rust notice candidate")
        data, digest = _file_digest(candidate, expected_hash)
        total += len(data)
        if total > MAX_NOTICE_TOTAL_BYTES:
            raise ValueError("Rust notice candidates exceed aggregate size bound")
        result.append({"relative_path": relative, "data": data,
                       "size": len(data), "sha256": digest})
    return result


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _add_tar_file(archive, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.type = tarfile.REGTYPE
    info.pax_headers = {}
    archive.addfile(info, fileobj=io.BytesIO(data))


def build_rust_notices(vendor_dir, cargo_lock_path, output_path):
    """Validate a Cargo vendor tree and atomically create its notice archive."""
    output = Path(output_path)
    try:
        output.lstat()
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError("output already exists: " + str(output))
    if not output.parent.is_dir():
        raise FileNotFoundError("output directory does not exist: " + str(output.parent))

    cargo_lock = runtime_sources.validate_rust_vendor(vendor_dir, cargo_lock_path)
    lock_packages = runtime_sources._parse_cargo_lock(cargo_lock)
    vendor = Path(vendor_dir)
    packages = []
    tar_files = []
    retained_count = 0
    retained_total = 0
    seen = set()
    for package_dir in sorted(vendor.iterdir(), key=lambda item: item.name):
        cargo_toml = package_dir / "Cargo.toml"
        info = cargo_toml.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Cargo.toml must be a regular file")
        metadata = _package_metadata(cargo_toml.read_bytes())
        key = (metadata["name"], metadata["version"])
        if key not in lock_packages:
            # Non-registry vendor packages (for example path overrides) are not in scope.
            continue
        if key in seen:
            raise ValueError("Cargo vendor repeats a package identity")
        seen.add(key)
        checksum_path = package_dir / ".cargo-checksum.json"
        checksum_info = checksum_path.lstat()
        if not stat.S_ISREG(checksum_info.st_mode):
            raise ValueError("Cargo vendor checksum manifest must be a regular file")
        candidates = _notice_candidates(package_dir, metadata, checksum_path.read_bytes())
        notice_records = []
        for candidate in candidates:
            retained_count += 1
            retained_total += candidate["size"]
            if retained_count > MAX_NOTICE_FILES:
                raise ValueError("Rust notice archive exceeds file count bound")
            if retained_total > MAX_NOTICE_TOTAL_BYTES:
                raise ValueError("Rust notice candidates exceed aggregate size bound")
            archive_path = "rust-notices/{}/{}/{}".format(
                metadata["name"], metadata["version"], candidate["relative_path"])
            # Validate the entire mapped name, including lock-provided crate identities.
            if _safe_relative_path(archive_path) != archive_path:
                raise ValueError("unsafe Rust notice archive path")
            tar_files.append((archive_path, candidate["data"]))
            notice_records.append({"path": archive_path, "size": candidate["size"],
                                  "sha256": candidate["sha256"]})
        crate = {"name": metadata["name"], "version": metadata["version"],
                 "license": metadata["license"], "license_file": metadata["license_file"],
                 "notice_files": notice_records,
                 "notice_status": "included" if notice_records else "no_candidate"}
        packages.append(crate)

    if seen != set(lock_packages):
        raise ValueError("Cargo vendor registry package set changed during notice collection")
    packages.sort(key=lambda item: (item["name"], item["version"]))
    manifest = {
        "schema_version": 1,
        "scope": {
            "description": "Text collection only; this is not legal clearance.",
            "corresponding_source": "Full crate source is published in the source sidecar.",
        },
        "crates": packages,
    }
    tar_files.sort(key=lambda item: item[0])
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + output.name + ".", suffix=".tmp",
                                                   dir=str(output.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            with tarfile.open(fileobj=stream, mode="w:xz", preset=6) as archive:
                _add_tar_file(archive, "MANIFEST.json", _json_bytes(manifest))
                for archive_path, data in tar_files:
                    _add_tar_file(archive, archive_path, data)
            stream.flush()
            os.fsync(stream.fileno())
        # link() is an atomic no-clobber promotion on the same filesystem.
        os.link(temporary_name, output)
    except FileExistsError as error:
        raise FileExistsError("output already exists: " + str(output)) from error
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor", required=True, help="validated cargo vendor directory")
    parser.add_argument("--cargo-lock", required=True, help="Cargo.lock matching the vendor tree")
    parser.add_argument("--output", required=True, help="new .tar.xz output path")
    args = parser.parse_args(argv)
    try:
        build_rust_notices(args.vendor, args.cargo_lock, args.output)
    except (OSError, ValueError, tarfile.TarError) as error:
        print("rust_notices: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
