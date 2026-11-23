import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import time
import unittest
import urllib.error
import zipfile
from unittest import mock
from pathlib import Path

from scripts.runtime_sources import (
    _collect_notice_data, _download, _source_member_name, build_source_sidecar,
    validate_inventory_binding, validate_rust_vendor,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture(root: Path):
    repo, assets, cache = root / "repo", root / "assets", root / "cache"
    repo.mkdir()
    assets.mkdir()
    cache.mkdir()
    (repo / "LICENSE").write_text("MIT\n")
    package_checksum = "d" * 64
    cargo_lock = ('[[package]]\nname = "serde"\nversion = "1.0.0"\n'
                  'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
                  'checksum = "' + package_checksum + '"\n')
    (repo / "Cargo.lock").write_text(cargo_lock)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "LICENSE", "Cargo.lock"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture source"], check=True)
    source_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                text=True, capture_output=True).stdout.strip()

    package = {"name": "demo", "version": "1.0-r0", "architecture": "aarch64"}
    inventory = {
        "schema_version": 1, "source_sha": source_sha, "rootfs_sha256": _sha(b"rootfs"),
        "metadata_status": "complete",
        "packages": [{**package, "origin": "demo", "aports_commit": "b" * 40,
                      "declared_license": "MIT"}],
        "source_groups": [{"origin": "demo", "aports_commit": "b" * 40, "packages": [package]}],
    }
    components = (json.dumps(inventory, sort_keys=True, indent=2) + "\n").encode()
    installed_db = b"P:demo\nV:1.0-r0\nA:aarch64\no:demo\nc:" + b"b" * 40 + b"\nL:MIT\n\n"
    guest_files = {"kernel": b"kernel", "rootfs.ext4": b"rootfs"}
    file_records = {name: {"size": len(data), "sha256": _sha(data)} for name, data in guest_files.items()}
    manifest = {"schema_version": 1, "version": "0.3.1", "architecture": "aarch64",
                "source_sha": source_sha, "guest": {"base": "alpine-3.22.8-aarch64",
                "kernel_version": "6.12.117"}, "files": file_records}
    provenance = {"schema_version": 1, "source_sha": source_sha, "files": file_records,
                  "component_inventory": {"name": "runtime-components.json", "sha256": _sha(components),
                  "installed_db_name": "runtime-installed.db", "installed_db_sha256": _sha(installed_db),
                  "metadata_status": "complete"},
                  "package_lock_sha256": _sha(b"{}\n"),
                  "kernel_config_fragment_sha256": _sha(b"CONFIG_TEST=y\n"),
                  "kernel_config_sha256": _sha(b"CONFIG_TEST=y\n"),
                  "upstreams": {"kernel": {"source_bundle_sha256": _sha(b"kernel source")},
                                "alpine_minirootfs": {"sha256": "c" * 64}}}
    asset_data = {
        "manifest.json": json.dumps(manifest, sort_keys=True).encode(),
        "provenance.json": json.dumps(provenance, sort_keys=True).encode(),
        **guest_files, "packages.lock.json": b"{}\n", "NOTICES.md": b"Existing guest notices\n",
        "kernel.config": b"CONFIG_TEST=y\n", "runtime-installed.db": installed_db,
        "kernel.generated.config": b"CONFIG_TEST=y\n", "runtime-components.json": components,
        "linux-6.12.117.tar.xz": b"kernel source",
    }
    for name, data in asset_data.items():
        (assets / name).write_bytes(data)

    source, recipe, notice = b"source archive", b"recipe patch", b"notice candidate"
    source_path = cache / "sources" / "demo" / "demo-1.0.tar.gz"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source)
    (cache / "recipes" / "demo").mkdir(parents=True)
    (cache / "recipes" / "demo" / "fix.patch").write_bytes(recipe)
    (cache / "notices").mkdir()
    (cache / "notices" / "candidate-000001.bin").write_bytes(notice)

    vendor = root / "vendor"
    package_dir = vendor / "serde-1.0.0"
    package_dir.mkdir(parents=True)
    cargo_toml = b'[package]\nname = "serde"\nversion = "1.0.0"\n'
    (package_dir / "Cargo.toml").write_bytes(cargo_toml)
    (package_dir / ".cargo-checksum.json").write_text(json.dumps({
        "files": {"Cargo.toml": _sha(cargo_toml)}, "package": package_checksum,
    }, sort_keys=True))

    lock = {
        "schema_version": 1,
        "expected": {"packages": 1, "groups": 1, "source_inputs": 1, "recipe_files": 1},
        "package_inputs": {"base_archive_sha256": "c" * 64, "package_lock_sha256": _sha(b"{}\n")},
        "collected_from": {"source_sha": "e" * 40, "rootfs_sha256": _sha(b"historical rootfs"),
                             "runtime_components_sha256": _sha(components),
                             "runtime_installed_db_sha256": _sha(installed_db)},
        "notice_evidence": {"candidate_count": 1, "legal_clearance": False},
        "source_inputs": [{"origin": "demo", "commit": "b" * 40, "filename": "demo-1.0.tar.gz",
                           "url": "https://example.invalid/demo-1.0.tar.gz", "size": len(source),
                           "sha256": _sha(source), "declared_sha512": hashlib.sha512(source).hexdigest(),
                           "cache_path": "sources/demo/demo-1.0.tar.gz"}],
        "recipe_files": [{"origin": "demo", "commit": "b" * 40, "repository": "main", "path": "fix.patch",
                          "url": "https://example.invalid/fix.patch", "size": len(recipe),
                          "sha256": _sha(recipe), "cache_path": "recipes/demo/fix.patch"}],
        "notice_candidates": [{"candidate_id": "candidate-000001", "origin": "demo", "commit": "b" * 40,
                                "source_filename": "demo-1.0.tar.gz", "size": len(notice),
                                "sha256": _sha(notice), "cache_path": "notices/candidate-000001.bin"}],
        "notice_empty_groups": [], "supplements": [],
        "groups": [{"origin": "demo", "commit": "b" * 40, "packages": [package],
                    "declared_package_licenses": ["MIT"]}],
    }
    lock_path = root / "sources.lock.json"
    lock_path.write_text(json.dumps(lock))
    return repo, assets, cache, vendor, lock_path, inventory, source_sha


class RuntimeSourcesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_download_length_failure_identifies_input_without_url(self):
        record = {"filename": "sqlite-3490200-LICENSE.md", "size": 3864,
                  "url": "https://example.invalid/raw?secret=never-log-this"}
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://example.invalid/html"
        response.headers = {"Content-Length": "49133"}
        with mock.patch("scripts.runtime_sources.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError,
                    r"sqlite-3490200-LICENSE.md.*expected 3864.*received 49133") as caught:
                _download(record, remaining=5000, deadline=time.monotonic() + 10)
        self.assertNotIn("secret", str(caught.exception))
        response.read.assert_not_called()

    def test_download_http_failure_identifies_input_without_response_or_url(self):
        record = {"filename": "protocols-6.4", "size": 3144,
                  "url": "https://example.invalid/raw?token=never-log-this"}
        body = io.BytesIO(b"private response must not be reported")
        error = urllib.error.HTTPError(record["url"], 418, "private response reason", {}, body)
        with mock.patch("scripts.runtime_sources.urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(ValueError, r"HTTP 418.*protocols-6.4") as caught:
                _download(record, remaining=5000, deadline=time.monotonic() + 10)
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("token", str(caught.exception))
        self.assertTrue(body.closed)

    def test_download_still_requires_both_locked_digests(self):
        payload = b"pinned upstream bytes"
        record = {"filename": "source.txt", "size": len(payload),
                  "url": "https://example.invalid/source.txt", "sha256": _sha(payload),
                  "declared_sha512": hashlib.sha512(payload).hexdigest()}
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.geturl.return_value = record["url"]
                response.headers = {"Content-Length": str(len(payload))}
                response.read.side_effect = [payload, b""]
                current = dict(record, declared_sha512="0" * 128) if corrupt else record
                with mock.patch("scripts.runtime_sources.urllib.request.build_opener") as opener:
                    opener.return_value.open.return_value = response
                    if corrupt:
                        with self.assertRaisesRegex(ValueError, "SHA-512 mismatch"):
                            _download(current, remaining=5000, deadline=time.monotonic() + 10)
                    else:
                        self.assertEqual(_download(current, remaining=5000,
                                         deadline=time.monotonic() + 10), payload)

    def test_checked_lock_retains_expected_actual_guest_evidence(self):
        lock = json.loads(Path("runtime/microvm/sources.lock.json").read_text())
        self.assertEqual(lock["expected"], {"packages": 142, "groups": 47,
                                            "source_inputs": 61, "recipe_files": 308})
        self.assertEqual(len(lock["notice_candidates"]), 1119)
        self.assertEqual(len(lock["notice_empty_groups"]), 4)
        ncurses = next(record for record in lock["source_inputs"] if record["origin"] == "ncurses")
        self.assertEqual(ncurses["initial_url_status"], 404)
        self.assertEqual(ncurses["url"], "https://distfiles.alpinelinux.org/distfiles/v3.22/ncurses-6.5-20250503.tgz")
        sqlite_license = next(record for record in lock["source_inputs"]
                              if record["filename"] == "sqlite-3490200-LICENSE.md")
        self.assertEqual(sqlite_license["url"],
                         "https://distfiles.alpinelinux.org/distfiles/v3.22/sqlite-3490200-LICENSE.md")
        self.assertEqual(sqlite_license["initial_url"], "https://www.sqlite.org/src/raw")
        self.assertEqual(sqlite_license["size"], 3864)
        self.assertEqual(sqlite_license["sha256"],
                         "a7a58d6022c090c528e3167026167b3141d682efc6a3c188b473ba59f1788fc7")
        self.assertEqual(sqlite_license["declared_sha512"],
                         "38844727b09d11ded660c069d19a5289100405b0b243e75338060d237e3190fd99"
                         "f0270611d214862beeb54a67993c2a337f917fdc452084f812d1371ce9869b")

        def assert_portable(value):
            if isinstance(value, dict):
                for item in value.values():
                    assert_portable(item)
            elif isinstance(value, list):
                for item in value:
                    assert_portable(item)
            elif isinstance(value, str):
                self.assertFalse(value.startswith("/"))
                self.assertNotIn("/private/tmp/", value)
                self.assertNotIn("/var/folders/", value)
        assert_portable(lock)

    def test_pinned_sources_use_verified_official_distfiles(self):
        lock = json.loads(Path("runtime/microvm/sources.lock.json").read_text())
        for record in lock["source_inputs"]:
            with self.subTest(filename=record["filename"]):
                self.assertEqual(record["url"], "https://distfiles.alpinelinux.org/distfiles/v3.22/"
                                 + record["filename"])
                self.assertIn("initial_url", record)

    def test_source_sidecar_is_deterministic_and_includes_bound_material(self):
        repo, assets, cache, vendor, lock, _, source_sha = _fixture(self.root)
        first, second = self.root / "first.tar.xz", self.root / "second.tar.xz"
        build_source_sidecar(assets, repo, first, "0.3.1", source_sha, cache, vendor, lock)
        build_source_sidecar(assets, repo, second, "0.3.1", source_sha, cache, vendor, lock)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first, "r:xz") as archive:
            names = archive.getnames()
            for name in ("source-inputs/demo/demo-1.0.tar.gz", "recipes/demo/main/fix.patch",
                         "notice-evidence/demo/candidate-000001.bin", "project/LICENSE",
                         "project/Cargo.lock",
                         "rust-vendor/Cargo.lock", "rust-vendor/serde-1.0.0/Cargo.toml",
                         "guest/runtime-components.json"):
                self.assertIn(name, names)
            index = json.load(archive.extractfile("MANIFEST.json"))
            self.assertEqual(index["source_sha"], source_sha)
            self.assertEqual(len(index["members"]), len(names) - 1)

    def test_empty_notice_member_is_retained_with_exact_digest(self):
        for case in ("valid", "wrong_digest", "negative_size"):
            with self.subTest(case=case):
                root = self.root / case
                root.mkdir()
                repo, assets, cache, vendor, lock, _, source_sha = _fixture(root)
                archive_bytes = io.BytesIO()
                with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
                    member = tarfile.TarInfo("demo/MODULE_LICENSE_PUBLIC_DOMAIN")
                    member.size = 0
                    archive.addfile(member, io.BytesIO(b""))
                payload = archive_bytes.getvalue()
                data = json.loads(lock.read_text())
                source = data["source_inputs"][0]
                source.update(size=len(payload), sha256=_sha(payload),
                              declared_sha512=hashlib.sha512(payload).hexdigest())
                (cache / source["cache_path"]).write_bytes(payload)
                candidate = data["notice_candidates"][0]
                candidate.pop("cache_path")
                candidate.update(member_path=member.name, size=-1 if case == "negative_size" else 0,
                                 sha256="0" * 64 if case == "wrong_digest" else _sha(b""))
                lock.write_text(json.dumps(data))
                output = root / "out.tar.xz"
                if case != "valid":
                    with self.assertRaisesRegex(ValueError, "notice candidate"):
                        build_source_sidecar(assets, repo, output, "0.3.1", source_sha,
                                             cache, vendor, lock)
                    self.assertFalse(output.exists())
                else:
                    build_source_sidecar(assets, repo, output, "0.3.1", source_sha,
                                         cache, vendor, lock)
                    with tarfile.open(output, "r:xz") as archive:
                        retained = archive.getmember("notice-evidence/demo/candidate-000001.bin")
                        self.assertEqual(retained.size, 0)
                        self.assertEqual(archive.extractfile(retained).read(), b"")

    def test_inventory_group_and_commit_mismatch_is_rejected(self):
        inventory = _fixture(self.root)[-2]
        inventory["packages"][0]["aports_commit"] = "e" * 40
        with self.assertRaisesRegex(ValueError, "source_groups|origin/commit groups"):
            validate_inventory_binding(inventory, {"groups": [{"origin": "demo", "commit": "b" * 40,
                                                                 "packages": inventory["source_groups"][0]["packages"],
                                                                 "declared_package_licenses": ["MIT"]}]})

    def test_missing_corrupt_and_traversal_inputs_are_rejected(self):
        for corruption in ("missing", "changed", "traversal"):
            with self.subTest(corruption=corruption):
                case = self.root / corruption
                case.mkdir()
                repo, assets, cache, vendor, lock, _, source_sha = _fixture(case)
                source = cache / "sources" / "demo" / "demo-1.0.tar.gz"
                if corruption == "missing":
                    source.unlink()
                elif corruption == "changed":
                    source.write_bytes(b"tampered")
                else:
                    data = json.loads(lock.read_text())
                    data["source_inputs"][0]["filename"] = "../escape.tar.gz"
                    lock.write_text(json.dumps(data))
                if corruption == "missing":
                    with mock.patch("scripts.runtime_sources._download", side_effect=urllib.error.URLError("offline")):
                        with self.assertRaises(urllib.error.URLError):
                            build_source_sidecar(assets, repo, case / "out.tar.xz", "0.3.1", source_sha,
                                                 cache, vendor, lock)
                else:
                    with self.assertRaises(ValueError):
                        build_source_sidecar(assets, repo, case / "out.tar.xz", "0.3.1", source_sha,
                                             cache, vendor, lock)

    def test_guest_digest_and_source_lock_binding_mismatches_are_rejected(self):
        repo, assets, cache, vendor, lock, _, source_sha = _fixture(self.root)
        (assets / "rootfs.ext4").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "rootfs digest"):
            build_source_sidecar(assets, repo, self.root / "digest.tar.xz", "0.3.1", source_sha,
                                 cache, vendor, lock)

        case = self.root / "binding"
        case.mkdir()
        repo, assets, cache, vendor, lock, _, source_sha = _fixture(case)
        data = json.loads(lock.read_text())
        data["package_inputs"]["package_lock_sha256"] = "e" * 64
        lock.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "package input binding"):
            build_source_sidecar(assets, repo, case / "binding.tar.xz", "0.3.1", source_sha,
                                 cache, vendor, lock)

    def test_unrelated_source_group_is_rejected_even_with_unchanged_counts(self):
        repo, assets, cache, vendor, lock, _, source_sha = _fixture(self.root)
        data = json.loads(lock.read_text())
        data['source_inputs'][0]['origin'] = 'unrelated'
        lock.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'source input.*group'):
            build_source_sidecar(assets, repo, self.root / 'out.tar.xz', '0.3.1', source_sha, cache, vendor, lock)

    def test_cached_input_budget_is_checked_before_reading(self):
        import scripts.runtime_sources as module
        record = {'size': 10, 'cache_path': 'source.bin'}
        (self.root / 'source.bin').write_bytes(b'x' * 10)
        with mock.patch.object(module, '_regular_bytes', side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(ValueError, 'aggregate|budget'):
                module._cached_or_download(self.root, record, remaining=1, deadline=0)

    def test_source_sidecar_refuses_to_replace_caller_output(self):
        repo, assets, cache, vendor, lock, _, source_sha = _fixture(self.root)
        output = self.root / 'caller-output.tar.xz'
        output.write_bytes(b'caller bytes')
        with self.assertRaises((ValueError, FileExistsError)):
            build_source_sidecar(assets, repo, output, '0.3.1', source_sha, cache, vendor, lock)
        self.assertEqual(output.read_bytes(), b'caller bytes')

    def test_vendor_hash_or_lock_checksum_mismatch_is_rejected(self):
        repo, _, _, vendor, _, _, _ = _fixture(self.root)
        (vendor / "serde-1.0.0" / "Cargo.toml").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "Cargo vendor file hash mismatch"):
            validate_rust_vendor(vendor, repo / "Cargo.lock")

    def test_zip_archive_notice_member_is_resolved_without_extraction(self):
        notice = b"license text"
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("demo/COPYING", notice)
        source = {"origin": "demo", "commit": "b" * 40, "filename": "demo.zip"}
        candidate = {"candidate_id": "candidate-000009", "origin": "demo", "commit": "b" * 40,
                     "source_filename": "demo.zip", "member_path": "demo/COPYING",
                     "size": len(notice), "sha256": _sha(notice)}
        with tempfile.TemporaryDirectory() as cache:
            result = _collect_notice_data([candidate], Path(cache), [source],
                                          {_source_member_name(source): zipped.getvalue()}, [], {})
        self.assertEqual(result[candidate["candidate_id"]], notice)


if __name__ == "__main__":
    unittest.main()
