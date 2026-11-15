import gzip
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import guest_packages


def apk_bytes(name="sample", version="1.0-r0", architecture="aarch64"):
    control = (
        f"pkgname = {name}\n"
        f"pkgver = {version}\n"
        f"arch = {architecture}\n"
    ).encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo(".PKGINFO")
        info.size = len(control)
        archive.addfile(info, io.BytesIO(control))
    return gzip.compress(raw.getvalue(), mtime=0)


class GuestPackageLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.content = apk_bytes()
        self.filename = "sample-1.0-r0.apk"
        self.url = f"https://dl-cdn.alpinelinux.org/alpine/v3.22/main/aarch64/{self.filename}"
        self.lock = {
            "schema_version": 1,
            "architecture": "aarch64",
            "base": {
                "url": "https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/aarch64/alpine-minirootfs-3.22.8-aarch64.tar.gz",
                "sha256": "36091efacd083c1cf5a8c837272530bff4fc72db1fbfdef559f52de0d890bca4",
            },
            "source": "Alpine 3.22; APKINDEX signatures authenticated by native apk",
            "indexes": [{"repository": "https://dl-cdn.alpinelinux.org/alpine/v3.22/main", "filename": "APKINDEX.abc.tar.gz", "sha256": "a" * 64}],
            "packages": [{
                "name": "sample",
                "version": "1.0-r0",
                "architecture": "aarch64",
                "url": self.url,
                "filename": self.filename,
                "size": len(self.content),
                "sha256": hashlib.sha256(self.content).hexdigest(),
            }],
        }
        self.lock_path = self.root / "packages.lock.json"
        self.write_lock()

    def tearDown(self):
        self.temp.cleanup()

    def write_lock(self):
        self.lock_path.write_text(json.dumps(self.lock))

    def test_reads_control_identity_after_signed_apk_gzip_member(self):
        signed_header = io.BytesIO()
        with tarfile.open(fileobj=signed_header, mode='w') as archive:
            info = tarfile.TarInfo('.SIGN.RSA.test.rsa.pub')
            info.size = 9
            archive.addfile(info, io.BytesIO(b'signature'))
        package = self.root / 'signed.apk'
        package.write_bytes(gzip.compress(signed_header.getvalue()) + apk_bytes())
        self.assertEqual(guest_packages._pkginfo(package),
                         {'pkgname':'sample', 'pkgver':'1.0-r0', 'arch':'aarch64'})

    def test_fetch_downloads_exact_locked_bytes_and_verify_accepts_them(self):
        output = self.root / "downloaded"
        response = io.BytesIO(self.content)
        response.geturl = lambda: self.url
        with patch.object(guest_packages, "urlopen", return_value=response):
            guest_packages.fetch(self.lock_path, output)
        self.assertEqual((output / self.filename).read_bytes(), self.content)
        guest_packages.verify(self.lock_path, output)

    def test_fetch_rejects_bytes_that_do_not_match_lock(self):
        output = self.root / "downloaded"
        response = io.BytesIO(b"tampered")
        response.geturl = lambda: self.url
        with patch.object(guest_packages, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "SHA-256|size"):
                guest_packages.fetch(self.lock_path, output)
        self.assertFalse((output / self.filename).exists())

    def test_verify_rejects_extra_apks_and_symlinked_packages(self):
        output = self.root / "downloaded"
        output.mkdir()
        (output / self.filename).write_bytes(self.content)
        (output / "extra.apk").write_bytes(b"extra")
        with self.assertRaisesRegex(ValueError, "extra"):
            guest_packages.verify(self.lock_path, output)
        (output / "extra.apk").unlink()
        (output / self.filename).unlink()
        (output / self.filename).symlink_to(self.lock_path)
        with self.assertRaisesRegex(ValueError, "symlink|regular"):
            guest_packages.verify(self.lock_path, output)

    def test_verify_rejects_missing_and_tampered_packages(self):
        output = self.root / "downloaded"
        output.mkdir()
        with self.assertRaisesRegex(ValueError, "missing"):
            guest_packages.verify(self.lock_path, output)
        (output / self.filename).write_bytes(self.content + b"changed")
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            guest_packages.verify(self.lock_path, output)

    def test_lock_rejects_untrusted_download_url_and_wrong_architecture(self):
        self.lock["packages"][0]["url"] = "http://example.invalid/sample.apk"
        self.write_lock()
        with self.assertRaisesRegex(ValueError, "URL"):
            guest_packages.load_lock(self.lock_path)
        self.lock["packages"][0]["url"] = self.url
        self.lock["packages"][0]["architecture"] = "x86_64"
        self.write_lock()
        with self.assertRaisesRegex(ValueError, "architecture"):
            guest_packages.load_lock(self.lock_path)


if __name__ == "__main__":
    unittest.main()
