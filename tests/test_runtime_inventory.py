import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts import runtime_inventory


def record(name, version="1-r0", arch="aarch64", *, origin="main/foo", commit="a" * 40,
           license="MIT", url="https://example.invalid/foo", build_time="1700000000", files=()):
    lines = [f"P:{name}", f"V:{version}", f"A:{arch}"]
    if license is not None:
        lines.append(f"L:{license}")
    if origin is not None:
        lines.append(f"o:{origin}")
    if url is not None:
        lines.append(f"U:{url}")
    if build_time is not None:
        lines.append(f"t:{build_time}")
    if commit is not None:
        lines.append(f"c:{commit}")
    return "\n".join(lines + [*(f"F:{item}" for item in files)])


def db(*records):
    return ("\n\n".join(records) + "\n").encode()


def base_archive(database, *, member="lib/apk/db/installed", duplicate=False, kind="file"):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(database)
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "../../elsewhere"
        archive.addfile(info, io.BytesIO(database) if kind == "file" else None)
        if duplicate:
            info = tarfile.TarInfo(member)
            info.size = len(database)
            archive.addfile(info, io.BytesIO(database))
    return gzip.compress(raw.getvalue(), mtime=0)


def lock_for(packages=()):
    return {
        "schema_version": 1,
        "architecture": "aarch64",
        "base": {
            "url": "https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/aarch64/alpine-minirootfs-3.22.8-aarch64.tar.gz",
            "sha256": "0" * 64,
        },
        "source": "test authenticated source",
        "indexes": [{"repository": "https://dl-cdn.alpinelinux.org/alpine/v3.22/main",
                     "filename": "APKINDEX.test", "sha256": "1" * 64}],
        "packages": list(packages),
    }


def locked(name, version="2-r0", arch="aarch64"):
    filename = f"{name}-{version}.apk"
    return {"name": name, "version": version, "architecture": arch,
            "url": f"https://dl-cdn.alpinelinux.org/alpine/v3.22/main/aarch64/{filename}",
            "filename": filename, "size": 123, "sha256": "2" * 64}


class RuntimeInventoryTests(unittest.TestCase):
    def build(self, final, base_db, packages=(), *, lock=None, archive=None):
        archive = archive if archive is not None else base_archive(base_db)
        value = lock if lock is not None else lock_for(packages)
        if lock is None:
            value["base"]["sha256"] = hashlib.sha256(archive).hexdigest()
        return runtime_inventory.build_inventory(
            installed_db=final, base_archive=archive,
            package_lock=json.dumps(value).encode(), rootfs_sha256="3" * 64,
            source_sha="4" * 40)

    def test_unions_verified_base_and_locked_packages_deterministically(self):
        base_db = db(record("base-only"), record("upgrade", "1-r0"))
        final = db(record("locked", "2-r0"), record("upgrade", "2-r0"), record("base-only"))
        output = locked("locked")
        result = self.build(final, base_db, [output, locked("upgrade", "2-r0")])
        self.assertEqual(result["metadata_status"], "complete")
        self.assertEqual([p["name"] for p in result["packages"]], ["base-only", "locked", "upgrade"])
        by_name = {p["name"]: p for p in result["packages"]}
        self.assertEqual(by_name["locked"]["provenance"]["kind"], "locked_apk")
        self.assertEqual(by_name["base-only"]["provenance"], {
            "kind": "base", "base_archive_sha256": hashlib.sha256(base_archive(base_db)).hexdigest()})
        self.assertEqual(by_name["upgrade"]["provenance"]["kind"], "locked_apk")
        self.assertEqual(result, self.build(final, base_db, [output, locked("upgrade", "2-r0")]))

    def test_rejects_base_hash_member_type_duplicates_and_database_size(self):
        database = db(record("base"))
        archive = base_archive(database)
        lock = lock_for([locked("notfinal")])
        lock["base"]["sha256"] = "f" * 64
        result = self.build(database, database, lock=lock, archive=archive)
        self.assertEqual(result["metadata_status"], "incomplete")
        self.assertTrue(any("base archive SHA-256" in issue["message"] for issue in result["issues"]))
        self.assertEqual(self.build(database, database, archive=base_archive(database, member="etc/installed"))["metadata_status"], "incomplete")
        self.assertEqual(self.build(database, database, archive=base_archive(database, kind="symlink"))["metadata_status"], "incomplete")
        self.assertEqual(self.build(database, database, archive=base_archive(database, duplicate=True))["metadata_status"], "incomplete")
        self.assertEqual(self.build(database, database, archive=gzip.compress(b"not a tar archive", mtime=0))["metadata_status"], "incomplete")
        self.assertEqual(self.build(database, database, archive=base_archive(b"x" * (2 * 1024 * 1024 + 1)))["metadata_status"], "incomplete")

    def test_bounds_compressed_archive_decompression_and_member_count(self):
        final, base = db(record("x")), db(record("x"))
        oversized_compressed = b"x" * (20 * 1024 * 1024 + 1)
        self.assertEqual(self.build(final, base, archive=oversized_compressed)["metadata_status"], "incomplete")
        decompression_bomb = gzip.compress(b"\0" * (128 * 1024 * 1024 + 1), mtime=0)
        self.assertEqual(self.build(final, base, archive=decompression_bomb)["metadata_status"], "incomplete")
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as archive:
            for index in range(10001):
                info = tarfile.TarInfo(f"ignored/{index}")
                archive.addfile(info)
        too_many = gzip.compress(raw.getvalue(), mtime=0)
        self.assertEqual(self.build(final, base, archive=too_many)["metadata_status"], "incomplete")

    def test_marks_missing_changed_and_unmapped_packages(self):
        base_db = db(record("base", "1-r0"), record("locked", "wrong-r0"))
        final = db(record("base", "1-r0"), record("new", "1-r0"), record("locked", "wrong-r0"))
        result = self.build(final, base_db, [locked("locked", "2-r0"), locked("absent")])
        self.assertEqual(result["metadata_status"], "incomplete")
        by_name = {p["name"]: p for p in result["packages"]}
        self.assertEqual(by_name["new"]["provenance"]["kind"], "unmapped")
        self.assertEqual(by_name["locked"]["provenance"]["kind"], "unmapped")
        self.assertTrue(any("absent" in issue["message"] for issue in result["issues"]))

    def test_rejects_malformed_and_duplicate_identity_records(self):
        base = db(record("x"))
        for final in (b"P:x\nV:1\n", db("P:x\nV:1\nA:aarch64\nP:y"),
                      db(record("x"), record("x")), b"not a package record\n"):
            with self.subTest(final=final):
                self.assertEqual(self.build(final, base)["metadata_status"], "incomplete")

    def test_file_list_is_not_source_metadata_and_missing_fields_are_explicit(self):
        incomplete = record("partial", origin=None, commit=None, license=None, url=None, build_time=None,
                            files=("fake/main/recipe-version", "usr/share/doc/NOTICE"))
        result = self.build(db(incomplete), db(record("partial")))
        package = result["packages"][0]
        self.assertEqual(package["missing_metadata"], sorted(["declared_license", "origin", "upstream_url", "build_time_unix", "aports_commit"]))
        self.assertEqual(package["raw_header_fields"].keys(), {"P", "V", "A"})
        self.assertEqual(result["metadata_status"], "incomplete")

    def test_invalid_source_values_are_not_grouped_or_inferred(self):
        result = self.build(db(record("bad", origin="", commit="x" * 40, build_time="9" * 3000)), db(record("bad")))
        package = result["packages"][0]
        self.assertIsNone(package["origin"])
        self.assertEqual(package["raw_header_fields"]["o"], "")
        self.assertIsNone(package["aports_commit"])
        self.assertTrue(package["missing_metadata"])
        self.assertEqual(result["source_groups"], [])

    def test_groups_only_exact_origin_commit_and_sorts_binary_members(self):
        origin, commit = "main/group", "b" * 40
        final = db(record("z", origin=origin, commit=commit), record("a", origin=origin, commit=commit),
                   record("other", origin=origin, commit="c" * 40))
        result = self.build(final, db(final.decode().strip()))
        self.assertEqual(len(result["source_groups"]), 2)
        group = next(g for g in result["source_groups"] if g["aports_commit"] == commit)
        self.assertEqual([p["name"] for p in group["packages"]], ["a", "z"])

    def test_lock_validation_is_strict_about_boolean_schema_identity_and_hashes(self):
        cases = []
        bad = lock_for([locked("a")]); bad["schema_version"] = True; cases.append(bad)
        bad = lock_for([locked("a")]); bad["packages"][0]["size"] = True; cases.append(bad)
        bad = lock_for([locked("a")]); bad["packages"][0]["sha256"] = "G" * 64; cases.append(bad)
        bad = lock_for([locked("a", arch="x86_64")]); cases.append(bad)
        bad = lock_for([locked("a"), locked("a")]); cases.append(bad)
        for lock in cases:
            with self.subTest(lock=lock):
                self.assertEqual(self.build(db(record("a", "2-r0")), db(record("a")), lock=lock)["metadata_status"], "incomplete")

    def test_cli_emits_json_and_returns_nonzero_for_incomplete_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final, base = db(record("missing")), db(record("base"))
            archive = base_archive(base)
            lock = lock_for([locked("absent")])
            lock["base"]["sha256"] = hashlib.sha256(archive).hexdigest()
            paths = [root / name for name in ("installed", "base.tar.gz", "lock.json")]
            paths[0].write_bytes(final); paths[1].write_bytes(archive); paths[2].write_text(json.dumps(lock))
            proc = subprocess.run([sys.executable, "scripts/runtime_inventory.py",
                                   "--installed-db", str(paths[0]), "--base-archive", str(paths[1]),
                                   "--package-lock", str(paths[2]), "--rootfs-sha256", "3" * 64,
                                   "--source-sha", "4" * 40], capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["metadata_status"], "incomplete")
            self.assertEqual(proc.stderr, "")

            final = db(record("sample", "2-r0"))
            base_db = db(record("base"))
            archive = base_archive(base_db)
            lock = lock_for([locked("sample", "2-r0")])
            lock["base"]["sha256"] = hashlib.sha256(archive).hexdigest()
            paths[0].write_bytes(final); paths[1].write_bytes(archive); paths[2].write_text(json.dumps(lock))
            proc = subprocess.run([sys.executable, "scripts/runtime_inventory.py",
                                   "--installed-db", str(paths[0]), "--base-archive", str(paths[1]),
                                   "--package-lock", str(paths[2]), "--rootfs-sha256", "3" * 64,
                                   "--source-sha", "4" * 40], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(json.loads(proc.stdout)["metadata_status"], "complete")
            self.assertEqual(proc.stderr, "")


if __name__ == "__main__":
    unittest.main()
