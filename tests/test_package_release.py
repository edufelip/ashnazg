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
            "helper": b"signed vm helper executable",
            "agent": b"agent instrumentation apk",
            "fixture": b"fixture apk",
            "license": b"MIT license",
            "setup": b"Install instructions",
            "notices": b"Dependency original notice texts",
        }.items():
            path = self.root / name
            path.write_bytes(content)
            if name in {"host", "helper"}:
                path.chmod(0o755)
            self.inputs[name] = path

    def tearDown(self):
        self.temp.cleanup()

    def build(self, **overrides):
        values = {**self.inputs, **overrides}
        platform = values.get("platform", "linux-x86_64")
        helper = overrides.get("helper", self.inputs["helper"] if platform == "macos-aarch64" else None)
        return package_release(
            version="0.3.0",
            source_sha="a" * 40,
            platform=platform,
            output=self.root / "release.zip",
            host_binary=values["host"],
            vm_helper=helper,
            agent_apk=values["agent"],
            fixture_apk=values["fixture"],
            license_file=values["license"],
            setup_file=values["setup"],
            third_party_notices=values["notices"],
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
                    "THIRD-PARTY-NOTICES.tar.xz",
                    "bin/ashnazg",
                    "android/agent-instrumentation.apk",
                    "android/fixture-instrumentation.apk",
                    "manifest.json",
                    "SHA256SUMS",
                },
            )
            self.assertFalse(any(name.startswith("/") or ".." in Path(name).parts for name in names))
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["version"], "0.3.0")
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
            package_release(version="0.1.0", source_sha="a" * 40, platform="linux-x86_64",
                            output=self.root / "other.zip", host_binary=self.inputs["host"],
                            agent_apk=self.inputs["agent"], fixture_apk=self.inputs["fixture"],
                            license_file=self.inputs["license"], setup_file=self.inputs["setup"], third_party_notices=self.inputs["notices"])
        with self.assertRaisesRegex(ValueError, "source"):
            package_release(version="0.3.0", source_sha="not-a-sha", platform="linux-x86_64",
                            output=self.root / "other.zip", host_binary=self.inputs["host"],
                            vm_helper=None,
                            agent_apk=self.inputs["agent"], fixture_apk=self.inputs["fixture"],
                            license_file=self.inputs["license"], setup_file=self.inputs["setup"], third_party_notices=self.inputs["notices"])

    def test_linux_package_rejects_vm_helper(self):
        for platform in ("linux-x86_64", "linux-aarch64"):
            with self.subTest(platform=platform), self.assertRaisesRegex(ValueError, "Linux.*helper"):
                self.build(platform=platform, helper=self.inputs["helper"])

    def test_rejects_unsupported_platform_roles(self):
        with self.assertRaisesRegex(ValueError, "platform"):
            self.build(platform="freebsd-aarch64")
        with self.assertRaisesRegex(ValueError, "macos-aarch64"):
            self.build(platform="macos-x86_64", helper=self.inputs["helper"])

    def test_macos_arm_package_requires_and_includes_executable_helper(self):
        with self.assertRaisesRegex(ValueError, "require.*helper"):
            self.build(platform="macos-aarch64", helper=None)

        archive_path = self.build(platform="macos-aarch64")
        first_bytes = archive_path.read_bytes()
        self.assertEqual(self.build(platform="macos-aarch64").read_bytes(), first_bytes)
        with zipfile.ZipFile(archive_path) as archive:
            self.assertIn("bin/ashnazg-vm", archive.namelist())
            self.assertEqual(archive.read("bin/ashnazg-vm"), self.inputs["helper"].read_bytes())
            helper_info = archive.getinfo("bin/ashnazg-vm")
            self.assertEqual((helper_info.external_attr >> 16) & 0o777, 0o755)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertIn("bin/ashnazg-vm", manifest["files"])
            self.assertEqual(
                manifest["files"]["bin/ashnazg-vm"]["sha256"],
                hashlib.sha256(self.inputs["helper"].read_bytes()).hexdigest(),
            )
            helper_digest = hashlib.sha256(archive.read("bin/ashnazg-vm")).hexdigest()
            self.assertIn(f"{helper_digest}  bin/ashnazg-vm\n", archive.read("SHA256SUMS").decode())

    def test_macos_arm_rejects_non_file_or_non_executable_helper(self):
        directory = self.root / "helper-directory"
        directory.mkdir()
        with self.assertRaises(FileNotFoundError):
            self.build(platform="macos-aarch64", helper=directory)

        non_executable = self.root / "not-executable"
        non_executable.write_bytes(b"signed helper")
        non_executable.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "executable"):
            self.build(platform="macos-aarch64", helper=non_executable)

    def test_linux_archive_is_deterministic_and_does_not_contain_helper(self):
        first = self.build().read_bytes()
        second = self.build().read_bytes()
        self.assertEqual(first, second)
        with zipfile.ZipFile(self.root / "release.zip") as archive:
            self.assertNotIn("bin/ashnazg-vm", archive.namelist())


if __name__ == "__main__":
    unittest.main()
