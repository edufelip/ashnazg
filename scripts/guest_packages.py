#!/usr/bin/env python3
"""Create, fetch, and verify the authenticated Alpine guest APK lock proposal."""

import argparse
import copy
import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from urllib.request import urlopen


ALPINE_VERSION = "3.22.8"
ALPINE_BASE_URL = (
    "https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/aarch64/"
    f"alpine-minirootfs-{ALPINE_VERSION}-aarch64.tar.gz"
)
ALPINE_BASE_SHA256 = "36091efacd083c1cf5a8c837272530bff4fc72db1fbfdef559f52de0d890bca4"
ALPINE_REPOSITORY_ROOT = "https://dl-cdn.alpinelinux.org/alpine/v3.22"
REQUESTED_PACKAGES = (
    "android-tools",
    "ca-certificates",
    "docker",
    "docker-cli-buildx",
    "iproute2",
    "iptables",
    "python3",
    "util-linux",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PACKAGE_NAME_RE = re.compile(r"^[a-zA-Z0-9+_.-]+$")
PACKAGE_VERSION_RE = re.compile(r"^[a-zA-Z0-9+_.~:-]+$")
APK_URL_RE = re.compile(
    r"^https://dl-cdn\.alpinelinux\.org/alpine/v3\.22/"
    r"(?:main|community)/aarch64/([A-Za-z0-9+_.-]+)\.apk$"
)
INDEX_URL_RE = re.compile(
    r"^https://dl-cdn\.alpinelinux\.org/alpine/v3\.22/(main|community)$"
)
PKGINFO_MAX_BYTES = 1024 * 1024


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_base(base):
    if not isinstance(base, dict) or set(base) != {"url", "sha256"}:
        raise ValueError("base must contain exactly url and sha256")
    if base["url"] != ALPINE_BASE_URL or base["sha256"] != ALPINE_BASE_SHA256:
        raise ValueError("base URL and SHA-256 must match the pinned Alpine 3.22.8 rootfs")


def _validate_lock(lock):
    expected = {"schema_version", "architecture", "base", "source", "indexes", "packages"}
    if not isinstance(lock, dict) or set(lock) != expected:
        raise ValueError("lock must contain schema_version, architecture, base, source, indexes, packages")
    if lock["schema_version"] != 1:
        raise ValueError("unsupported lock schema_version")
    if lock["architecture"] != "aarch64":
        raise ValueError("lock architecture must be aarch64 or noarch")
    _validate_base(lock["base"])
    if not isinstance(lock["source"], str) or not lock["source"].strip():
        raise ValueError("source must document the authenticated Alpine package source")
    indexes = lock["indexes"]
    if not isinstance(indexes, list) or not indexes:
        raise ValueError("indexes must contain authenticated original index hashes")
    seen_repositories = set()
    for index in indexes:
        if not isinstance(index, dict) or set(index) != {"repository", "filename", "sha256"}:
            raise ValueError("each index must contain repository, filename, and sha256")
        if not isinstance(index["repository"], str) or not INDEX_URL_RE.fullmatch(index["repository"]):
            raise ValueError("index repository URL is not an official Alpine v3.22 repository")
        if index["repository"] in seen_repositories:
            raise ValueError("duplicate index repository")
        seen_repositories.add(index["repository"])
        if not isinstance(index["filename"], str) or not re.fullmatch(r"APKINDEX(?:\.[A-Za-z0-9_.-]+)?", index["filename"]):
            raise ValueError("invalid APKINDEX filename")
        if not isinstance(index["sha256"], str) or not SHA256_RE.fullmatch(index["sha256"]):
            raise ValueError("invalid APKINDEX SHA-256")
    packages = lock["packages"]
    if not isinstance(packages, list) or not packages:
        raise ValueError("packages must be a non-empty list")
    seen_names, seen_files = set(), set()
    for package in packages:
        fields = {"name", "version", "architecture", "url", "filename", "size", "sha256"}
        if not isinstance(package, dict) or set(package) != fields:
            raise ValueError("each package must contain name, version, architecture, url, filename, size, sha256")
        name, version = package["name"], package["version"]
        if not isinstance(name, str) or not PACKAGE_NAME_RE.fullmatch(name):
            raise ValueError("invalid package name")
        if not isinstance(version, str) or not PACKAGE_VERSION_RE.fullmatch(version):
            raise ValueError("invalid package version")
        if package["architecture"] not in {"aarch64", "noarch"}:
            raise ValueError(f"package {name} architecture must be aarch64 or noarch")
        filename = f"{name}-{version}.apk"
        if package["filename"] != filename or PurePosixPath(filename).name != filename:
            raise ValueError(f"invalid package filename for {name}")
        match = APK_URL_RE.fullmatch(package["url"] if isinstance(package["url"], str) else "")
        if not match or match.group(1) + ".apk" != filename:
            raise ValueError(f"package URL for {name} must be an official Alpine v3.22 aarch64 URL matching its filename")
        if isinstance(package["size"], bool) or not isinstance(package["size"], int) or package["size"] <= 0:
            raise ValueError(f"invalid package size for {name}")
        if not isinstance(package["sha256"], str) or not SHA256_RE.fullmatch(package["sha256"]):
            raise ValueError(f"invalid package SHA-256 for {name}")
        if name in seen_names or filename in seen_files:
            raise ValueError("duplicate package name or filename")
        seen_names.add(name)
        seen_files.add(filename)
    return lock


def load_lock(path):
    try:
        lock = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read package lock: {error}") from error
    return _validate_lock(lock)


def _pkginfo(path):
    try:
        with tarfile.open(path, mode="r:gz", ignore_zeros=True) as archive:
            matches = [item for item in archive if item.name == ".PKGINFO"]
            if len(matches) != 1 or not matches[0].isfile() or matches[0].size > PKGINFO_MAX_BYTES:
                raise ValueError(f"{Path(path).name} has missing, duplicate, or oversized .PKGINFO")
            stream = archive.extractfile(matches[0])
            if stream is None:
                raise ValueError(f"{Path(path).name} has unreadable .PKGINFO")
            content = stream.read(PKGINFO_MAX_BYTES + 1)
    except (OSError, tarfile.TarError, EOFError) as error:
        raise ValueError(f"{Path(path).name} is not a readable APK control archive") from error
    if len(content) > PKGINFO_MAX_BYTES:
        raise ValueError(f"{Path(path).name} has oversized .PKGINFO")
    metadata = {}
    for line in content.decode("utf-8", errors="strict").splitlines():
        if not line or line.startswith("#") or " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        if key in {"pkgname", "pkgver", "arch"}:
            if key in metadata:
                raise ValueError(f"{Path(path).name} has duplicate .PKGINFO {key}")
            metadata[key] = value
    if set(metadata) != {"pkgname", "pkgver", "arch"}:
        raise ValueError(f"{Path(path).name} .PKGINFO lacks package identity fields")
    return metadata


def _check_apk(path, package):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"package is not a regular file: {path.name}")
    size = path.stat().st_size
    if size != package["size"]:
        raise ValueError(f"size mismatch for {path.name}: expected {package['size']}, got {size}")
    digest = sha256_file(path)
    if digest != package["sha256"]:
        raise ValueError(f"SHA-256 mismatch for {path.name}")
    metadata = _pkginfo(path)
    for key, field in (("name", "pkgname"), ("version", "pkgver"), ("architecture", "arch")):
        if metadata[field] != package[key]:
            raise ValueError(f"APK .PKGINFO {field} mismatch for {path.name}")


def verify(lock_path, directory):
    lock = load_lock(lock_path)
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"package directory is not a regular directory: {root}")
    expected = {package["filename"]: package for package in lock["packages"]}
    found_apks = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed in package directory: {path}")
        if path.is_file() and path.suffix == ".apk":
            relative = path.relative_to(root).as_posix()
            if relative not in expected:
                raise ValueError(f"extra APK in package directory: {relative}")
            found_apks.add(relative)
    missing = set(expected) - found_apks
    if missing:
        raise ValueError("missing APKs: " + ", ".join(sorted(missing)))
    for filename, package in expected.items():
        _check_apk(root / filename, package)
    return {"package_count": len(expected), "architecture": lock["architecture"]}


def fetch(lock_path, output_dir):
    lock = load_lock(lock_path)
    output = Path(output_dir)
    if output.is_symlink():
        raise ValueError(f"output directory must not be a symlink: {output}")
    output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir() or any(output.iterdir()):
        raise ValueError("output directory must be empty")
    with tempfile.TemporaryDirectory(prefix="guest-apks-", dir=str(output)) as staging_name:
        staging = Path(staging_name)
        for package in lock["packages"]:
            destination = staging / package["filename"]
            request_url = package["url"]
            parsed = urlparse(request_url)
            if parsed.scheme != "https" or parsed.hostname != "dl-cdn.alpinelinux.org":
                raise ValueError(f"untrusted package URL: {request_url}")
            digest = hashlib.sha256()
            size = 0
            try:
                with urlopen(request_url, timeout=60) as response, destination.open("xb") as target:
                    final_url = response.geturl()
                    if final_url != request_url:
                        raise ValueError(f"package URL redirected away from the locked source: {request_url}")
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > package["size"]:
                            raise ValueError(f"size mismatch for {package['filename']}")
                        digest.update(chunk)
                        target.write(chunk)
            except OSError as error:
                raise ValueError(f"download failed for {package['filename']}: {error}") from error
            if size != package["size"]:
                raise ValueError(f"size mismatch for {package['filename']}: expected {package['size']}, got {size}")
            if digest.hexdigest() != package["sha256"]:
                raise ValueError(f"SHA-256 mismatch for {package['filename']}")
            _check_apk(destination, package)
        for package in lock["packages"]:
            os.replace(staging / package["filename"], output / package["filename"])
    return verify(lock_path, output)


def _run_chroot(rootfs, arguments, *, capture=False):
    return subprocess.run(
        ["chroot", str(rootfs), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )


def _safe_extract_rootfs(archive_path, destination):
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = []
        for original in archive.getmembers():
            member = original
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError(f"unsafe path in pinned rootfs: {member.name}")
            if member.issym():
                target = PurePosixPath(member.linkname)
                rooted_target = posixpath.normpath(
                    member.linkname.lstrip("/") if target.is_absolute()
                    else posixpath.join(posixpath.dirname(member.name), member.linkname)
                )
                if rooted_target == ".." or rooted_target.startswith("../"):
                    raise ValueError(f"unsafe link in pinned rootfs: {member.name}")
                if target.is_absolute():
                    member = copy.copy(member)
                    member.linkname = posixpath.relpath(rooted_target, posixpath.dirname(member.name) or ".")
            elif member.islnk():
                target = PurePosixPath(member.linkname)
                rooted_target = posixpath.normpath(member.linkname.lstrip("/"))
                if rooted_target == ".." or rooted_target.startswith("../"):
                    raise ValueError(f"unsafe hard link in pinned rootfs: {member.name}")
                if target.is_absolute():
                    member = copy.copy(member)
                    member.linkname = rooted_target
            members.append(member)
        archive.extractall(destination, members=members)


def _native_bootstrap(output_dir):
    if not sys.platform.startswith("linux") or platform.machine().lower() not in {"aarch64", "arm64"}:
        raise ValueError("bootstrap requires native Linux aarch64")
    if os.geteuid() != 0:
        raise ValueError("bootstrap requires root to run chroot and verify signed APKs")
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        raise ValueError("bootstrap is restricted to a disposable GitHub Actions runner")

    output = Path(output_dir)
    if output.is_symlink():
        raise ValueError(f"output directory must not be a symlink: {output}")
    output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir() or any(output.iterdir()):
        raise ValueError("output directory must be empty")

    with tempfile.TemporaryDirectory(prefix="guest-package-rootfs-") as temp_name:
        work = Path(temp_name)
        rootfs = work / "rootfs"
        rootfs.mkdir()
        base_archive = work / "alpine-minirootfs.tar.gz"
        digest = hashlib.sha256()
        with urlopen(ALPINE_BASE_URL, timeout=120) as response, base_archive.open("xb") as target:
            if response.geturl() != ALPINE_BASE_URL:
                raise ValueError("pinned Alpine rootfs URL redirected")
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != ALPINE_BASE_SHA256:
            raise ValueError("pinned Alpine rootfs SHA-256 mismatch")
        _safe_extract_rootfs(base_archive, rootfs)
        # APK post-install scripts need these devices. Create them only inside
        # this fresh owned chroot; do not bind the runner's /dev or /proc.
        for name in ('null', 'zero', 'random', 'urandom'):
            target = rootfs / 'dev' / name
            if target.exists() or target.is_symlink():
                target.unlink()
            original = (Path('/dev') / name).stat()
            if not stat.S_ISCHR(original.st_mode):
                raise ValueError(f'runner /dev/{name} is not a character device')
            os.mknod(target, stat.S_IFCHR | 0o666, original.st_rdev)

        resolver = rootfs / 'etc/resolv.conf'
        if resolver.exists() or resolver.is_symlink(): resolver.unlink()
        resolver.write_bytes(Path('/etc/resolv.conf').read_bytes())
        common_cache = rootfs / 'tmp/guest-package-index-cache'
        common_cache.mkdir()
        combined_repositories = rootfs / 'tmp/guest-package-repositories'
        combined_repositories.write_text(''.join(ALPINE_REPOSITORY_ROOT + '/' + n + '\n'
                                               for n in ('main', 'community')))
        apk_cache = rootfs / "tmp/guest-package-apks"
        apk_cache.mkdir(parents=True)
        requested = list(REQUESTED_PACKAGES)
        indexes = []
        for repository_name in ("main", "community"):
            repository = ALPINE_REPOSITORY_ROOT + "/" + repository_name
            repository_file = rootfs / f"tmp/guest-package-{repository_name}-repositories"
            repository_file.write_text(repository + "\n", encoding="utf-8")
            index_cache = rootfs / f"tmp/guest-package-index-{repository_name}"
            index_cache.mkdir()
            _run_chroot(rootfs, ["/sbin/apk", "--repositories-file", f"/tmp/guest-package-{repository_name}-repositories",
                                 "--cache-dir", f"/tmp/guest-package-index-{repository_name}", "update"])
            index_files = sorted(index_cache.glob("APKINDEX.*"))
            if len(index_files) != 1:
                raise ValueError(f"native apk update for {repository_name} did not leave exactly one APKINDEX")
            shutil.copy2(index_files[0], common_cache / index_files[0].name)
            indexes.append({
                "repository": repository,
                "filename": index_files[0].name,
                "sha256": sha256_file(index_files[0]),
            })

        catalog_flags = ['--repositories-file', '/tmp/guest-package-repositories',
                         '--cache-dir', '/tmp/guest-package-index-cache']
        url_result = _run_chroot(
            rootfs,
            ["/sbin/apk", *catalog_flags, "--no-network", "fetch", "--recursive", "--url", *requested],
            capture=True,
        )
        urls = [line.strip() for line in url_result.stdout.splitlines() if line.strip()]
        by_filename = {}
        for url in urls:
            match = APK_URL_RE.fullmatch(url)
            if not match:
                raise ValueError(f"native apk fetch returned a non-official package URL: {url}")
            filename = match.group(1) + ".apk"
            if filename in by_filename:
                raise ValueError(f"native apk fetch returned duplicate package URL: {url}")
            by_filename[filename] = url

        # --url lists sources; it deliberately does not download package bytes.
        _run_chroot(rootfs, ['/sbin/apk', *catalog_flags, 'fetch', '--recursive',
                             '--output', '/tmp/guest-package-apks', *requested])
        apk_files = sorted(apk_cache.glob("*.apk"))
        if len(apk_files) != len(by_filename) or {path.name for path in apk_files} != set(by_filename):
            raise ValueError("native apk fetch URL listing does not match the downloaded recursive closure")

        packages = []
        for path in apk_files:
            metadata = _pkginfo(path)
            filename = path.name
            package = {
                "name": metadata["pkgname"],
                "version": metadata["pkgver"],
                "architecture": metadata["arch"],
                "url": by_filename[filename],
                "filename": filename,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            if filename != f"{package['name']}-{package['version']}.apk":
                raise ValueError(f"APK filename does not match its actual .PKGINFO identity: {filename}")
            packages.append(package)
        if not set(REQUESTED_PACKAGES).issubset({package["name"] for package in packages}):
            raise ValueError("recursive APK closure does not contain all requested package roots")

        empty_repositories = rootfs / "tmp/guest-package-empty-repositories"
        empty_repositories.write_text("", encoding="utf-8")
        # Validate against the actual pinned base, including its existing package DB
        # and trusted keys. Every added/updated package must be a local locked APK.
        _run_chroot(rootfs, ["/sbin/apk", "--repositories-file",
                             "/tmp/guest-package-empty-repositories", "--no-network", "add",
                             *[f"/tmp/guest-package-apks/{path.name}" for path in apk_files]])

        lock = {
            "schema_version": 1,
            "architecture": "aarch64",
            "base": {"url": ALPINE_BASE_URL, "sha256": ALPINE_BASE_SHA256},
            "source": "Alpine v3.22 repositories; native apk update authenticated the original indexes using the pinned rootfs keys; native apk add verified package signatures and dependency closure offline.",
            "indexes": indexes,
            "packages": sorted(packages, key=lambda package: (package["name"], package["version"])),
        }
        _validate_lock(lock)
        (output / "packages.lock.json").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        index_proof = {
            "base": lock["base"],
            "source": lock["source"],
            "indexes": lock["indexes"],
        }
        (output / "index-proof.json").write_text(
            json.dumps(index_proof, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        summary = {
            "architecture": "aarch64",
            "base_sha256": ALPINE_BASE_SHA256,
            "index_count": len(indexes),
            "package_count": len(packages),
            "requested_packages": list(REQUESTED_PACKAGES),
            "offline_signature_and_dependency_validation": "passed",
            "qualification": "authenticated package snapshot proposal; no kernel build or boot validation claimed",
        }
        (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap_parser = subparsers.add_parser("bootstrap", help="create a package lock proposal on Linux ARM64 CI")
    bootstrap_parser.add_argument("--output-dir", required=True)
    fetch_parser = subparsers.add_parser("fetch", help="fetch and verify the exact APK bytes named by a lock")
    fetch_parser.add_argument("--lock", required=True)
    fetch_parser.add_argument("--output-dir", required=True)
    verify_parser = subparsers.add_parser("verify", help="verify a local APK directory against a lock")
    verify_parser.add_argument("--lock", required=True)
    verify_parser.add_argument("--directory", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "bootstrap":
            _native_bootstrap(args.output_dir)
        elif args.command == "fetch":
            result = fetch(args.lock, args.output_dir)
            print(json.dumps(result, sort_keys=True))
        else:
            result = verify(args.lock, args.directory)
            print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, subprocess.CalledProcessError, UnicodeError, tarfile.TarError) as error:
        print(f"guest_packages: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
