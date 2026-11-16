import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import rust_notices


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _package(vendor, name, version, files, *, metadata=""):
    package = vendor / (name + "-" + version)
    package.mkdir(parents=True)
    cargo_toml = (
        '[package]\nname = "' + name + '"\nversion = "' + version + '"\n'
        + metadata + "\n"
    ).encode("utf-8")
    (package / "Cargo.toml").write_bytes(cargo_toml)
    checksums = {"Cargo.toml": _sha(cargo_toml)}
    for relative, content in files.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        checksums[relative] = _sha(content)
    checksum = ("a" if name == "alpha" else "b") * 64
    (package / ".cargo-checksum.json").write_text(json.dumps({"files": checksums, "package": checksum}))
    return checksum


def _fixture(root, *, include_candidate=True):
    vendor = root / "vendor"
    vendor.mkdir()
    alpha_files = {"LICENSE-MIT": b"alpha license\x00bytes\n"} if include_candidate else {"src/lib.rs": b"source"}
    alpha_checksum = _package(
        vendor, "alpha", "1.2.3", alpha_files,
        metadata='license = "MIT OR Apache-2.0"\nlicense-file = "LICENSE-MIT"',
    )
    beta_checksum = _package(
        vendor, "beta", "0.4.0", {"nested/CoPyRiGhT.txt": b"beta notice"},
        metadata="license = \"BSD-3-Clause\"",
    )
    lock = root / "Cargo.lock"
    lock.write_text(
        '[[package]]\nname = "alpha"\nversion = "1.2.3"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "' + alpha_checksum + '"\n\n'
        '[[package]]\nname = "beta"\nversion = "0.4.0"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "' + beta_checksum + '"\n'
    )
    return vendor, lock


class RustNoticesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.vendor, self.lock = _fixture(self.root)

    def _build(self, output):
        rust_notices.build_rust_notices(self.vendor, self.lock, output)

    def test_deterministic_tar_keeps_exact_notice_bytes_and_accounts_for_every_crate(self):
        first, second = self.root / "one.tar.xz", self.root / "two.tar.xz"
        self._build(first)
        self._build(second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first, "r:xz") as archive:
            members = archive.getmembers()
            self.assertTrue(all(member.uid == member.gid == member.mtime == 0 for member in members))
            self.assertTrue(all(member.mode == 0o644 for member in members))
            self.assertEqual([member.name for member in members], [
                "MANIFEST.json",
                "rust-notices/alpha/1.2.3/LICENSE-MIT",
                "rust-notices/beta/0.4.0/nested/CoPyRiGhT.txt",
            ])
            self.assertEqual(archive.extractfile(members[1]).read(), b"alpha license\x00bytes\n")
            manifest = json.loads(archive.extractfile(members[0]).read())
        self.assertEqual([(item["name"], item["version"]) for item in manifest["crates"]],
                         [("alpha", "1.2.3"), ("beta", "0.4.0")])
        alpha, beta = manifest["crates"]
        self.assertEqual(alpha["license"], "MIT OR Apache-2.0")
        self.assertEqual(alpha["license_file"], "LICENSE-MIT")
        self.assertEqual(alpha["notice_files"][0]["sha256"], _sha(b"alpha license\x00bytes\n"))
        self.assertEqual(beta["notice_files"][0]["sha256"], _sha(b"beta notice"))

    def test_crate_without_notice_has_explicit_no_candidate_status(self):
        vendor = self.root / "vendor-empty"
        vendor.mkdir()
        checksum = _package(vendor, "empty", "1.0.0", {"src/lib.rs": b"source"})
        lock = self.root / "empty.lock"
        lock.write_text(
            '[[package]]\nname = "empty"\nversion = "1.0.0"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "' + checksum + '"\n'
        )
        output = self.root / "empty.tar.xz"
        rust_notices.build_rust_notices(vendor, lock, output)
        with tarfile.open(output, "r:xz") as archive:
            manifest = json.loads(archive.extractfile("MANIFEST.json").read())
        self.assertEqual(manifest["crates"][0]["notice_status"], "no_candidate")
        self.assertEqual(manifest["crates"][0]["notice_files"], [])

    def test_corrupt_vendor_is_rejected_before_archive_is_created(self):
        (self.vendor / "alpha-1.2.3" / "LICENSE-MIT").write_bytes(b"changed")
        output = self.root / "corrupt.tar.xz"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self._build(output)
        self.assertFalse(output.exists())

    def test_vendor_symlink_is_rejected(self):
        link = self.vendor / "alpha-1.2.3" / "LICENSE-link"
        link.symlink_to("LICENSE-MIT")
        output = self.root / "symlink.tar.xz"
        with self.assertRaisesRegex(ValueError, "link"):
            self._build(output)

    def test_traversing_license_file_path_is_rejected(self):
        package = self.vendor / "alpha-1.2.3"
        cargo_toml = b'[package]\nname = "alpha"\nversion = "1.2.3"\nlicense-file = "../escape"\n'
        (package / "Cargo.toml").write_bytes(cargo_toml)
        checksum_path = package / ".cargo-checksum.json"
        checksum = json.loads(checksum_path.read_text())
        checksum["files"]["Cargo.toml"] = _sha(cargo_toml)
        checksum_path.write_text(json.dumps(checksum))
        output = self.root / "traversal.tar.xz"
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self._build(output)

    def test_per_file_and_aggregate_bounds_are_enforced(self):
        with mock.patch.object(rust_notices, "MAX_NOTICE_FILE_BYTES", 4):
            with self.assertRaisesRegex(ValueError, "per-file"):
                self._build(self.root / "large.tar.xz")
        with mock.patch.object(rust_notices, "MAX_NOTICE_FILE_BYTES", 100), \
             mock.patch.object(rust_notices, "MAX_NOTICE_TOTAL_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "aggregate"):
                self._build(self.root / "aggregate.tar.xz")

    def test_output_refuses_to_overwrite_existing_file_or_symlink(self):
        output = self.root / "existing.tar.xz"
        output.write_bytes(b"caller data")
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            self._build(output)
        self.assertEqual(output.read_bytes(), b"caller data")
        target = self.root / "target"
        target.write_bytes(b"target")
        link = self.root / "output-link"
        link.symlink_to(target)
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            self._build(link)
        self.assertEqual(target.read_bytes(), b"target")

    def test_command_line_accepts_vendor_lock_and_output_options(self):
        output = self.root / "cli.tar.xz"
        script = Path(__file__).parents[1] / "scripts" / "rust_notices.py"
        result = subprocess.run([
            sys.executable, str(script), "--vendor", str(self.vendor),
            "--cargo-lock", str(self.lock), "--output", str(output),
        ], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
