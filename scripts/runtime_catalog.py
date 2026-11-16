#!/usr/bin/env python3
"""Build and validate deterministic metadata for staged Ashnazg runtime assets."""

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path


SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
VERSION_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
MAX_GUEST_SIZE = 1024**3
MAX_SMALL_ARTIFACT_SIZE = 64 * 1024**2
MAX_SOURCE_ARCHIVES = 64
MAX_SOURCE_ARCHIVE_SIZE = 1024**3
CHUNK_SIZE = 1024 * 1024

ROLE_SPECS = {
    "guest": ("linux-aarch64", "ashnazg-microvm-v{version}-linux-aarch64.tar.xz", MAX_GUEST_SIZE),
    "helper": ("macos-aarch64", "ashnazg-vm-v{version}-macos-aarch64", MAX_SMALL_ARTIFACT_SIZE),
    "agent": ("android", "ashnazg-agent-v{version}.apk", MAX_SMALL_ARTIFACT_SIZE),
    "fixture": ("android", "ashnazg-fixture-v{version}.apk", MAX_SMALL_ARTIFACT_SIZE),
}
ENTRY_FIELDS = {"name", "platform", "source_sha", "size", "sha256"}
ROOT_FIELDS = {"schema_version", "version", "release_tag", "source_sha", "artifacts", "source_archives"}


def _validate_version(version):
    if not isinstance(version, str) or VERSION_RE.fullmatch(version) is None:
        raise ValueError("version must be stable semantic version X.Y.Z without a v prefix")


def _validate_source_sha(source_sha):
    if not isinstance(source_sha, str) or SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise ValueError("source_sha must be 40 lowercase hexadecimal characters")


def _collect_source_archives(source_archives):
    """Materialize at most the permitted number of parts plus one overflow item."""
    collected = []
    for item in source_archives:
        collected.append(item)
        if len(collected) > MAX_SOURCE_ARCHIVES:
            raise ValueError(f"source_archives may contain at most {MAX_SOURCE_ARCHIVES} parts")
    return collected


def _identity(info):
    """Fields that identify a regular file and detect changes during streaming."""
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_size,
        getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
        getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000)),
    )


def _hash_file(path, *, max_size, description):
    path = Path(path)
    try:
        before_path = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ValueError(f"{description} file does not exist: {path}") from exc
    if stat.S_ISLNK(before_path.st_mode):
        raise ValueError(f"{description} must not be a symlink: {path}")
    if not stat.S_ISREG(before_path.st_mode):
        raise ValueError(f"{description} must be a regular file: {path}")
    if before_path.st_size <= 0:
        raise ValueError(f"{description} must not be empty: {path}")
    if before_path.st_size > max_size:
        raise ValueError(f"{description} exceeds size limit of {max_size} bytes: {path}")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"could not safely open {description}: {path}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before_path):
            raise ValueError(f"{description} changed or was replaced before hashing: {path}")
        if opened.st_size <= 0 or opened.st_size > max_size:
            raise ValueError(f"{description} has invalid size: {path}")

        digest = hashlib.sha256()
        byte_count = 0
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while True:
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > max_size:
                    raise ValueError(f"{description} exceeds size limit of {max_size} bytes: {path}")
                digest.update(chunk)

        after_fd = os.fstat(fd)
        try:
            after_path = os.stat(path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ValueError(f"{description} was replaced or removed while hashing: {path}") from exc
        if (
            _identity(after_fd) != _identity(opened)
            or _identity(after_path) != _identity(opened)
            or byte_count != opened.st_size
        ):
            raise ValueError(f"{description} changed or was replaced while hashing: {path}")
        return byte_count, digest.hexdigest()
    finally:
        os.close(fd)


def _validate_entry(entry, *, version, source_sha, expected_name, expected_platform, max_size, description):
    if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
        raise ValueError(f"{description} must contain exactly {sorted(ENTRY_FIELDS)}")
    if not isinstance(entry["name"], str) or entry["name"] != expected_name:
        raise ValueError(f"invalid {description} name")
    if "/" in entry["name"] or "\\" in entry["name"] or entry["name"] in (".", ".."):
        raise ValueError(f"unsafe {description} name")
    if entry["name"].endswith(".json"):
        raise ValueError(f"{description} cannot reference a catalog file")
    if entry["platform"] != expected_platform:
        raise ValueError(f"invalid {description} platform")
    if entry["source_sha"] != source_sha:
        raise ValueError(f"{description} source_sha does not match catalog")
    size = entry["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > max_size:
        raise ValueError(f"{description} size must be a positive integer no greater than {max_size}")
    if not isinstance(entry["sha256"], str) or SHA256_RE.fullmatch(entry["sha256"]) is None:
        raise ValueError(f"invalid {description} sha256")


def validate_catalog(catalog):
    """Strictly validate a catalog and return the same object."""
    if not isinstance(catalog, dict) or set(catalog) != ROOT_FIELDS:
        raise ValueError(f"catalog must contain exactly {sorted(ROOT_FIELDS)}")
    if type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1:
        raise ValueError("schema_version must be integer 1")
    version = catalog["version"]
    _validate_version(version)
    if catalog["release_tag"] != "v" + version:
        raise ValueError("release_tag must equal v plus version")
    source_sha = catalog["source_sha"]
    _validate_source_sha(source_sha)

    artifacts = catalog["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != set(ROLE_SPECS):
        raise ValueError("artifacts must contain exactly the four required roles")
    names = set()
    for role, (platform, name_template, max_size) in ROLE_SPECS.items():
        expected_name = name_template.format(version=version)
        entry = artifacts[role]
        _validate_entry(
            entry, version=version, source_sha=source_sha, expected_name=expected_name,
            expected_platform=platform, max_size=max_size, description=f"artifact {role}",
        )
        if entry["name"] in names:
            raise ValueError("artifact and source archive names must be unique")
        names.add(entry["name"])

    source_archives = catalog["source_archives"]
    if not isinstance(source_archives, list):
        raise ValueError("source_archives must be an array")
    if len(source_archives) > MAX_SOURCE_ARCHIVES:
        raise ValueError(f"source_archives may contain at most {MAX_SOURCE_ARCHIVES} parts")
    source_indices = set()
    source_pattern = re.compile(rf"ashnazg-runtime-sources-v{re.escape(version)}-([0-9]{{3}})\.tar\.xz\Z")
    for index, entry in enumerate(source_archives):
        if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
            raise ValueError(f"source archive {index} must contain exactly {sorted(ENTRY_FIELDS)}")
        name = entry["name"]
        match = source_pattern.fullmatch(name) if isinstance(name, str) else None
        if match is None or int(match.group(1)) == 0:
            raise ValueError("invalid source archive name or index")
        part_index = int(match.group(1))
        if part_index in source_indices:
            raise ValueError("source archive indices must be unique")
        source_indices.add(part_index)
        if name in names:
            raise ValueError("artifact and source archive names must be unique")
        names.add(name)
        _validate_entry(
            entry, version=version, source_sha=source_sha, expected_name=name,
            expected_platform="source", max_size=MAX_SOURCE_ARCHIVE_SIZE,
            description=f"source archive {index}",
        )
    return catalog


def build_catalog(version, source_sha, artifacts, source_archives=()):
    """Hash staged artifact files and return a validated deterministic catalog value."""
    _validate_version(version)
    _validate_source_sha(source_sha)
    if not isinstance(artifacts, dict) or set(artifacts) != set(ROLE_SPECS):
        raise ValueError("artifacts must map exactly the four required roles")

    entries = {}
    seen_paths = set()
    seen_names = set()
    for role, (platform, name_template, max_size) in ROLE_SPECS.items():
        path = Path(artifacts[role])
        expected_name = name_template.format(version=version)
        if path.name != expected_name:
            raise ValueError(f"artifact {role} must use fixed basename {expected_name}")
        identity = os.path.abspath(os.fspath(path))
        if identity in seen_paths or expected_name in seen_names:
            raise ValueError("artifact paths and names must be unique")
        seen_paths.add(identity)
        seen_names.add(expected_name)
        size, digest = _hash_file(path, max_size=max_size, description=f"artifact {role}")
        entries[role] = {
            "name": expected_name,
            "platform": platform,
            "source_sha": source_sha,
            "size": size,
            "sha256": digest,
        }

    source_archives = _collect_source_archives(source_archives)
    source_entries = []
    source_indices = set()
    for path_value in source_archives:
        path = Path(path_value)
        match = re.fullmatch(
            rf"ashnazg-runtime-sources-v{re.escape(version)}-([0-9]{{3}})\.tar\.xz", path.name
        )
        if match is None or int(match.group(1)) == 0:
            raise ValueError("source archive name must be ashnazg-runtime-sources-v<version>-NNN.tar.xz")
        part_index = int(match.group(1))
        if part_index in source_indices:
            raise ValueError("source archive indices must be unique")
        source_indices.add(part_index)
        identity = os.path.abspath(os.fspath(path))
        if identity in seen_paths or path.name in seen_names:
            raise ValueError("artifact and source archive paths and names must be unique")
        seen_paths.add(identity)
        seen_names.add(path.name)
        size, digest = _hash_file(path, max_size=MAX_SOURCE_ARCHIVE_SIZE, description="source archive")
        source_entries.append({
            "name": path.name,
            "platform": "source",
            "source_sha": source_sha,
            "size": size,
            "sha256": digest,
        })
    source_entries.sort(key=lambda entry: entry["name"])

    catalog = {
        "schema_version": 1,
        "version": version,
        "release_tag": "v" + version,
        "source_sha": source_sha,
        "artifacts": entries,
        "source_archives": source_entries,
    }
    return validate_catalog(catalog)


def _prepare_output(output, input_paths):
    output = Path(output)
    parent = output.parent
    try:
        directory_info = os.stat(parent, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ValueError(f"output parent directory does not exist: {parent}") from exc
    if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
        raise ValueError("output parent must be an existing, non-symlink directory")
    try:
        os.stat(output, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(f"refusing to replace existing output: {output}")

    output_absolute = os.path.abspath(os.fspath(output))
    for path in input_paths:
        if output_absolute == os.path.abspath(os.fspath(path)):
            raise ValueError(f"output cannot alias input file: {path}")
        try:
            if output.resolve(strict=False) == Path(path).resolve(strict=True):
                raise ValueError(f"output cannot alias input file: {path}")
        except FileNotFoundError:
            continue
    return output


def write_catalog(*, version, source_sha, artifacts, source_archives=(), output):
    """Write a validated catalog atomically without replacing any existing path."""
    source_archives = _collect_source_archives(source_archives)
    input_paths = list(artifacts.values()) if isinstance(artifacts, dict) else []
    input_paths.extend(source_archives)
    output = _prepare_output(output, input_paths)
    catalog = build_catalog(version, source_sha, artifacts, source_archives)
    payload = (json.dumps(catalog, sort_keys=True, indent=2) + "\n").encode("utf-8")

    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link creation is atomic and fails if a destination appeared meanwhile.
        os.link(temporary_path, output)
        temporary_path.unlink()
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--guest", required=True)
    parser.add_argument("--helper", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--source-archive", action="append", default=[])
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    artifacts = {role: getattr(args, role) for role in ROLE_SPECS}
    try:
        write_catalog(
            version=args.version,
            source_sha=args.source_sha,
            artifacts=artifacts,
            source_archives=args.source_archive,
            output=args.output,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
