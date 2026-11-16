"""Build a bounded, deterministic inventory of installed runtime package metadata."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import os
import re
import stat
import sys
import tarfile
from urllib.parse import urlsplit


MAX_DB_BYTES = 2 * 1024 * 1024
MAX_LOCK_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_TAR_MEMBERS = 10_000
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
PACKAGE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]{0,254}\Z", re.ASCII)
PACKAGE_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.~:-]{0,255}\Z", re.ASCII)
IDENTITY_FIELDS = {"P": "name", "V": "version", "A": "architecture"}
SOURCE_FIELDS = {
    "L": "declared_license", "o": "origin", "U": "upstream_url",
    "t": "build_time_unix", "c": "aports_commit",
}
REQUIRED_SOURCE_FIELDS = tuple(SOURCE_FIELDS.values())
TARGET_DB_PATHS = {"lib/apk/db/installed", "./lib/apk/db/installed"}
LOCK_FIELDS = {"schema_version", "architecture", "base", "source", "indexes", "packages"}
BASE_FIELDS = {"url", "sha256"}
INDEX_FIELDS = {"repository", "filename", "sha256"}
PACKAGE_FIELDS = {"name", "version", "architecture", "url", "filename", "size", "sha256"}


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json_constant(_value):
    raise ValueError("non-finite JSON number")


def _issue(code, message, **details):
    return {"code": code, "message": message, **details}


def _safe_string(value, *, maximum=4096):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum and "\x00" not in value


def _valid_http_url(value):
    if not _safe_string(value, maximum=2048):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme in {"http", "https"} and bool(parts.hostname) and not parts.username and not parts.password
    except (ValueError, UnicodeError):
        return False


def _validate_lock(raw):
    try:
        lock = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique_json_object,
                          parse_constant=_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError("package lock is not valid strict JSON") from None
    if not isinstance(lock, dict) or set(lock) != LOCK_FIELDS:
        raise ValueError("package lock has invalid fields")
    if type(lock["schema_version"]) is not int or lock["schema_version"] != 1:
        raise ValueError("package lock schema_version must be integer 1")
    if lock["architecture"] != "aarch64":
        raise ValueError("package lock architecture must be aarch64")
    base = lock["base"]
    if not isinstance(base, dict) or set(base) != BASE_FIELDS or not _valid_http_url(base.get("url")) or not isinstance(base.get("sha256"), str) or not SHA256_RE.fullmatch(base["sha256"]):
        raise ValueError("package lock base URL or SHA-256 is invalid")
    if not _safe_string(lock["source"], maximum=4096):
        raise ValueError("package lock source is invalid")
    indexes = lock["indexes"]
    if not isinstance(indexes, list) or not indexes:
        raise ValueError("package lock indexes must be a non-empty list")
    for item in indexes:
        if (not isinstance(item, dict) or set(item) != INDEX_FIELDS or
                not _valid_http_url(item.get("repository")) or
                not _safe_string(item.get("filename"), maximum=255) or
                not isinstance(item.get("sha256"), str) or not SHA256_RE.fullmatch(item["sha256"])):
            raise ValueError("package lock contains an invalid index record")
    packages = lock["packages"]
    if not isinstance(packages, list) or not packages:
        raise ValueError("package lock packages must be a non-empty list")
    seen_names, seen_identities, seen_filenames = set(), set(), set()
    for item in packages:
        if not isinstance(item, dict) or set(item) != PACKAGE_FIELDS:
            raise ValueError("package lock contains an invalid package record")
        name, version, arch = item["name"], item["version"], item["architecture"]
        if not isinstance(name, str) or not PACKAGE_NAME_RE.fullmatch(name):
            raise ValueError("package lock contains an invalid package name")
        if not isinstance(version, str) or not PACKAGE_VERSION_RE.fullmatch(version):
            raise ValueError("package lock contains an invalid package version")
        if not isinstance(arch, str) or arch not in {"aarch64", "noarch"}:
            raise ValueError("package lock package architecture is invalid")
        filename = item["filename"]
        if not isinstance(filename, str) or filename != f"{name}-{version}.apk":
            raise ValueError("package lock filename does not match package identity")
        if type(item["size"]) is not int or item["size"] <= 0:
            raise ValueError("package lock size must be a positive integer")
        if not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"]):
            raise ValueError("package lock package SHA-256 is invalid")
        if not _valid_http_url(item["url"]) or not item["url"].endswith("/" + filename):
            raise ValueError("package lock package URL is invalid")
        if name in seen_names or (name, version, arch) in seen_identities or filename in seen_filenames:
            raise ValueError("package lock contains duplicate package identity or filename")
        seen_names.add(name)
        seen_identities.add((name, version, arch))
        seen_filenames.add(filename)
    return lock


def _parse_db(raw, source_label, issues):
    if not isinstance(raw, bytes) or len(raw) > MAX_DB_BYTES:
        issues.append(_issue("invalid_database", f"{source_label} database is missing or exceeds 2 MiB"))
        return []
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        issues.append(_issue("invalid_database", f"{source_label} database is not valid UTF-8"))
        return []
    records = []
    seen_identity = set()
    seen_names = set()
    for index, block in enumerate(re.split(r"\n[\t ]*\n", text), start=1):
        if not block.strip():
            if source_label == "final" and not records and index == 1:
                issues.append(_issue("invalid_database", f"{source_label} database is empty"))
            continue
        fields = {}
        malformed = False
        in_file_list = False
        for line in block.splitlines():
            if not line:
                continue
            if len(line) > 8192 or "\x00" in line or ":" not in line:
                malformed = True
                break
            key, value = line.split(":", 1)
            if not re.fullmatch(r"[A-Za-z]", key, re.ASCII):
                malformed = True
                break
            if key == "F":
                in_file_list = True
            if in_file_list:
                continue
            if len(value) > 4096:
                malformed = True
                break
            values = fields.setdefault(key, [])
            values.append(value)
        if malformed:
            issues.append(_issue("malformed_database_record", f"{source_label} database record {index} is malformed"))
            continue
        if any(len(fields.get(field, [])) != 1 or not _safe_string(fields[field][0], maximum=255)
               for field in IDENTITY_FIELDS):
            issues.append(_issue("invalid_identity", f"{source_label} database record {index} has missing, duplicate, or invalid identity fields"))
            continue
        identity = tuple(fields[field][0] for field in ("P", "V", "A"))
        if not PACKAGE_NAME_RE.fullmatch(identity[0]) or not PACKAGE_VERSION_RE.fullmatch(identity[1]) or identity[2] not in {"aarch64", "noarch"}:
            issues.append(_issue("invalid_identity", f"{source_label} database record {index} has invalid package identity"))
            continue
        if identity in seen_identity or identity[0] in seen_names:
            issues.append(_issue("duplicate_package_record", f"{source_label} database repeats package identity {identity[0]} {identity[1]} {identity[2]}"))
            continue
        seen_identity.add(identity)
        seen_names.add(identity[0])
        raw_header = {key: (values[0] if len(values) == 1 else values) for key, values in sorted(fields.items())}
        package = {
            "name": identity[0], "version": identity[1], "architecture": identity[2],
            "origin": None, "declared_license": None, "upstream_url": None,
            "aports_commit": None, "build_time_unix": None,
            "raw_header_fields": raw_header, "missing_metadata": [],
            "provenance": None,
        }
        invalid_source = []
        for tag, output in SOURCE_FIELDS.items():
            values = fields.get(tag, [])
            if len(values) != 1:
                package["missing_metadata"].append(output)
                if len(values) > 1:
                    invalid_source.append(output)
                continue
            value = values[0]
            valid = _safe_string(value, maximum=2048)
            if output == "aports_commit":
                valid = isinstance(value, str) and bool(SOURCE_SHA_RE.fullmatch(value))
            elif output == "build_time_unix":
                valid = _parse_build_time(value) is not None
            elif output == "upstream_url":
                valid = _valid_http_url(value)
            elif output in {"origin", "declared_license"}:
                valid = _safe_string(value, maximum=255)
            if valid:
                package[output] = _parse_build_time(value) if output == "build_time_unix" else value
            else:
                package["missing_metadata"].append(output)
                invalid_source.append(output)
        package["missing_metadata"] = sorted(set(package["missing_metadata"]))
        if invalid_source:
            issues.append(_issue("invalid_source_metadata", f"{source_label} package {package['name']} has invalid source metadata", package=package["name"], fields=sorted(set(invalid_source))))
        records.append(package)
    records.sort(key=_package_key)
    return records


def _parse_build_time(value):
    if not isinstance(value, str) or len(value) > 20 or not re.fullmatch(r"-?[0-9]+", value, re.ASCII):
        return None
    try:
        result = int(value, 10)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(float(result)) else None


def _package_key(package):
    return package["name"], package["version"], package["architecture"]


def _read_base_db(archive_bytes):
    if not isinstance(archive_bytes, bytes) or len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise ValueError("compressed base archive is missing or exceeds 20 MiB")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(archive_bytes), mode="rb") as stream:
            expanded = stream.read(MAX_DECOMPRESSED_BYTES + 1)
            if len(expanded) > MAX_DECOMPRESSED_BYTES:
                raise ValueError("base archive exceeds 128 MiB decompressed limit")
            # Force gzip trailer/checksum validation, while retaining a hard bound.
            if stream.read(1):
                raise ValueError("base archive exceeds 128 MiB decompressed limit")
    except (OSError, EOFError, gzip.BadGzipFile, zlib_error()) as error:
        raise ValueError("base archive is truncated or invalid gzip") from error
    target = None
    found = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            for count, member in enumerate(archive, start=1):
                if count > MAX_TAR_MEMBERS:
                    raise ValueError("base archive exceeds 10000 members")
                normalized = member.name[2:] if member.name.startswith("./") else member.name
                if normalized != "lib/apk/db/installed":
                    continue
                found += 1
                if found > 1:
                    raise ValueError("base archive contains duplicate installed database members")
                if member.name not in TARGET_DB_PATHS or not member.isfile() or member.size > MAX_DB_BYTES:
                    raise ValueError("base installed database member is unsafe, non-regular, or oversized")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError("base installed database member is unreadable")
                target = extracted.read(MAX_DB_BYTES + 1)
                if len(target) != member.size or len(target) > MAX_DB_BYTES:
                    raise ValueError("base installed database member is truncated or oversized")
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ValueError("base archive is malformed or truncated") from error
    if found != 1 or target is None:
        raise ValueError("base archive lacks a unique regular installed database member")
    return target


def zlib_error():
    # Resolve lazily to keep the module's public dependencies minimal.
    import zlib
    return zlib.error


def _input_hash(value):
    return hashlib.sha256(value).hexdigest() if isinstance(value, bytes) else None


def build_inventory(*, installed_db: bytes, base_archive: bytes, package_lock: bytes,
                    rootfs_sha256: str, source_sha: str) -> dict:
    """Return an evidence inventory; malformed input is represented as issues."""
    issues = []
    db_bytes = installed_db if isinstance(installed_db, bytes) else b""
    archive_bytes = base_archive if isinstance(base_archive, bytes) else b""
    lock_bytes = package_lock if isinstance(package_lock, bytes) else b""
    if not isinstance(rootfs_sha256, str) or not SHA256_RE.fullmatch(rootfs_sha256):
        issues.append(_issue("invalid_rootfs_sha256", "rootfs_sha256 must be 64 lowercase hexadecimal characters"))
    if not isinstance(source_sha, str) or not SOURCE_SHA_RE.fullmatch(source_sha):
        issues.append(_issue("invalid_source_sha", "source_sha must be 40 lowercase hexadecimal characters"))
    lock = None
    try:
        if len(lock_bytes) > MAX_LOCK_BYTES:
            raise ValueError("package lock exceeds 2 MiB")
        lock = _validate_lock(lock_bytes)
    except ValueError as error:
        issues.append(_issue("invalid_package_lock", str(error)))
    db_records = _parse_db(db_bytes, "final", issues)
    base_records = []
    archive_hash = _input_hash(archive_bytes)
    base_db = None
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        issues.append(_issue("invalid_base_archive", "compressed base archive exceeds 20 MiB"))
    elif lock is not None and archive_hash != lock["base"]["sha256"]:
        issues.append(_issue("base_archive_hash_mismatch", "base archive SHA-256 does not match package lock"))
    elif lock is not None:
        try:
            base_db = _read_base_db(archive_bytes)
            base_records = _parse_db(base_db, "base", issues)
        except ValueError as error:
            issues.append(_issue("invalid_base_archive", str(error)))
    base_by_identity = {_package_key(package): package for package in base_records}
    lock_by_name = {item["name"]: item for item in lock["packages"]} if lock is not None else {}
    final_identities = set()
    all_mapped = True
    required_metadata_valid = True
    for package in db_records:
        identity = _package_key(package)
        final_identities.add(identity)
        lock_item = lock_by_name.get(package["name"])
        if lock_item is not None:
            locked_identity = (lock_item["name"], lock_item["version"], lock_item["architecture"])
            if identity == locked_identity:
                package["provenance"] = {"kind": "locked_apk", "filename": lock_item["filename"],
                                         "size": lock_item["size"], "sha256": lock_item["sha256"], "url": lock_item["url"]}
            elif (identity[1] == lock_item["version"] and
                  (base_package := base_by_identity.get(identity)) is not None and
                  package["raw_header_fields"] == base_package["raw_header_fields"]):
                # apk may retain a same-version base package when the downloaded
                # candidate uses a different architecture. Preserve both facts;
                # this does not attribute the installed bytes to that candidate.
                package["provenance"] = {
                    "kind": "base", "base_archive_sha256": archive_hash,
                    "locked_candidate": {**lock_item, "disposition": "not_installed_base_retained"},
                }
            else:
                package["provenance"] = {"kind": "unmapped"}
                all_mapped = False
                issues.append(_issue("locked_identity_mismatch", f"final package {package['name']} identity does not match its locked identity", package=package["name"]))
        else:
            base_package = base_by_identity.get(identity)
            metadata_fields = ("origin", "aports_commit", "declared_license", "upstream_url", "build_time_unix")
            same_metadata = base_package is not None and all(package[key] == base_package[key] for key in metadata_fields)
            if same_metadata:
                package["provenance"] = {"kind": "base", "base_archive_sha256": archive_hash}
            else:
                package["provenance"] = {"kind": "unmapped"}
                all_mapped = False
                issues.append(_issue("unmapped_package", f"final package {package['name']} has no exact locked or verified base identity and source metadata", package=package["name"]))
        if package["missing_metadata"]:
            required_metadata_valid = False
    if lock is not None:
        matched_names = {identity[0] for identity in final_identities}
        for item in lock["packages"]:
            if item["name"] not in matched_names:
                issues.append(_issue("locked_package_missing", f"locked package {item['name']} is absent from final installed database", package=item["name"]))
                all_mapped = False
    groups = {}
    for package in db_records:
        if package["origin"] is None or package["aports_commit"] is None:
            continue
        key = package["origin"], package["aports_commit"]
        groups.setdefault(key, []).append({"name": package["name"], "version": package["version"], "architecture": package["architecture"]})
    source_groups = [
        {"origin": origin, "aports_commit": commit,
         "packages": sorted(packages, key=lambda item: (item["name"], item["version"], item["architecture"]))}
        for (origin, commit), packages in sorted(groups.items())
    ]
    issues.sort(key=lambda item: (item["code"], item.get("package", ""), item["message"], json.dumps(item, sort_keys=True)))
    inputs = {"installed_db_sha256": _input_hash(db_bytes), "base_archive_sha256": archive_hash,
              "base_db_sha256": _input_hash(base_db) if base_db is not None else None,
              "package_lock_sha256": _input_hash(lock_bytes)}
    complete = lock is not None and all_mapped and required_metadata_valid and not any(
        issue["code"] in {"invalid_database", "malformed_database_record", "invalid_identity", "duplicate_package_record",
                          "invalid_base_archive", "base_archive_hash_mismatch", "invalid_rootfs_sha256", "invalid_source_sha"}
        for issue in issues)
    return {"schema_version": 1, "source_sha": source_sha, "rootfs_sha256": rootfs_sha256,
            "inputs": inputs, "packages": db_records, "source_groups": source_groups,
            "issues": issues, "metadata_status": "complete" if complete else "incomplete",
            "source_closure_status": "not_collected"}


def _read_input(path, maximum):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("input must be a bounded regular non-symlink file")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise ValueError("input exceeds size limit")
        return data
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed-db", required=True)
    parser.add_argument("--base-archive", required=True)
    parser.add_argument("--package-lock", required=True)
    parser.add_argument("--rootfs-sha256", required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args(argv)
    issues = []
    values = {}
    for label, path, limit in (("installed_db", args.installed_db, MAX_DB_BYTES),
                               ("base_archive", args.base_archive, MAX_ARCHIVE_BYTES),
                               ("package_lock", args.package_lock, MAX_LOCK_BYTES)):
        try:
            values[label] = _read_input(path, limit)
        except (OSError, ValueError) as error:
            issues.append(_issue("input_file_error", f"{label}: {error}"))
            values[label] = b""
    result = build_inventory(installed_db=values["installed_db"], base_archive=values["base_archive"],
                             package_lock=values["package_lock"], rootfs_sha256=args.rootfs_sha256,
                             source_sha=args.source_sha)
    result["issues"].extend(issues)
    result["issues"].sort(key=lambda item: (item["code"], item.get("package", ""), item["message"]))
    if issues:
        result["metadata_status"] = "incomplete"
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
    return 0 if result["metadata_status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
