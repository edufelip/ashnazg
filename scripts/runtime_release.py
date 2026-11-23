#!/usr/bin/env python3
"""Prepare and verify a complete, source-pinned Ashnazg runtime release."""

import argparse
import hashlib
import json
import os
import re
import stat
import tarfile
import zipfile
from pathlib import Path

if __package__:
    from scripts import package_release, runtime_catalog
else:  # support direct `python scripts/runtime_release.py ...` execution
    import package_release
    import runtime_catalog


DEFAULT_VERSION = "0.3.0"
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
CATALOG_NAME = "ashnazg-runtime-v{version}.json"
MAX_CATALOG_SIZE = 64 * 1024
MAX_CANDIDATE_SIZE = 2 * 1024 * 1024
MAX_SUMS_SIZE = 1024 * 1024
MAX_HOST_ZIP_SIZE = 64 * 1024 * 1024
MAX_GUEST_SIZE = 1024**3
MAX_TAR_KERNEL_SIZE = 128 * 1024**2
MAX_TAR_ROOTFS_SIZE = 4 * 1024**3
CHUNK_SIZE = 1024 * 1024

GUEST_MEMBERS = {
    "NOTICES.md", "kernel", "kernel.config", "kernel.generated.config",
    "linux-6.12.117.tar.xz", "manifest.json", "packages.lock.json",
    "runtime-components.json", "runtime-installed.db", "rootfs.ext4",
    "provenance.json",
}
ZIP_BASE_MEMBERS = {
    "LICENSE", "README-SETUP.md", "THIRD-PARTY-NOTICES.tar.xz", "android/agent-instrumentation.apk",
    "android/fixture-instrumentation.apk", "bin/ashnazg",
}
IOS_FIXTURE_MEMBERS = {
    f"platform/ios/{rel}" for rel in package_release.IOS_FIXTURE_FILES
}


def _version(value):
    try:
        runtime_catalog._validate_version(value)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _source_sha(value):
    if not isinstance(value, str) or SOURCE_SHA_RE.fullmatch(value) is None:
        raise ValueError("source_sha must be 40 lowercase hexadecimal characters")


def _sha_stream(stream, *, limit, description):
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise ValueError(f"{description} exceeds size limit of {limit} bytes")
        digest.update(chunk)
    if size <= 0:
        raise ValueError(f"{description} must not be empty")
    return size, digest.hexdigest()


def _safe_regular(path, *, limit, description):
    path = Path(path)
    try:
        before = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"missing {description}: {path}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{description} must be a regular non-symlink file: {path}")
    if before.st_size <= 0:
        raise ValueError(f"{description} must not be empty: {path}")
    if before.st_size > limit:
        raise ValueError(f"{description} exceeds size limit of {limit} bytes: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"could not safely open {description}: {path}") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"{description} changed or was replaced: {path}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _hash_path(path, *, limit, description):
    fd = _safe_regular(path, limit=limit, description=description)
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            size, digest = _sha_stream(stream, limit=limit, description=description)
        after = os.fstat(fd)
        if after.st_size != size:
            raise ValueError(f"{description} changed while hashing")
        return {"name": Path(path).name, "size": size, "sha256": digest}
    finally:
        os.close(fd)


def _read_bounded(path, *, limit, description):
    _hash_path(path, limit=limit, description=description)
    with open(path, "rb") as stream:
        return stream.read(limit + 1)


def _strict_json(raw, description):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{description} contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=unique_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{description} is not strict UTF-8 JSON: {exc}") from exc


def _canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _catalog_artifact_paths(directory, version):
    return {
        "guest": directory / f"ashnazg-microvm-v{version}-linux-aarch64.tar.xz",
        "helper": directory / f"ashnazg-vm-v{version}-macos-aarch64",
        "agent": directory / f"ashnazg-agent-v{version}.apk",
        "fixture": directory / f"ashnazg-fixture-v{version}.apk",
    }


def _expected_names(version):
    return {
        f"ashnazg-v{version}-linux-x86_64.zip",
        f"ashnazg-v{version}-macos-aarch64.zip",
        f"ashnazg-microvm-v{version}-linux-aarch64.tar.xz",
        f"ashnazg-vm-v{version}-macos-aarch64",
        f"ashnazg-agent-v{version}.apk",
        f"ashnazg-fixture-v{version}.apk",
        f"ashnazg-runtime-sources-v{version}-001.tar.xz",
    }


def _directory_names(directory):
    try:
        metadata = os.stat(directory, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"release directory is unavailable: {directory}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("release directory must be a real directory, not a symlink")
    return {entry.name for entry in Path(directory).iterdir()}


def _file_limits(version):
    names = _expected_names(version)
    limits = {name: MAX_HOST_ZIP_SIZE for name in names if name.endswith(".zip")}
    limits[f"ashnazg-microvm-v{version}-linux-aarch64.tar.xz"] = MAX_GUEST_SIZE
    for name in names - set(limits):
        if name.endswith(".tar.xz"):
            limits[name] = runtime_catalog.MAX_SOURCE_ARCHIVE_SIZE
        elif name.endswith(".apk"):
            limits[name] = runtime_catalog.MAX_SMALL_ARTIFACT_SIZE
        else:
            limits[name] = runtime_catalog.MAX_SMALL_ARTIFACT_SIZE
    return limits


def _validate_guest(path, *, source_sha, version):
    try:
        with tarfile.open(path, mode="r|xz") as archive:
            seen = set()
            manifest = None
            hashes = {"kernel": hashlib.sha256(), "rootfs.ext4": hashlib.sha256()}
            sizes = {"kernel": 0, "rootfs.ext4": 0}
            for member in archive:
                name = member.name
                if name in seen or name not in GUEST_MEMBERS:
                    raise ValueError(f"guest archive has duplicate or unexpected member: {name}")
                if not member.isfile() or member.issparse():
                    raise ValueError(f"guest archive member must be a regular file: {name}")
                seen.add(name)
                maximum = {"kernel": MAX_TAR_KERNEL_SIZE, "rootfs.ext4": MAX_TAR_ROOTFS_SIZE,
                           "manifest.json": MAX_CATALOG_SIZE, "kernel.config": 64 * 1024,
                           "NOTICES.md": 1024 * 1024, "linux-6.12.117.tar.xz": 256 * 1024**2}.get(name, 2 * 1024**2)
                if member.size <= 0 or member.size > maximum:
                    raise ValueError(f"guest archive member has invalid size: {name}")
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"could not read guest archive member: {name}")
                if name == "manifest.json":
                    data = source.read(MAX_CATALOG_SIZE + 1)
                    if len(data) > MAX_CATALOG_SIZE or len(data) != member.size:
                        raise ValueError("guest manifest exceeds its size limit or was truncated")
                    manifest = data
                elif name in hashes:
                    while True:
                        chunk = source.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        sizes[name] += len(chunk)
                        if sizes[name] > maximum:
                            raise ValueError(f"guest {name} exceeds its size limit")
                        hashes[name].update(chunk)
                    if sizes[name] != member.size:
                        raise ValueError(f"guest {name} size mismatch")
            if seen != GUEST_MEMBERS:
                raise ValueError(f"guest archive members differ from exact contract; missing {sorted(GUEST_MEMBERS - seen)}")
    except (OSError, tarfile.TarError, EOFError, lzma_error()) as exc:
        raise ValueError(f"invalid guest tar.xz archive: {exc}") from exc
    guest = _strict_json(manifest, "guest manifest")
    if not isinstance(guest, dict) or set(guest) != {"schema_version", "version", "source_sha", "architecture", "files", "guest"}:
        raise ValueError("guest manifest has invalid fields")
    if manifest != _canonical(guest):
        raise ValueError("guest manifest bytes are not canonical")
    if type(guest["schema_version"]) is not int or guest["schema_version"] != 1:
        raise ValueError("guest manifest schema_version must be integer 1")
    if guest["version"] != version or guest["source_sha"] != source_sha or guest["architecture"] != "aarch64":
        raise ValueError("guest manifest version or source does not match the release")
    expected_guest = {
        "kernel_version": "6.12.117", "base": "alpine-3.22.8-aarch64",
        "redroid_index": "sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee",
    }
    if guest["guest"] != expected_guest or not isinstance(guest["files"], dict) or set(guest["files"]) != set(hashes):
        raise ValueError("guest manifest files or pinned guest contract is invalid")
    for name in hashes:
        item = guest["files"].get(name) if isinstance(guest["files"], dict) else None
        if not isinstance(item, dict) or set(item) != {"size", "sha256"}:
            raise ValueError(f"guest manifest is missing valid metadata for {name}")
        if type(item["size"]) is not int or item["size"] != sizes[name] or item["sha256"] != hashes[name].hexdigest():
            raise ValueError(f"guest manifest {name} size or SHA-256 does not match contents")


def lzma_error():
    import lzma
    return lzma.LZMAError


def _validate_zip(path, *, source_sha, version, platform, raw_helper, raw_agent, raw_fixture):
    try:
        with zipfile.ZipFile(path) as archive:
            payload_names = (
                ZIP_BASE_MEMBERS
                | ({"bin/ashnazg-vm"} if platform == "macos-aarch64" else set())
                | (IOS_FIXTURE_MEMBERS if platform.startswith("macos-") else set())
            )
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or set(names) != payload_names | {"manifest.json", "SHA256SUMS"}:
                raise ValueError("host ZIP members differ from exact package contract")
            for info in infos:
                mode = info.external_attr >> 16
                if info.is_dir() or (mode and not stat.S_ISREG(mode)):
                    raise ValueError(f"host ZIP member is not a regular file: {info.filename}")
                if info.file_size > MAX_SMALL_MEMBER_SIZE:
                    raise ValueError(f"host ZIP member exceeds size limit: {info.filename}")
            manifest_raw = archive.read("manifest.json")
            manifest = _strict_json(manifest_raw, "host ZIP manifest")
            if manifest_raw != _canonical(manifest):
                raise ValueError("host ZIP manifest bytes are not canonical")
            if not isinstance(manifest, dict) or set(manifest) != {"name", "version", "source_sha", "platform", "files"}:
                raise ValueError("host ZIP manifest has invalid fields")
            if manifest["name"] != "ashnazg" or manifest["version"] != version or manifest["source_sha"] != source_sha or manifest["platform"] != platform:
                raise ValueError("host ZIP manifest source, version, or platform mismatch")
            if not isinstance(manifest["files"], dict) or set(manifest["files"]) != payload_names:
                raise ValueError("host ZIP manifest files differ from exact package contract")
            payloads = {}
            for name in sorted(payload_names):
                data = archive.read(name)
                if not data:
                    raise ValueError(f"host ZIP payload is empty: {name}")
                metadata = manifest["files"][name]
                if not isinstance(metadata, dict) or set(metadata) != {"sha256", "size"}:
                    raise ValueError(f"host ZIP manifest metadata is invalid for {name}")
                if (type(metadata["size"]) is not int or metadata["size"] != len(data) or
                        not isinstance(metadata["sha256"], str) or
                        SHA256_RE.fullmatch(metadata["sha256"]) is None or
                        metadata["sha256"] != hashlib.sha256(data).hexdigest()):
                    raise ValueError(f"host ZIP payload digest or size mismatch: {name}")
                payloads[name] = data
            if payloads["android/agent-instrumentation.apk"] != raw_agent:
                raise ValueError("packaged agent APK does not match the cataloged agent artifact")
            if payloads["android/fixture-instrumentation.apk"] != raw_fixture:
                raise ValueError("packaged fixture APK does not match the cataloged fixture artifact")
            if platform == "macos-aarch64" and payloads["bin/ashnazg-vm"] != raw_helper:
                raise ValueError("macOS ZIP helper does not match the raw signed helper")
            expected_sums = "".join(
                f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                for name, data in sorted({**payloads, "manifest.json": manifest_raw}.items())
            ).encode("ascii")
            if archive.read("SHA256SUMS") != expected_sums:
                raise ValueError("host ZIP SHA256SUMS bytes are invalid")
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError(f"invalid host ZIP archive: {exc}") from exc


MAX_SMALL_MEMBER_SIZE = 64 * 1024 * 1024


def _inspect_payloads(directory, *, source_sha, version):
    names = _directory_names(directory)
    expected = _expected_names(version)
    if names != expected:
        raise ValueError(f"release input directory must contain exactly payload files; missing {sorted(expected - names)}, unexpected {sorted(names - expected)}")
    metadata = {}
    for name, limit in _file_limits(version).items():
        metadata[name] = _hash_path(directory / name, limit=limit, description="payload")
    guest_path = directory / f"ashnazg-microvm-v{version}-linux-aarch64.tar.xz"
    helper_path = directory / f"ashnazg-vm-v{version}-macos-aarch64"
    helper = _read_bounded(helper_path, limit=runtime_catalog.MAX_SMALL_ARTIFACT_SIZE, description="signed helper")
    agent = _read_bounded(directory / f"ashnazg-agent-v{version}.apk",
                          limit=runtime_catalog.MAX_SMALL_ARTIFACT_SIZE, description="agent APK")
    fixture = _read_bounded(directory / f"ashnazg-fixture-v{version}.apk",
                            limit=runtime_catalog.MAX_SMALL_ARTIFACT_SIZE, description="fixture APK")
    _validate_guest(guest_path, source_sha=source_sha, version=version)
    _validate_zip(directory / f"ashnazg-v{version}-linux-x86_64.zip", source_sha=source_sha,
                  version=version, platform="linux-x86_64", raw_helper=helper,
                  raw_agent=agent, raw_fixture=fixture)
    _validate_zip(directory / f"ashnazg-v{version}-macos-aarch64.zip", source_sha=source_sha,
                  version=version, platform="macos-aarch64", raw_helper=helper,
                  raw_agent=agent, raw_fixture=fixture)
    return metadata


def prepare_release(directory, *, source_sha, version=DEFAULT_VERSION):
    """Create catalog, candidate inventory and release checksums without replacement."""
    _version(version)
    _source_sha(source_sha)
    root = Path(directory)
    _inspect_payloads(root, source_sha=source_sha, version=version)
    # runtime_catalog deliberately refuses symlinked parent components (notably
    # macOS /var -> /private/var), so hand it the resolved real directory path.
    root = root.resolve(strict=True)
    catalog_path = root / CATALOG_NAME.format(version=version)
    runtime_catalog.write_catalog(version=version, source_sha=source_sha,
                                  artifacts=_catalog_artifact_paths(root, version),
                                  source_archives=[root / f"ashnazg-runtime-sources-v{version}-001.tar.xz"],
                                  output=catalog_path)
    catalog_raw = _read_bounded(catalog_path, limit=MAX_CATALOG_SIZE, description="runtime catalog")
    catalog = _strict_json(catalog_raw, "runtime catalog")
    runtime_catalog.validate_catalog(catalog)
    if catalog_raw != _canonical(catalog) or catalog["source_sha"] != source_sha:
        raise ValueError("runtime catalog bytes or source do not match the release")
    artifacts = []
    for name in sorted(_expected_names(version) | {catalog_path.name}):
        artifacts.append(_hash_path(root / name, limit=MAX_GUEST_SIZE, description="candidate artifact"))
    candidate = {"schema_version": 1, "version": version, "source_sha": source_sha, "artifacts": artifacts}
    candidate_path = root / "candidate.json"
    if candidate_path.exists() or candidate_path.is_symlink():
        raise ValueError("refusing to replace existing candidate.json")
    candidate_path.write_bytes(_canonical(candidate))
    sums_path = root / "SHA256SUMS"
    if sums_path.exists() or sums_path.is_symlink():
        raise ValueError("refusing to replace existing SHA256SUMS")
    checksum_items = artifacts + [{"name": "candidate.json", "sha256": hashlib.sha256(_canonical(candidate)).hexdigest()}]
    sums = "".join(f"{item['sha256']}  {item['name']}\n" for item in sorted(checksum_items, key=lambda item: item["name"])).encode("ascii")
    sums_path.write_bytes(sums)
    return True


def verify_release(directory, *, source_sha, version=DEFAULT_VERSION):
    """Strictly verify a prepared release. Verification never repairs files."""
    _version(version)
    _source_sha(source_sha)
    root = Path(directory)
    expected_payloads = _expected_names(version)
    expected_all = expected_payloads | {CATALOG_NAME.format(version=version), "candidate.json", "SHA256SUMS"}
    if _directory_names(root) != expected_all:
        actual = _directory_names(root)
        raise ValueError(f"release directory files differ from exact allowlist; missing {sorted(expected_all - actual)}, unexpected {sorted(actual - expected_all)}")
    metadata = {}
    for name, limit in _file_limits(version).items():
        metadata[name] = _hash_path(root / name, limit=limit, description="payload")
    _validate_guest(root / f"ashnazg-microvm-v{version}-linux-aarch64.tar.xz", source_sha=source_sha, version=version)
    helper = _read_bounded(root / f"ashnazg-vm-v{version}-macos-aarch64",
                           limit=runtime_catalog.MAX_SMALL_ARTIFACT_SIZE, description="signed helper")
    agent = _read_bounded(root / f"ashnazg-agent-v{version}.apk",
                          limit=runtime_catalog.MAX_SMALL_ARTIFACT_SIZE, description="agent APK")
    fixture = _read_bounded(root / f"ashnazg-fixture-v{version}.apk",
                            limit=runtime_catalog.MAX_SMALL_ARTIFACT_SIZE, description="fixture APK")
    _validate_zip(root / f"ashnazg-v{version}-linux-x86_64.zip", source_sha=source_sha,
                  version=version, platform="linux-x86_64", raw_helper=helper,
                  raw_agent=agent, raw_fixture=fixture)
    _validate_zip(root / f"ashnazg-v{version}-macos-aarch64.zip", source_sha=source_sha,
                  version=version, platform="macos-aarch64", raw_helper=helper,
                  raw_agent=agent, raw_fixture=fixture)
    catalog_name = CATALOG_NAME.format(version=version)
    catalog_raw = _read_bounded(root / catalog_name, limit=MAX_CATALOG_SIZE, description="runtime catalog")
    metadata[catalog_name] = _hash_path(root / catalog_name, limit=MAX_CATALOG_SIZE,
                                        description="runtime catalog")
    catalog = _strict_json(catalog_raw, "runtime catalog")
    runtime_catalog.validate_catalog(catalog)
    if catalog_raw != _canonical(catalog) or catalog["source_sha"] != source_sha or catalog["version"] != version:
        raise ValueError("runtime catalog bytes, version, or source mismatch")
    for role, path in _catalog_artifact_paths(root, version).items():
        entry = catalog["artifacts"][role]
        actual = metadata[path.name]
        if entry["name"] != actual["name"] or entry["size"] != actual["size"] or entry["sha256"] != actual["sha256"]:
            raise ValueError(f"runtime catalog does not match payload {path.name}")
    source_name = f"ashnazg-runtime-sources-v{version}-001.tar.xz"
    source_entry = next((item for item in catalog["source_archives"] if item["name"] == source_name), None)
    if (len(catalog["source_archives"]) != 1 or source_entry is None or
            source_entry["size"] != metadata[source_name]["size"] or
            source_entry["sha256"] != metadata[source_name]["sha256"]):
        raise ValueError("runtime catalog does not match source archive")
    candidate_path = root / "candidate.json"
    candidate_raw = _read_bounded(candidate_path, limit=MAX_CANDIDATE_SIZE, description="candidate")
    candidate = _strict_json(candidate_raw, "candidate")
    expected_artifacts = [metadata[name] for name in sorted(expected_payloads | {catalog_name})]
    expected_candidate = {"schema_version": 1, "version": version, "source_sha": source_sha,
                          "artifacts": expected_artifacts}
    valid_candidate = (
        isinstance(candidate, dict) and set(candidate) == {"schema_version", "version", "source_sha", "artifacts"} and
        type(candidate.get("schema_version")) is int and candidate.get("schema_version") == 1 and
        candidate.get("version") == version and candidate.get("source_sha") == source_sha and
        isinstance(candidate.get("artifacts"), list)
    )
    if valid_candidate:
        for item in candidate["artifacts"]:
            if (not isinstance(item, dict) or set(item) != {"name", "size", "sha256"} or
                    type(item.get("size")) is not int or not isinstance(item.get("name"), str) or
                    not isinstance(item.get("sha256"), str) or SHA256_RE.fullmatch(item["sha256"]) is None):
                valid_candidate = False
                break
    if not valid_candidate or candidate_raw != _canonical(candidate) or candidate != expected_candidate:
        raise ValueError("candidate bytes or artifact inventory do not match verified files")
    sums = _read_bounded(root / "SHA256SUMS", limit=MAX_SUMS_SIZE, description="SHA256SUMS")
    checksum_items = expected_artifacts + [{"name": "candidate.json", "sha256": hashlib.sha256(candidate_raw).hexdigest()}]
    expected_sums = "".join(f"{item['sha256']}  {item['name']}\n" for item in sorted(checksum_items, key=lambda item: item["name"])).encode("ascii")
    if sums != expected_sums:
        raise ValueError("SHA256SUMS bytes do not match candidate artifacts")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "verify"):
        child = subparsers.add_parser(command)
        child.add_argument("--directory", required=True)
        child.add_argument("--source-sha", required=True)
        child.add_argument("--version", default=DEFAULT_VERSION)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare_release(args.directory, source_sha=args.source_sha, version=args.version)
        else:
            verify_release(args.directory, source_sha=args.source_sha, version=args.version)
    except (OSError, ValueError, tarfile.TarError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
