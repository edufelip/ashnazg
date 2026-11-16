#!/usr/bin/env python3
"""Create a deterministic source and notice sidecar for an exact microVM guest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
SHA512_RE = re.compile(r"[0-9a-f]{128}\Z", re.ASCII)
ORIGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,127}\Z", re.ASCII)
MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_DECOMPRESSED_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 500_000
MAX_LOCK_BYTES = 8 * 1024 * 1024
MAX_FILES = 20_000
MAX_DOWNLOAD_SECONDS = 900
MAX_FILE_DOWNLOAD_SECONDS = 180
ASSET_NAMES = (
    "manifest.json", "provenance.json", "kernel", "rootfs.ext4", "packages.lock.json",
    "NOTICES.md", "kernel.config", "runtime-installed.db", "kernel.generated.config",
    "runtime-components.json", "linux-6.12.117.tar.xz",
)
SIDECAR_ASSET_NAMES = (
    "manifest.json", "provenance.json", "packages.lock.json", "NOTICES.md", "kernel.config",
    "kernel.generated.config", "runtime-installed.db", "runtime-components.json",
    "linux-6.12.117.tar.xz",
)


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _read_json(path):
    try:
        raw = _regular_bytes(path, maximum=MAX_LOCK_BYTES)
        return json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique_json_object,
                          parse_constant=_reject_json_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON input: " + str(path)) from error
    except ValueError as error:
        if str(error) in {"duplicate JSON key", "non-finite JSON number"}:
            raise ValueError("invalid JSON input: " + str(path)) from error
        raise


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError("non-finite JSON number")


def _reject_local_paths(value):
    if isinstance(value, dict):
        for item in value.values():
            _reject_local_paths(item)
    elif isinstance(value, list):
        for item in value:
            _reject_local_paths(item)
    elif isinstance(value, str):
        if value.startswith("/") or "/private/tmp/" in value or "/var/folders/" in value or "\\" in value:
            raise ValueError("checked source lock must not contain local filesystem paths")


def _hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _safe_relpath(value, *, max_parts=32):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("unsafe archive or cache path")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) > max_parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("unsafe archive or cache path")
    if any(not part or part in {".", ".."} or any(ord(char) < 32 or ord(char) == 127 for char in part)
           for part in path.parts):
        raise ValueError("unsafe archive or cache path")
    return path.as_posix()


def _regular_bytes(path, *, maximum=MAX_INPUT_BYTES):
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise FileNotFoundError("required sidecar input is missing: " + str(path)) from error
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise ValueError("input must be a bounded regular file: " + str(path))
    data = path.read_bytes()
    if len(data) != info.st_size:
        raise ValueError("input changed while being read: " + str(path))
    return data


def _file_digest(path, *, maximum=8 * 1024 * 1024 * 1024):
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise FileNotFoundError("required sidecar input is missing: " + str(path)) from error
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise ValueError("input must be a bounded regular file: " + str(path))
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            count += len(chunk)
            digest.update(chunk)
    if count != info.st_size:
        raise ValueError("input changed while being hashed: " + str(path))
    return count, digest.hexdigest()


def _cache_file(root, relative):
    root = Path(root)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("verified cache root must be a real directory")
    current = root
    parts = PurePosixPath(_safe_relpath(relative)).parts
    for part in parts[:-1]:
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("verified cache contains a link or non-directory")
    return current / parts[-1]


def _validate_transfer_record(record, kind):
    if not isinstance(record, dict):
        raise ValueError("invalid " + kind + " record")
    if type(record.get("size")) is not int or not 0 < record["size"] <= MAX_INPUT_BYTES:
        raise ValueError("invalid size in " + kind + " record")
    if not isinstance(record.get("sha256"), str) or not SHA256_RE.fullmatch(record["sha256"]):
        raise ValueError("invalid SHA-256 in " + kind + " record")
    url = record.get("url")
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("invalid URL in " + kind + " record")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source URLs must be pinned HTTPS URLs")
    return record


def _validate_record(record, kind):
    _validate_transfer_record(record, kind)
    origin, commit = record.get("origin"), record.get("commit")
    if not isinstance(origin, str) or not ORIGIN_RE.fullmatch(origin):
        raise ValueError("invalid origin in " + kind + " record")
    if not isinstance(commit, str) or not SOURCE_SHA_RE.fullmatch(commit):
        raise ValueError("invalid commit in " + kind + " record")
    return origin, commit


def _groups_from_inventory(inventory):
    groups = {}
    for package in inventory.get("packages", []):
        origin, commit = package.get("origin"), package.get("aports_commit")
        if not isinstance(origin, str) or not isinstance(commit, str) or not SOURCE_SHA_RE.fullmatch(commit):
            raise ValueError("installed package has missing or invalid origin/commit metadata")
        key = (origin, commit)
        group = groups.setdefault(key, {"origin": origin, "commit": commit, "packages": [], "declared_package_licenses": set()})
        group["packages"].append({k: package[k] for k in ("name", "version", "architecture")})
        license_value = package.get("declared_license")
        if isinstance(license_value, str) and license_value:
            group["declared_package_licenses"].add(license_value)
    for group in groups.values():
        group["packages"].sort(key=lambda item: (item["name"], item["version"], item["architecture"]))
        group["declared_package_licenses"] = sorted(group["declared_package_licenses"])
    declared = inventory.get("source_groups")
    if not isinstance(declared, list):
        raise ValueError("guest inventory source_groups is missing")
    declared_map = {}
    for group in declared:
        key = (group.get("origin"), group.get("aports_commit"))
        if key in declared_map:
            raise ValueError("guest inventory repeats an origin/commit group")
        packages = sorted(group.get("packages", []), key=lambda item: (item.get("name"), item.get("version"), item.get("architecture")))
        declared_map[key] = packages
    if set(declared_map) != set(groups) or any(
            declared_map[key] != groups[key]["packages"] for key in groups):
        raise ValueError("guest inventory source_groups differ from installed package metadata")
    return groups


def validate_inventory_binding(inventory, lock):
    if not isinstance(inventory, dict) or inventory.get("metadata_status") != "complete":
        raise ValueError("guest component inventory is missing or incomplete")
    actual = _groups_from_inventory(inventory)
    expected = {}
    for group in lock.get("groups", []):
        key = (group.get("origin"), group.get("commit"))
        if key in expected:
            raise ValueError("source lock repeats an origin/commit group")
        expected[key] = group
    if set(actual) != set(expected):
        raise ValueError("guest inventory origin/commit groups do not exactly match source lock")
    for key, actual_group in actual.items():
        expected_group = expected[key]
        expected_packages = expected_group.get("packages")
        if not isinstance(expected_packages, list):
            raise ValueError("source lock group is missing package membership")
        normalized = sorted(expected_packages, key=lambda item: (item["name"], item["version"], item["architecture"]))
        if actual_group["packages"] != normalized:
            raise ValueError("guest inventory package membership differs from source lock for " + key[0])
        licenses = sorted(expected_group.get("declared_package_licenses", []))
        if actual_group["declared_package_licenses"] != licenses:
            raise ValueError("guest package license metadata differs from source lock for " + key[0])
    return actual


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, max_redirects=5):
        super().__init__()
        self.max_redirects = max_redirects
        self.count = 0

    def redirect_request(self, request, response, code, message, headers, new_url):
        self.count += 1
        if self.count > self.max_redirects:
            raise urllib.error.HTTPError(new_url, code, "too many redirects", headers, None)
        if urllib.parse.urlsplit(new_url).scheme != "https":
            raise urllib.error.HTTPError(new_url, code, "redirect left HTTPS", headers, None)
        return super().redirect_request(request, response, code, message, headers, new_url)


def _download(record, *, remaining, deadline):
    redirect = _NoRedirect()
    opener = urllib.request.build_opener(redirect)
    request = urllib.request.Request(record["url"], headers={"User-Agent": "ashnazg-source-sidecar/1"})
    started = time.monotonic()
    chunks, size = [], 0
    with opener.open(request, timeout=30) as response:
        if urllib.parse.urlsplit(response.geturl()).scheme != "https":
            raise ValueError("download redirected away from HTTPS")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) != record["size"]:
            name = _safe_relpath(record.get("filename", record.get("path", "input")))
            raise ValueError("download Content-Length differs from lock for " + name
                             + ": expected " + str(record["size"])
                             + " bytes, received " + str(int(content_length)))
        while True:
            now = time.monotonic()
            if now - started > MAX_FILE_DOWNLOAD_SECONDS or now > deadline:
                raise ValueError("source download exceeded file or overall time limit")
            chunk = response.read(min(1024 * 1024, record["size"] + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > record["size"] or size > remaining:
                raise ValueError("source download exceeded locked or aggregate size")
    result = b"".join(chunks)
    _verify_record_bytes(record, result)
    return result


def _collect_notice_data(records, cache_root, source_records, source_bytes, recipe_records, recipe_bytes):
    """Resolve cached evidence or recover requested upstream members in one archive pass."""
    result, archive_requests, direct_sources = {}, {}, {}
    source_by_key = {(item["origin"], item["commit"], item["filename"]): item for item in source_records}
    recipes_by_cache = {item["cache_path"]: item for item in recipe_records}
    for record in records:
        candidate_id = record["candidate_id"]
        cache_path = record.get("cache_path")
        candidate_cache = _cache_file(cache_root, cache_path) if cache_path else None
        if candidate_cache and (candidate_cache.exists() or candidate_cache.is_symlink()):
            data = _regular_bytes(candidate_cache, maximum=record["size"])
            if len(data) != record["size"] or _hash_bytes(data) != record["sha256"]:
                raise ValueError("notice candidate size or SHA-256 mismatch")
            result[candidate_id] = data
            continue
        recipe_cache_path = record.get("recipe_cache_path")
        if recipe_cache_path:
            recipe_record = recipes_by_cache.get(_safe_relpath(recipe_cache_path))
            if recipe_record is None:
                raise ValueError("notice candidate recipe is not in the checked recipe inputs")
            data = recipe_bytes[_recipe_member_name(recipe_record)]
            if len(data) != record["size"] or _hash_bytes(data) != record["sha256"]:
                raise ValueError("notice candidate recipe size or SHA-256 mismatch")
            result[candidate_id] = data
            continue
        filename = record.get("source_filename")
        source_key = (record["origin"], record["commit"], filename)
        source_record = source_by_key.get(source_key)
        if source_record is None:
            raise ValueError("notice candidate source archive is not in the checked source inputs")
        if record.get("member_path") is None:
            direct_sources.setdefault(source_key, []).append(record)
        else:
            member = _safe_relpath(record["member_path"], max_parts=32)
            archive_requests.setdefault(source_key, {})[member] = record
    for source_key, candidates in direct_sources.items():
        source_record = source_by_key[source_key]
        data = source_bytes[_source_member_name(source_record)]
        for record in candidates:
            if len(data) != record["size"] or _hash_bytes(data) != record["sha256"]:
                raise ValueError("notice candidate input size or SHA-256 mismatch")
            result[record["candidate_id"]] = data
    archive_members = 0
    archive_expanded = 0
    for source_key, requested in archive_requests.items():
        source_record = source_by_key[source_key]
        archive_bytes = source_bytes[_source_member_name(source_record)]
        remaining = set(requested)
        stream = __import__("io").BytesIO(archive_bytes)
        if zipfile.is_zipfile(stream):
            try:
                stream.seek(0)
                with zipfile.ZipFile(stream) as upstream:
                    infos = upstream.infolist()
                    expanded = sum(info.file_size for info in infos)
                    archive_members += len(infos)
                    archive_expanded += expanded
                    if (len(infos) > MAX_ARCHIVE_MEMBERS or archive_members > MAX_ARCHIVE_MEMBERS or
                            expanded > MAX_DECOMPRESSED_ARCHIVE_BYTES or archive_expanded > MAX_DECOMPRESSED_ARCHIVE_BYTES):
                        raise ValueError("notice ZIP source exceeds member or decompressed size bound")
                    for info in infos:
                        if info.filename not in remaining:
                            if info.filename in requested:
                                raise ValueError("notice ZIP source repeats a requested member path")
                            continue
                        mode = (info.external_attr >> 16) & 0o170000
                        record = requested[info.filename]
                        if info.is_dir() or mode not in {0, stat.S_IFREG} or info.file_size != record["size"] or info.file_size > MAX_INPUT_BYTES:
                            raise ValueError("notice candidate ZIP member is missing or not a regular file")
                        data = upstream.read(info)
                        if len(data) != record["size"] or _hash_bytes(data) != record["sha256"]:
                            raise ValueError("notice candidate ZIP member size or SHA-256 mismatch")
                        result[record["candidate_id"]] = data
                        remaining.remove(info.filename)
            except zipfile.BadZipFile as error:
                raise ValueError("notice candidate source input is not a readable ZIP archive") from error
        else:
            try:
                stream.seek(0)
                with tarfile.open(fileobj=stream, mode="r|*") as upstream:
                    expanded = 0
                    for count, member in enumerate(upstream, start=1):
                        if count > MAX_ARCHIVE_MEMBERS:
                            raise ValueError("notice source archive exceeds member count bound")
                        expanded += max(0, member.size)
                        if expanded > MAX_DECOMPRESSED_ARCHIVE_BYTES:
                            raise ValueError("notice source archive exceeds decompressed size bound")
                        archive_expanded += max(0, member.size)
                        if archive_expanded > MAX_DECOMPRESSED_ARCHIVE_BYTES:
                            raise ValueError("notice archives exceed aggregate decompressed size bound")
                        archive_members += 1
                        if archive_members > MAX_ARCHIVE_MEMBERS:
                            raise ValueError("notice archives exceed aggregate member count bound")
                        if member.name not in remaining:
                            if member.name in requested:
                                raise ValueError("notice source archive repeats a requested member path")
                            continue
                        record = requested[member.name]
                        if not member.isfile() or member.size != record["size"] or member.size > MAX_INPUT_BYTES:
                            raise ValueError("notice candidate archive member is missing or not a regular file")
                        member_stream = upstream.extractfile(member)
                        data = member_stream.read(MAX_INPUT_BYTES + 1) if member_stream else b""
                        if len(data) != record["size"] or _hash_bytes(data) != record["sha256"]:
                            raise ValueError("notice candidate archive member size or SHA-256 mismatch")
                        result[record["candidate_id"]] = data
                        remaining.remove(member.name)
            except tarfile.TarError as error:
                raise ValueError("notice candidate source input is not a readable archive: " + source_record["filename"]) from error
        if remaining:
            raise ValueError("notice candidate archive member was not found: " + sorted(remaining)[0])
    if len(result) != len(records):
        raise ValueError("notice candidate identities are duplicated")
    return result


def _verify_record_bytes(record, data):
    _validate_transfer_record(record, "input")
    if len(data) != record["size"] or _hash_bytes(data) != record["sha256"]:
        raise ValueError("locked source input size or SHA-256 mismatch: " + record.get("filename", record.get("path", "")))
    declared = record.get("declared_sha512")
    if declared is not None:
        if not isinstance(declared, str) or not SHA512_RE.fullmatch(declared):
            raise ValueError("invalid declared SHA-512 in source lock")
        if hashlib.sha512(data).hexdigest() != declared:
            raise ValueError("declared SHA-512 mismatch for source input")
    observed = record.get("observed_sha512")
    if observed is not None:
        if not isinstance(observed, str) or not SHA512_RE.fullmatch(observed):
            raise ValueError("invalid observed SHA-512 in source lock")
        if hashlib.sha512(data).hexdigest() != observed:
            raise ValueError("observed SHA-512 mismatch for source input")


def _cached_or_download(cache_root, record, *, remaining, deadline):
    if record["size"] > remaining:
        raise ValueError("source input exceeds remaining aggregate budget")
    path = _cache_file(cache_root, record.get("cache_path"))
    try:
        info = path.lstat()
    except FileNotFoundError:
        data = _download(record, remaining=remaining, deadline=deadline)
    else:
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("cached source input is not a regular file")
        data = _regular_bytes(path, maximum=record["size"])
        _verify_record_bytes(record, data)
    return data


def _parse_cargo_lock(raw):
    """Read the registry package subset of Cargo.lock on Python 3.9 and later."""
    text = raw.decode("utf-8", "strict")
    try:
        import tomllib  # Python 3.11+
        lock = tomllib.loads(text)
        packages = lock.get("package", [])
    except ImportError:  # macOS system Python 3.9 remains supported.
        packages = []
        block = {}
        for line in text.splitlines() + ["[[package]]"]:
            if line.strip() == "[[package]]":
                if block:
                    packages.append(block)
                block = {}
            elif "=" in line:
                key, _, value = line.partition("=")
                value = value.strip()
                if value.startswith('"') and value.endswith('"'):
                    block[key.strip()] = value[1:-1]
    result = {}
    for package in packages:
        source, checksum = package.get("source"), package.get("checksum")
        if isinstance(source, str) and source.startswith("registry+"):
            name, version = package.get("name"), package.get("version")
            if not isinstance(name, str) or not isinstance(version, str) or not isinstance(checksum, str) or not SHA256_RE.fullmatch(checksum):
                raise ValueError("Cargo.lock registry package lacks a valid checksum")
            key = (name, version)
            if key in result:
                raise ValueError("Cargo.lock repeats a registry package identity")
            result[key] = checksum
    return result


def validate_rust_vendor(vendor_dir, cargo_lock_path):
    cargo_lock = (cargo_lock_path if isinstance(cargo_lock_path, bytes)
                  else _regular_bytes(cargo_lock_path, maximum=32 * 1024 * 1024))
    lock_packages = _parse_cargo_lock(cargo_lock)
    vendor_dir = Path(vendor_dir)
    if not vendor_dir.is_dir() or vendor_dir.is_symlink():
        raise ValueError("--rust-vendor must be a directory produced by cargo vendor")
    found = {}
    total_bytes = 0
    total_files = 0
    for package_dir in sorted(vendor_dir.iterdir(), key=lambda p: p.name):
        if package_dir.is_symlink() or not package_dir.is_dir():
            raise ValueError("Cargo vendor root may contain only regular package directories")
        checksum_path = package_dir / ".cargo-checksum.json"
        checksum_raw = _regular_bytes(checksum_path, maximum=4 * 1024 * 1024)
        checksum = json.loads(checksum_raw.decode("utf-8"))
        if (not isinstance(checksum, dict) or not {"files", "package"}.issubset(checksum) or
                set(checksum) - {"files", "package", "$comment"} or not isinstance(checksum["files"], dict)):
            raise ValueError("invalid Cargo vendor checksum manifest")
        declared_files = checksum["files"]
        actual_files = set()
        for root, dirs, files in os.walk(package_dir, followlinks=False):
            dirs.sort()
            files.sort()
            for name in list(dirs):
                item = Path(root) / name
                if item.is_symlink() or not item.is_dir():
                    raise ValueError("Cargo vendor contains a link or non-directory")
            for name in files:
                item = Path(root) / name
                if item.is_symlink() or not item.is_file():
                    raise ValueError("Cargo vendor contains a link or non-regular file")
                if item == checksum_path:
                    continue
                total_files += 1
                if total_files > MAX_FILES:
                    raise ValueError("Cargo vendor exceeds file count bound")
                rel = item.relative_to(package_dir).as_posix()
                _safe_relpath(rel)
                actual_files.add(rel)
                expected_hash = declared_files.get(rel)
                if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
                    raise ValueError("Cargo vendor file is absent from .cargo-checksum.json")
                data = _regular_bytes(item, maximum=MAX_INPUT_BYTES)
                total_bytes += len(data)
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError("Cargo vendor exceeds aggregate size bound")
                if _hash_bytes(data) != expected_hash:
                    raise ValueError("Cargo vendor file hash mismatch: " + rel)
        if actual_files != set(declared_files):
            raise ValueError("Cargo vendor file list differs from .cargo-checksum.json")
        package_checksum = checksum["package"]
        if package_checksum is None:
            continue
        if not isinstance(package_checksum, str) or not SHA256_RE.fullmatch(package_checksum):
            raise ValueError("invalid Cargo vendor package checksum")
        # cargo vendor directory names are <name>-<version>; match exact lock identity
        # from Cargo.toml, without importing a TOML dependency on older Python.
        cargo_toml = package_dir / "Cargo.toml"
        manifest_text = _regular_bytes(cargo_toml, maximum=2 * 1024 * 1024).decode("utf-8", "strict")
        name_match = re.search(r"(?m)^name\s*=\s*\"([^\"]+)\"\s*$", manifest_text)
        version_match = re.search(r"(?m)^version\s*=\s*\"([^\"]+)\"\s*$", manifest_text)
        if not name_match or not version_match:
            raise ValueError("Cargo vendor package lacks a top-level name/version")
        key = (name_match.group(1), version_match.group(1))
        if key in found:
            raise ValueError("Cargo vendor repeats a package identity")
        found[key] = package_checksum
    if found != lock_packages:
        raise ValueError("Cargo vendor package/checksum set differs from Cargo.lock registry packages")
    return cargo_lock


def _add_member(archive, name, data, members, *, mode=0o644):
    name = _safe_relpath(name)
    if name in members:
        raise ValueError("duplicate sidecar archive path: " + name)
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.type = tarfile.REGTYPE
    archive.addfile(info, __import__("io").BytesIO(data))
    members[name] = {"size": len(data), "sha256": _hash_bytes(data), "mode": mode}


def _validate_assets(assets_dir, version, source_sha):
    assets_dir = Path(assets_dir)
    if not assets_dir.is_dir() or assets_dir.is_symlink():
        raise ValueError("--assets must be a directory containing the eleven flat guest members")
    names = {p.name for p in assets_dir.iterdir()}
    if names != set(ASSET_NAMES):
        raise ValueError("--assets must contain exactly the eleven documented flat guest members")
    hashes = {name: _file_digest(assets_dir / name) for name in ASSET_NAMES}
    files = {name: _regular_bytes(assets_dir / name,
                                  maximum=256 * 1024 * 1024 if name == "linux-6.12.117.tar.xz" else 64 * 1024 * 1024)
             for name in SIDECAR_ASSET_NAMES}
    for name, data in files.items():
        if hashes[name] != (len(data), _hash_bytes(data)):
            raise ValueError("guest asset changed while it was being verified: " + name)
    manifest = json.loads(files["manifest.json"].decode("utf-8"))
    provenance = json.loads(files["provenance.json"].decode("utf-8"))
    components = json.loads(files["runtime-components.json"].decode("utf-8"))
    if manifest.get("version") != version or manifest.get("source_sha") != source_sha:
        raise ValueError("guest manifest version/source SHA does not match sidecar arguments")
    if provenance.get("source_sha") != source_sha or components.get("source_sha") != source_sha:
        raise ValueError("guest provenance and component inventory must bind the requested source SHA")
    if hashes["rootfs.ext4"][1] != components.get("rootfs_sha256"):
        raise ValueError("component inventory rootfs digest does not match supplied guest rootfs")
    for name in ("kernel", "rootfs.ext4"):
        descriptor = manifest.get("files", {}).get(name)
        if not isinstance(descriptor, dict) or descriptor.get("size") != hashes[name][0] or descriptor.get("sha256") != hashes[name][1]:
            raise ValueError("guest manifest digest does not match " + name)
        if provenance.get("files", {}).get(name) != descriptor:
            raise ValueError("guest provenance digest differs from manifest for " + name)
    component_ref = provenance.get("component_inventory", {})
    if component_ref.get("name") != "runtime-components.json" or component_ref.get("sha256") != _hash_bytes(files["runtime-components.json"]):
        raise ValueError("provenance does not bind runtime-components.json")
    if component_ref.get("installed_db_name") != "runtime-installed.db" or component_ref.get("installed_db_sha256") != _hash_bytes(files["runtime-installed.db"]):
        raise ValueError("provenance does not bind runtime-installed.db")
    if components.get("metadata_status") != "complete" or component_ref.get("metadata_status") != "complete":
        raise ValueError("guest inventory metadata is not complete")
    if provenance.get("package_lock_sha256") != _hash_bytes(files["packages.lock.json"]):
        raise ValueError("provenance package lock digest mismatch")
    if provenance.get("kernel_config_fragment_sha256") != _hash_bytes(files["kernel.config"]):
        raise ValueError("provenance kernel configuration fragment digest mismatch")
    if provenance.get("kernel_config_sha256") != _hash_bytes(files["kernel.generated.config"]):
        raise ValueError("provenance generated kernel configuration digest mismatch")
    kernel_source_hash = provenance.get("upstreams", {}).get("kernel", {}).get("source_bundle_sha256")
    if kernel_source_hash != hashes["linux-6.12.117.tar.xz"][1]:
        raise ValueError("provenance kernel source digest mismatch")
    _validate_installed_db(files["runtime-installed.db"], components)
    return files, hashes, manifest, provenance, components


def _validate_installed_db(raw, inventory):
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise ValueError("installed APK database is not valid UTF-8") from error
    records = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        fields = {}
        for line in block.splitlines():
            if not line or line.startswith(" "):
                continue
            if ":" not in line:
                raise ValueError("installed APK database contains a malformed field")
            key, value = line.split(":", 1)
            fields.setdefault(key, []).append(value)
        required = {"P": "name", "V": "version", "A": "architecture", "o": "origin", "c": "aports_commit", "L": "declared_license"}
        record = {}
        for key, output in required.items():
            values = fields.get(key, [])
            if len(values) != 1:
                raise ValueError("installed APK database has missing or duplicate source metadata")
            record[output] = values[0]
        records.append(record)
    expected = sorted(({
        "name": package["name"], "version": package["version"], "architecture": package["architecture"],
        "origin": package["origin"], "aports_commit": package["aports_commit"],
        "declared_license": package["declared_license"],
    } for package in inventory.get("packages", [])), key=lambda item: (item["name"], item["version"], item["architecture"]))
    records.sort(key=lambda item: (item["name"], item["version"], item["architecture"]))
    if records != expected:
        raise ValueError("installed APK database package/source metadata differs from component inventory")


def _collect_project(repo_root, source_sha, *, remaining=MAX_TOTAL_BYTES):
    repo_root = Path(repo_root)
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise ValueError("--source-sha must be a full lowercase Git commit SHA")
    raw = tempfile.TemporaryFile()
    try:
        subprocess.run(["git", "-C", str(repo_root), "archive", "--format=tar", source_sha],
                       check=True, stdout=raw, stderr=subprocess.DEVNULL, timeout=60)
        raw.seek(0)
    except (OSError, subprocess.SubprocessError) as error:
        raw.close()
        raise ValueError("could not produce project source archive for requested full SHA") from error
    result, modes = {}, {}
    total_bytes = 0
    try:
        with tarfile.open(fileobj=raw, mode="r:") as archive:
            for member in archive:
                path = _safe_relpath(member.name, max_parts=32)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError("project source includes a non-regular member: " + path)
                if member.size > MAX_INPUT_BYTES:
                    raise ValueError("project source file exceeds input size bound")
                if member.size > remaining - total_bytes:
                    raise ValueError("project source exceeds remaining aggregate budget")
                stream = archive.extractfile(member)
                data = stream.read(MAX_INPUT_BYTES + 1) if stream else b""
                if len(data) != member.size:
                    raise ValueError("truncated project source member")
                total_bytes += len(data)
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError("project source exceeds aggregate size bound")
                result[path] = data
                modes[path] = member.mode & 0o777
                if len(result) > MAX_FILES:
                    raise ValueError("project source exceeds file count bound")
    except (tarfile.TarError, OSError) as error:
        raise ValueError("invalid git archive output") from error
    finally:
        raw.close()
    if not result or "LICENSE" not in result:
        raise ValueError("exact Git source archive is empty or has no project LICENSE")
    return result, modes


def _collect_rust_vendor(vendor_dir, cargo_lock_bytes, *, remaining=MAX_TOTAL_BYTES):
    cargo_lock = validate_rust_vendor(vendor_dir, cargo_lock_bytes)
    root = Path(vendor_dir)
    files = {"rust-vendor/Cargo.lock": cargo_lock}
    total = len(cargo_lock)
    if total > remaining:
        raise ValueError("Rust vendor exceeds remaining aggregate budget")
    for package_dir in sorted(root.iterdir(), key=lambda p: p.name):
        checksum_path = package_dir / ".cargo-checksum.json"
        checksum = json.loads(_regular_bytes(checksum_path, maximum=4 * 1024 * 1024).decode("utf-8", "strict"))
        for directory, dirs, names in os.walk(package_dir, followlinks=False):
            dirs.sort()
            names.sort()
            for name in names:
                path = Path(directory) / name
                rel = path.relative_to(root).as_posix()
                _safe_relpath(rel, max_parts=32)
                if path.lstat().st_size > remaining - total:
                    raise ValueError("Rust vendor exceeds remaining aggregate budget")
                data = _regular_bytes(path, maximum=MAX_INPUT_BYTES)
                total += len(data)
                if path != checksum_path and _hash_bytes(data) != checksum.get("files", {}).get(path.relative_to(package_dir).as_posix()):
                    raise ValueError("Cargo vendor file changed after checksum validation: " + rel)
                files["rust-vendor/" + rel] = data
                if len(files) > MAX_FILES:
                    raise ValueError("Rust vendor source exceeds file count bound")
    return files


def _source_member_name(record):
    origin = record["origin"]
    filename = _safe_relpath(record.get("filename"), max_parts=1)
    return "source-inputs/" + origin + "/" + filename


def _recipe_member_name(record):
    path = _safe_relpath(record.get("path"), max_parts=12)
    return "recipes/" + record["origin"] + "/" + record["repository"] + "/" + path


def build_source_sidecar(assets_dir, repo_root, output, version, source_sha,
                         input_dir, rust_vendor_dir, lock_path=None):
    """Verify guest and all locked inputs, then atomically write a deterministic xz tar."""
    repo_root = Path(repo_root)
    lock_path = Path(lock_path) if lock_path else repo_root / "runtime/microvm/sources.lock.json"
    lock = _read_json(lock_path)
    if not isinstance(lock, dict) or lock.get("schema_version") != 1:
        raise ValueError("unsupported or invalid source lock")
    _reject_local_paths(lock)
    files, asset_hashes, manifest, provenance, inventory = _validate_assets(assets_dir, version, source_sha)
    # Sources describe the fixed Alpine package closure, not a single historical
    # rootfs build. Bind the new sidecar to its actual guest below; keep the
    # collection's original source/image hashes as historical evidence only.
    package_inputs = {"base_archive_sha256": provenance.get("upstreams", {}).get("alpine_minirootfs", {}).get("sha256"),
                      "package_lock_sha256": _hash_bytes(files["packages.lock.json"])}
    if lock.get("package_inputs") != package_inputs:
        raise ValueError("guest package input binding differs from source lock")
    actual_groups = validate_inventory_binding(inventory, lock)
    expected = lock.get("expected", {})
    if len(inventory.get("packages", [])) != expected.get("packages") or len(actual_groups) != expected.get("groups"):
        raise ValueError("guest inventory package/group counts do not match checked lock")
    if len(lock.get("source_inputs", [])) != expected.get("source_inputs") or len(lock.get("recipe_files", [])) != expected.get("recipe_files"):
        raise ValueError("source lock input counts do not match checked-lock expectations")
    locked_pairs = {(x.get("origin"), x.get("commit")) for x in lock.get("groups", [])}
    if locked_pairs != set(actual_groups):
        raise ValueError("source lock origin/commit groups do not match guest inventory")
    archive_free = {(x.get("origin"), x.get("commit")) for x in lock.get("archive_free_groups", [])}
    if not archive_free <= locked_pairs:
        raise ValueError("archive-free source group is not installed")
    for kind, records, name_fn, expected_pairs in (
        ("source input", lock["source_inputs"], _source_member_name, locked_pairs - archive_free),
        ("recipe input", lock["recipe_files"], _recipe_member_name, locked_pairs),
    ):
        pairs = {_validate_record(record, kind) for record in records}
        if pairs != expected_pairs:
            raise ValueError(kind + " groups do not cover the exact installed source groups")
        names = [name_fn(record) for record in records]
        if len(names) != len(set(names)):
            raise ValueError(kind + " records repeat an archive destination")
    input_root = Path(input_dir) if input_dir else Path(tempfile.gettempdir()) / "ashnazg-source-cache"
    overall_deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
    total_input_bytes, member_sources = sum(map(len, files.values())), {}
    for record in lock["source_inputs"]:
        _validate_record(record, "source")
        if not isinstance(record.get("declared_sha512"), str) or not SHA512_RE.fullmatch(record["declared_sha512"]):
            raise ValueError("source record must include a declared SHA-512")
        data = _cached_or_download(input_root, record, remaining=MAX_TOTAL_BYTES - total_input_bytes,
                                   deadline=overall_deadline)
        total_input_bytes += len(data)
        member_sources[_source_member_name(record)] = data
    recipe_members = {}
    for record in lock["recipe_files"]:
        _validate_record(record, "recipe")
        if record.get("repository") not in {"main", "community"}:
            raise ValueError("recipe repository must be main or community")
        name = _recipe_member_name(record)
        data = _cached_or_download(input_root, record, remaining=MAX_TOTAL_BYTES - total_input_bytes,
                                   deadline=overall_deadline)
        total_input_bytes += len(data)
        recipe_members[name] = data
    notice_members = {}
    for record in lock.get("notice_candidates", []):
        origin, commit = record.get("origin"), record.get("commit")
        if not isinstance(origin, str) or not ORIGIN_RE.fullmatch(origin) or not isinstance(commit, str) or not SOURCE_SHA_RE.fullmatch(commit):
            raise ValueError("invalid notice candidate origin/commit")
        if type(record.get("size")) is not int or not 0 <= record["size"] <= MAX_INPUT_BYTES or not SHA256_RE.fullmatch(record.get("sha256", "")):
            raise ValueError("invalid notice candidate size or SHA-256")
        candidate_id = record.get("candidate_id")
        if not isinstance(candidate_id, str) or not re.fullmatch(r"candidate-[0-9]{6}", candidate_id):
            raise ValueError("notice candidate id is malformed")
    if sum(record["size"] for record in lock.get("notice_candidates", [])) > MAX_TOTAL_BYTES - total_input_bytes:
        raise ValueError("notice evidence exceeds remaining aggregate budget")
    notice_bytes = _collect_notice_data(lock.get("notice_candidates", []), input_root,
                                        lock["source_inputs"], member_sources,
                                        lock["recipe_files"], recipe_members)
    for record in lock.get("notice_candidates", []):
        origin, commit = record.get("origin"), record.get("commit")
        candidate_id = record.get("candidate_id")
        data = notice_bytes[candidate_id]
        total_input_bytes += len(data)
        notice_members["notice-evidence/" + origin + "/" + candidate_id + ".bin"] = data
    expected_empty = {(g["origin"], g["commit"]) for g in lock.get("notice_empty_groups", [])}
    notice_pairs = {(r["origin"], r["commit"]) for r in lock.get("notice_candidates", [])}
    if expected_empty & notice_pairs or (expected_empty | notice_pairs) - locked_pairs:
        raise ValueError("notice status records do not bind to exact locked origin/commit groups")
    if expected_empty | notice_pairs != locked_pairs:
        raise ValueError("notice evidence must state candidates or explicit unavailable status for every group")
    for record in lock.get("supplements", []):
        _validate_transfer_record(record, "supplement")
        data = _cached_or_download(input_root, record, remaining=MAX_TOTAL_BYTES - total_input_bytes,
                                   deadline=overall_deadline)
        total_input_bytes += len(data)
        notice_members["supplemental-texts/" + _safe_relpath(record["filename"], max_parts=1)] = data
    project_files, project_modes = _collect_project(repo_root, source_sha, remaining=MAX_TOTAL_BYTES - total_input_bytes)
    total_input_bytes += sum(map(len, project_files.values()))
    source_cargo_lock = project_files.get("Cargo.lock")
    if source_cargo_lock is None:
        raise ValueError("exact project source SHA has no Cargo.lock")
    rust_files = _collect_rust_vendor(rust_vendor_dir, source_cargo_lock, remaining=MAX_TOTAL_BYTES - total_input_bytes)
    all_data = list(member_sources.values()) + list(recipe_members.values()) + list(notice_members.values()) + list(files.values()) + list(project_files.values()) + list(rust_files.values())
    if sum(map(len, all_data)) > MAX_TOTAL_BYTES:
        raise ValueError("source sidecar uncompressed content exceeds 2 GiB bound")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".tmp-", dir=str(output.parent))
    os.close(fd)
    temp_output = Path(temp_name)
    index = {
        "schema_version": 1, "version": version, "source_sha": source_sha,
        "guest": {"rootfs_sha256": asset_hashes["rootfs.ext4"][1],
                  "runtime_components_sha256": _hash_bytes(files["runtime-components.json"]),
                  "runtime_installed_db_sha256": _hash_bytes(files["runtime-installed.db"]),
                  "package_count": len(inventory["packages"]), "origin_commit_group_count": len(actual_groups)},
        "source_closure": {"source_input_count": len(member_sources), "recipe_support_file_count": len(recipe_members),
                           "notice_candidate_count": len(notice_members) - len(lock.get("supplements", [])),
                           "notice_empty_groups": lock.get("notice_empty_groups", []),
                           "legal_clearance": False,
                           "notice_scope": "original candidate evidence retained; candidates are not a legal completeness or clearance determination"},
        "notice_evidence": lock.get("notice_evidence", {}),
        "base_archive": provenance.get("upstreams", {}).get("alpine_minirootfs", {}),
        "package_inputs": package_inputs,
        "collected_from": lock.get("collected_from", {}),
        "members": {},
    }
    try:
        with tarfile.open(temp_output, mode="w:xz", format=tarfile.PAX_FORMAT) as archive:
            members = {}
            for name, data in sorted(member_sources.items()):
                _add_member(archive, name, data, members)
            for name, data in sorted(recipe_members.items()):
                _add_member(archive, name, data, members)
            for name, data in sorted(notice_members.items()):
                _add_member(archive, name, data, members)
            for name in SIDECAR_ASSET_NAMES:
                _add_member(archive, "guest/" + name, files[name], members)
            for name, data in sorted(project_files.items()):
                _add_member(archive, "project/" + name, data, members, mode=project_modes[name])
            for name, data in sorted(rust_files.items()):
                _add_member(archive, name, data, members)
            _add_member(archive, "source-lock.json", _json_bytes(lock), members)
            _add_member(archive, "notice-evidence.json", _json_bytes(lock.get("notice_evidence", {})), members)
            index["members"] = members
            _add_member(archive, "MANIFEST.json", _json_bytes(index), {})
        os.link(temp_output, output, follow_symlinks=False)
        temp_output.unlink()
    except BaseException:
        try:
            temp_output.unlink()
        except FileNotFoundError:
            pass
        raise
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", required=True, help="directory with exactly eleven flat guest bundle members")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--input-dir", help="optional verified cache with lock-relative source/recipe/notice paths")
    parser.add_argument("--rust-vendor", required=True, help="directory produced by cargo vendor --locked")
    parser.add_argument("--output", required=True)
    parser.add_argument("--version", default="0.3.0")
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args(argv)
    try:
        result = build_source_sidecar(args.assets, args.repo_root, args.output, args.version,
                                      args.source_sha, args.input_dir, args.rust_vendor)
    except (ValueError, OSError, subprocess.SubprocessError, urllib.error.URLError, tarfile.TarError) as error:
        print("runtime source sidecar: " + str(error), file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
