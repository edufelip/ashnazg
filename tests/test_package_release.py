import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.package_release import package_release


class PackageReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inputs = {}
        for name, content in {
            "host": b"host executable",
            "agent": b"agent instrumentation apk",
            "fixture": b"fixture apk",
            "license": b"MIT license",
            "setup": b"Install instructions",
        }.items():
            path = self.root / name
            path.write_bytes(content)
            self.inputs[name] = path

    def tearDown(self):
        self.temp.cleanup()

    def build(self, **overrides):
        values = {**self.inputs, **overrides}
        return package_release(
            version="0.1.0",
            source_sha="a" * 40,
            platform="linux-x86_64",
            output=self.root / "release.zip",
            host_binary=values["host"],
            agent_apk=values["agent"],
            fixture_apk=values["fixture"],
            license_file=values["license"],
            setup_file=values["setup"],
        )

    def test_missing_input_is_rejected_before_archive_creation(self):
        with self.assertRaisesRegex(FileNotFoundError, "agent"):
            self.build(agent=self.root / "missing.apk")
        self.assertFalse((self.root / "release.zip").exists())

    def test_archive_has_safe_paths_only_required_files_and_provenance_checksums(self):
        archive_path = self.build()
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            self.assertEqual(names, sorted(names))
            self.assertEqual(
                set(names),
                {
                    "LICENSE",
                    "README-SETUP.md",
                    "bin/ashnazg",
                    "android/agent-instrumentation.apk",
                    "android/fixture-instrumentation.apk",
                    "manifest.json",
                    "SHA256SUMS",
                },
            )
            self.assertFalse(any(name.startswith("/") or ".." in Path(name).parts for name in names))
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["version"], "0.1.0")
            self.assertEqual(manifest["source_sha"], "a" * 40)
            self.assertEqual(manifest["platform"], "linux-x86_64")
            payloads = manifest["files"]
            self.assertEqual(set(payloads), set(names) - {"manifest.json", "SHA256SUMS"})
            for name, metadata in payloads.items():
                self.assertEqual(metadata["sha256"], hashlib.sha256(archive.read(name)).hexdigest())
            checksums = archive.read("SHA256SUMS").decode()
            for name in payloads:
                self.assertIn(f"  {name}\n", checksums)
            checksum_entries = {
                name: digest
                for digest, name in (line.split("  ", 1) for line in checksums.splitlines())
            }
            self.assertEqual(set(checksum_entries), set(names) - {"SHA256SUMS"})
            for name, digest in checksum_entries.items():
                self.assertEqual(digest, hashlib.sha256(archive.read(name)).hexdigest())
            self.assertNotIn("fixture-debug.apk", names)

    def test_same_inputs_produce_byte_identical_archives(self):
        first = self.build().read_bytes()
        second = self.build().read_bytes()
        self.assertEqual(first, second)

    def test_rejects_version_or_source_metadata_that_could_not_be_trusted(self):
        with self.assertRaisesRegex(ValueError, "version"):
            package_release(version="0.2.0", source_sha="a" * 40, platform="linux-x86_64",
                            output=self.root / "other.zip", host_binary=self.inputs["host"],
                            agent_apk=self.inputs["agent"], fixture_apk=self.inputs["fixture"],
                            license_file=self.inputs["license"], setup_file=self.inputs["setup"])
        with self.assertRaisesRegex(ValueError, "source"):
            package_release(version="0.1.0", source_sha="not-a-sha", platform="linux-x86_64",
                            output=self.root / "other.zip", host_binary=self.inputs["host"],
                            agent_apk=self.inputs["agent"], fixture_apk=self.inputs["fixture"],
                            license_file=self.inputs["license"], setup_file=self.inputs["setup"])


if __name__ == "__main__":
    unittest.main()
