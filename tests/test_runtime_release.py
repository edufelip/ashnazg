import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.runtime_release import prepare_release, verify_release


VERSION = "0.3.0"
SOURCE_SHA = "a" * 40
OTHER_SHA = "b" * 40
PAYLOAD_NAMES = {
    f"ashnazg-v{VERSION}-linux-x86_64.zip",
    f"ashnazg-v{VERSION}-macos-aarch64.zip",
    f"ashnazg-microvm-v{VERSION}-linux-aarch64.tar.xz",
    f"ashnazg-vm-v{VERSION}-macos-aarch64",
    f"ashnazg-agent-v{VERSION}.apk",
    f"ashnazg-fixture-v{VERSION}.apk",
    f"ashnazg-runtime-sources-v{VERSION}-001.tar.xz",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


class RuntimeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._make_payloads(SOURCE_SHA)

    def tearDown(self):
        self.temp.cleanup()

    def _make_payloads(self, source_sha, *, helper=None, zip_helper=None, guest_source_sha=None):
        helper = helper or b"signed helper bytes"
        (self.root / f"ashnazg-vm-v{VERSION}-macos-aarch64").write_bytes(helper)
        (self.root / f"ashnazg-agent-v{VERSION}.apk").write_bytes(b"agent apk")
        (self.root / f"ashnazg-fixture-v{VERSION}.apk").write_bytes(b"fixture apk")
        (self.root / f"ashnazg-runtime-sources-v{VERSION}-001.tar.xz").write_bytes(b"source archive")

        guest_files = {
            "kernel": b"kernel fixture",
            "rootfs.ext4": b"small rootfs fixture",
            "NOTICES.md": b"notices",
            "kernel.config": b"config",
            "kernel.generated.config": b"generated config",
            "manifest.json": canonical({
                "schema_version": 1, "version": VERSION,
                "source_sha": guest_source_sha or source_sha, "architecture": "aarch64",
                "files": {
                    name: {"size": len(data), "sha256": digest(data)}
                    for name, data in (("kernel", b"kernel fixture"), ("rootfs.ext4", b"small rootfs fixture"))
                },
                "guest": {"kernel_version": "6.12.117", "base": "alpine-3.22.8-aarch64",
                          "redroid_index": "sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee"},
            }),
            "provenance.json": b"{}\n",
            "packages.lock.json": b"{}\n",
            "runtime-components.json": b"{}\n",
            "runtime-installed.db": b"P:fixture\nV:1\nA:aarch64\n\n",
            "linux-6.12.117.tar.xz": b"x" * (3 * 1024 * 1024),
        }
        guest_path = self.root / f"ashnazg-microvm-v{VERSION}-linux-aarch64.tar.xz"
        with tarfile.open(guest_path, "w:xz") as archive:
            for name, data in guest_files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, __import__("io").BytesIO(data))

        for platform, name in (("linux-x86_64", f"ashnazg-v{VERSION}-linux-x86_64.zip"),
                               ("macos-aarch64", f"ashnazg-v{VERSION}-macos-aarch64.zip")):
            contents = {
                "bin/ashnazg": b"host binary distinct from helper",
                "android/agent-instrumentation.apk": b"agent apk",
                "android/fixture-instrumentation.apk": b"fixture apk",
                "LICENSE": b"MIT",
                "README-SETUP.md": b"setup",
            }
            if platform == "macos-aarch64":
                contents["bin/ashnazg-vm"] = zip_helper or helper
            manifest = {"name": "ashnazg", "version": VERSION, "source_sha": source_sha,
                        "platform": platform,
                        "files": {path: {"sha256": digest(data), "size": len(data)}
                                  for path, data in sorted(contents.items())}}
            archive_members = {**contents, "manifest.json": canonical(manifest)}
            archive_members["SHA256SUMS"] = "".join(
                f"{digest(data)}  {path}\n" for path, data in sorted(archive_members.items())
            ).encode()
            with zipfile.ZipFile(self.root / name, "w") as archive:
                for path, data in sorted(archive_members.items()):
                    info = zipfile.ZipInfo(path)
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    archive.writestr(info, data)

    def prepare(self, source_sha=SOURCE_SHA):
        return prepare_release(self.root, source_sha=source_sha, version=VERSION)

    def test_prepare_is_deterministic_and_outputs_non_cyclic_candidate_and_checksums(self):
        self.prepare()
        first_candidate = (self.root / "candidate.json").read_bytes()
        first_sums = (self.root / "SHA256SUMS").read_bytes()
        self.assertEqual(verify_release(self.root, source_sha=SOURCE_SHA, version=VERSION), True)
        self.assertEqual(set(path.name for path in self.root.iterdir()), PAYLOAD_NAMES | {
            f"ashnazg-runtime-v{VERSION}.json", "candidate.json", "SHA256SUMS"
        })
        candidate = json.loads(first_candidate)
        self.assertEqual(candidate["source_sha"], SOURCE_SHA)
        self.assertEqual({item["name"] for item in candidate["artifacts"]},
                         PAYLOAD_NAMES | {f"ashnazg-runtime-v{VERSION}.json"})
        self.assertNotIn("candidate.json", [item["name"] for item in candidate["artifacts"]])
        self.assertNotIn("SHA256SUMS", [item["name"] for item in candidate["artifacts"]])
        for name in ("candidate.json", "SHA256SUMS", f"ashnazg-runtime-v{VERSION}.json"):
            (self.root / name).unlink()
        self.prepare()
        self.assertEqual((self.root / "candidate.json").read_bytes(), first_candidate)
        self.assertEqual((self.root / "SHA256SUMS").read_bytes(), first_sums)

    def test_accepts_actual_host_packager_outputs(self):
        from scripts.package_release import package_release
        import tempfile
        with tempfile.TemporaryDirectory() as inputs:
            folder = Path(inputs)
            for name, data in {"host": b"actual host", "license": b"MIT", "setup": b"setup"}.items():
                (folder / name).write_bytes(data)
            helper = self.root / f"ashnazg-vm-v{VERSION}-macos-aarch64"
            helper.chmod(0o755)
            for platform in ("linux-x86_64", "macos-aarch64"):
                package_release(version=VERSION, source_sha=SOURCE_SHA, platform=platform,
                    output=self.root / f"ashnazg-v{VERSION}-{platform}.zip", host_binary=folder / "host",
                    agent_apk=self.root / f"ashnazg-agent-v{VERSION}.apk",
                    fixture_apk=self.root / f"ashnazg-fixture-v{VERSION}.apk",
                    license_file=folder / "license", setup_file=folder / "setup",
                    vm_helper=helper if platform == "macos-aarch64" else None)
            self.prepare()
            self.assertTrue(verify_release(self.root, source_sha=SOURCE_SHA))
            checksums = (self.root / "SHA256SUMS").read_text()
            self.assertIn(f"{digest((self.root / 'candidate.json').read_bytes())}  candidate.json\n", checksums)

    def test_cli_prepare_and_verify(self):
        command = [sys.executable, "scripts/runtime_release.py", "prepare", "--directory", str(self.root),
                   "--source-sha", SOURCE_SHA, "--version", VERSION]
        prepared = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        verified = subprocess.run([sys.executable, "scripts/runtime_release.py", "verify", "--directory",
                                   str(self.root), "--source-sha", SOURCE_SHA], capture_output=True, text=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_rejects_mixed_source_before_writing_release_metadata(self):
        self._make_payloads(OTHER_SHA)
        with self.assertRaisesRegex(ValueError, "source"):
            self.prepare()
        self.assertFalse((self.root / "candidate.json").exists())

    def test_verify_detects_payload_tampering_and_source_mismatch_without_repair(self):
        self.prepare()
        guest = self.root / f"ashnazg-agent-v{VERSION}.apk"
        guest.write_bytes(b"tampered agent")
        with self.assertRaisesRegex(ValueError, "digest|size|mismatch|hash|catalog"):
            verify_release(self.root, source_sha=SOURCE_SHA, version=VERSION)
        with self.assertRaisesRegex(ValueError, "source"):
            verify_release(self.root, source_sha=OTHER_SHA, version=VERSION)
        self.assertEqual(guest.read_bytes(), b"tampered agent")

    def test_rejects_symlink_empty_and_unexpected_files(self):
        name = f"ashnazg-agent-v{VERSION}.apk"
        data = self.root.parent / (self.root.name + "-agent-copy")
        data.write_bytes(b"agent apk")
        (self.root / name).unlink()
        (self.root / name).symlink_to(data)
        with self.assertRaisesRegex(ValueError, "symlink|regular"):
            self.prepare()
        (self.root / name).unlink()
        (self.root / name).write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty|size"):
            self.prepare()
        (self.root / name).write_bytes(b"agent apk")
        (self.root / "surprise.txt").write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "unexpected|exact|files"):
            self.prepare()
        data.unlink()

    def test_rejects_mac_zip_helper_that_differs_from_raw_signed_helper(self):
        self._make_payloads(SOURCE_SHA, zip_helper=b"different helper")
        with self.assertRaisesRegex(ValueError, "helper|match|differ"):
            self.prepare()

    def test_verify_rejects_malformed_candidate_and_checksums_bytes(self):
        self.prepare()
        candidate = self.root / "candidate.json"
        candidate.write_bytes(candidate.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "candidate|canonical|bytes"):
            verify_release(self.root, source_sha=SOURCE_SHA, version=VERSION)

    def test_verify_rejects_added_or_missing_files_without_repair(self):
        self.prepare()
        extra = self.root / "new-payload"
        extra.write_bytes(b"new")
        with self.assertRaisesRegex(ValueError, "allowlist|unexpected"):
            verify_release(self.root, source_sha=SOURCE_SHA, version=VERSION)
        extra.unlink()
        (self.root / f"ashnazg-fixture-v{VERSION}.apk").unlink()
        with self.assertRaisesRegex(ValueError, "allowlist|missing"):
            verify_release(self.root, source_sha=SOURCE_SHA, version=VERSION)

    def test_rejects_guest_embedded_manifest_source_mismatch(self):
        self._make_payloads(SOURCE_SHA, guest_source_sha=OTHER_SHA)
        with self.assertRaisesRegex(ValueError, "guest|source"):
            self.prepare()


if __name__ == "__main__":
    unittest.main()
