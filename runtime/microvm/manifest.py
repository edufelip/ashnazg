"""Validation for the fixed v0.3 guest artifact bundle contract."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

EXPECTED_GUEST = {
    "kernel_version": "6.12.117",
    "base": "alpine-3.22.8-aarch64",
    "redroid_index": "sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad",
}
ARTIFACTS = ("kernel", "rootfs.ext4")


class ManifestError(ValueError):
    pass


def _read_regular_file(root: Path, name: str) -> Path:
    path = root / name
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ManifestError(f"missing artifact {name}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ManifestError(f"artifact {name} must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise ManifestError(f"artifact {name} must be a regular file")
    return path


def _open_regular_file(root: Path, name: str):
    path = _read_regular_file(root, name)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ManifestError(f"cannot open regular artifact {name}: {exc}") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ManifestError(f"artifact {name} must be a regular file")
    return os.fdopen(descriptor, "rb")


def validate_manifest(assets_dir: Path, *, expected_architecture: str = "aarch64") -> dict:
    root = Path(assets_dir)
    if root.is_symlink() or not root.is_dir():
        raise ManifestError("asset directory must be a real directory")
    try:
        with _open_regular_file(root, "manifest.json") as source:
            manifest = json.loads(source.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest.json is invalid: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ManifestError("unsupported manifest schema_version")
    if manifest.get("version") != "0.3.0":
        raise ManifestError("manifest version must be 0.3.0")
    if manifest.get("architecture") != expected_architecture:
        raise ManifestError(f"unsupported architecture: {manifest.get('architecture')!r}")
    source_sha = manifest.get("source_sha")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ManifestError("source_sha must be a lowercase 40-character Git commit")
    guest = manifest.get("guest")
    if not isinstance(guest, dict) or any(guest.get(key) != value for key, value in EXPECTED_GUEST.items()):
        raise ManifestError("guest versions or pinned ReDroid index do not match the contract")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(ARTIFACTS):
        raise ManifestError("manifest files must contain exactly kernel and rootfs.ext4")
    for name in ARTIFACTS:
        metadata = files[name]
        if not isinstance(metadata, dict):
            raise ManifestError(f"invalid metadata for {name}")
        digest = metadata.get("sha256")
        size = metadata.get("size")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ManifestError(f"invalid SHA256 for {name}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            raise ManifestError(f"invalid size for {name}")
        _read_regular_file(root, name)
        hasher = hashlib.sha256()
        with _open_regular_file(root, name) as artifact:
            if os.fstat(artifact.fileno()).st_size != size:
                raise ManifestError(f"size mismatch for {name}")
            for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise ManifestError(f"SHA256 digest mismatch for {name}")
    return manifest
