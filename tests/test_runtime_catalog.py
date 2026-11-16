import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.runtime_catalog import build_catalog, validate_catalog, write_catalog


SHA = "a" * 40
VERSION = "1.2.3"
ROLES = ("guest", "helper", "agent", "fixture")


class RuntimeCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.inputs = {}
        for role, content in {
            "guest": b"guest archive bytes",
            "helper": b"helper executable bytes",
            "agent": b"agent apk bytes",
            "fixture": b"fixture apk bytes",
        }.items():
            path = self.root / self.basename(role)
            path.write_bytes(content)
            self.inputs[role] = path

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def basename(role, version=VERSION):
        return {
            "guest": f"ashnazg-microvm-v{version}-linux-aarch64.tar.xz",
            "helper": f"ashnazg-vm-v{version}-macos-aarch64",
            "agent": f"ashnazg-agent-v{version}.apk",
            "fixture": f"ashnazg-fixture-v{version}.apk",
        }[role]

    def build(self, **overrides):
        return build_catalog(
            overrides.get("version", VERSION),
            overrides.get("source_sha", SHA),
            overrides.get("artifacts", self.inputs),
            overrides.get("source_archives", ()),
        )

    def test_builds_valid_catalog_with_fixed_role_metadata_and_no_self_hash(self):
        catalog = self.build()
        self.assertEqual(set(catalog), {
            "schema_version", "version", "release_tag", "source_sha", "artifacts", "source_archives"
        })
        self.assertEqual(catalog["schema_version"], 1)
        self.assertEqual(catalog["release_tag"], "v" + VERSION)
        self.assertEqual(catalog["source_sha"], SHA)
        self.assertEqual(set(catalog["artifacts"]), set(ROLES))
        for role, path in self.inputs.items():
            entry = catalog["artifacts"][role]
            self.assertEqual(set(entry), {"name", "platform", "source_sha", "size", "sha256"})
            self.assertEqual(entry["name"], self.basename(role))
            self.assertEqual(entry["source_sha"], SHA)
            self.assertEqual(entry["size"], path.stat().st_size)
            self.assertEqual(entry["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(catalog["source_archives"], [])
        self.assertIs(validate_catalog(catalog), catalog)
        self.assertFalse(any("catalog" in item["name"] for item in catalog["artifacts"].values()))

    def test_write_catalog_produces_deterministic_sorted_json_with_one_newline(self):
        first, second = self.root / "first.json", self.root / "second.json"
        self.assertEqual(write_catalog(version=VERSION, source_sha=SHA, artifacts=self.inputs, output=first), first)
        write_catalog(version=VERSION, source_sha=SHA, artifacts=self.inputs, output=second)
        contents = first.read_bytes()
        self.assertEqual(contents, second.read_bytes())
        self.assertTrue(contents.endswith(b"\n"))
        self.assertFalse(contents.endswith(b"\n\n"))
        self.assertEqual(contents, (json.dumps(json.loads(contents), sort_keys=True, indent=2) + "\n").encode())

    def test_requires_exact_artifact_roles(self):
        with self.assertRaisesRegex(ValueError, "role|artifact"):
            self.build(artifacts={key: value for key, value in self.inputs.items() if key != "fixture"})
        with self.assertRaisesRegex(ValueError, "role|artifact"):
            self.build(artifacts={**self.inputs, "extra": self.inputs["fixture"]})

    def test_rejects_unsafe_version_and_invalid_source_sha(self):
        for version in ("v1.2.3", "1.2", "1.2.3/evil", "1.2.3;echo", "01.2.3", "1.2.3-rc.1"):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "version"):
                self.build(version=version)
        with self.assertRaisesRegex(ValueError, "source"):
            self.build(source_sha="A" * 40)

    def test_rejects_wrong_role_names_and_duplicate_paths(self):
        wrong = dict(self.inputs)
        wrong["agent"] = self.inputs["fixture"]
        with self.assertRaisesRegex(ValueError, "name|agent"):
            self.build(artifacts=wrong)
        duplicate = dict(self.inputs)
        duplicate["fixture"] = self.inputs["agent"]
        with self.assertRaisesRegex(ValueError, "duplicate|unique|name"):
            self.build(artifacts=duplicate)

    def test_validator_rejects_unknown_fields_wrong_types_and_invalid_hashes(self):
        base = self.build()
        variants = []
        bad = json.loads(json.dumps(base)); bad["unexpected"] = True; variants.append(bad)
        bad = json.loads(json.dumps(base)); bad["artifacts"]["guest"]["extra"] = 1; variants.append(bad)
        bad = json.loads(json.dumps(base)); bad["artifacts"]["guest"]["size"] = True; variants.append(bad)
        bad = json.loads(json.dumps(base)); bad["artifacts"]["guest"]["sha256"] = "A" * 64; variants.append(bad)
        bad = json.loads(json.dumps(base)); bad["artifacts"]["guest"]["source_sha"] = "b" * 40; variants.append(bad)
        bad = json.loads(json.dumps(base)); bad["artifacts"]["guest"]["platform"] = "android"; variants.append(bad)
        for catalog in variants:
            with self.subTest(catalog=catalog), self.assertRaises(ValueError):
                validate_catalog(catalog)

    def test_rejects_final_component_symlink_missing_empty_and_nonregular_inputs(self):
        link = self.root / "link.apk"
        link.symlink_to(self.inputs["agent"])
        bad = dict(self.inputs); bad["agent"] = link
        with self.assertRaises((ValueError, OSError)):
            self.build(artifacts=bad)
        bad = dict(self.inputs); bad["agent"] = self.root / "missing.apk"
        with self.assertRaises((ValueError, OSError)):
            self.build(artifacts=bad)
        empty = self.root / self.basename("agent"); empty.write_bytes(b"")
        bad = dict(self.inputs); bad["agent"] = empty
        with self.assertRaisesRegex(ValueError, "empty|size"):
            self.build(artifacts=bad)
        bad = dict(self.inputs); bad["agent"] = self.root
        with self.assertRaises((ValueError, OSError)):
            self.build(artifacts=bad)

    def test_rejects_role_size_caps_without_large_fixture_files(self):
        catalog = self.build()
        catalog["artifacts"]["guest"]["size"] = 1024**3 + 1
        with self.assertRaisesRegex(ValueError, "size|large|limit"):
            validate_catalog(catalog)
        catalog = self.build()
        catalog["artifacts"]["helper"]["size"] = 64 * 1024**2 + 1
        with self.assertRaisesRegex(ValueError, "size|large|limit"):
            validate_catalog(catalog)

    def test_accepts_source_parts_and_rejects_duplicate_or_excessive_indices(self):
        part = self.root / f"ashnazg-runtime-sources-v{VERSION}-002.tar.xz"
        part.write_bytes(b"source part")
        catalog = self.build(source_archives=[part])
        self.assertEqual(catalog["source_archives"][0]["platform"], "source")
        self.assertEqual(catalog["source_archives"][0]["name"], part.name)
        with self.assertRaisesRegex(ValueError, "duplicate|unique|index"):
            self.build(source_archives=[part, part])
        with mock.patch("scripts.runtime_catalog.MAX_SOURCE_ARCHIVES", 1):
            with self.assertRaisesRegex(ValueError, "64|parts|archives|limit"):
                self.build(source_archives=[part, self._source_part(3)])
        with self.assertRaisesRegex(ValueError, "name|source"):
            self.build(source_archives=[self._source_part(1000)])
        oversized = self.build(source_archives=[part])
        oversized["source_archives"][0]["size"] = 1024**3 + 1
        with self.assertRaisesRegex(ValueError, "size|large|limit"):
            validate_catalog(oversized)

    def _source_part(self, index):
        path = self.root / f"ashnazg-runtime-sources-v{VERSION}-{index:03d}.tar.xz"
        path.write_bytes(b"source part")
        return path

    def test_detects_replacement_during_hashing(self):
        original = self.inputs["agent"]
        replacement = self.root / "replacement"
        replacement.write_bytes(b"replacement with a different identity")
        real_stat = os.stat
        calls = 0

        def replacing_stat(path, *args, **kwargs):
            nonlocal calls
            if Path(path) == original:
                calls += 1
                if calls == 2:
                    os.replace(replacement, original)
            return real_stat(path, *args, **kwargs)

        with mock.patch("scripts.runtime_catalog.os.stat", side_effect=replacing_stat):
            with self.assertRaisesRegex((ValueError, OSError), "chang|replac|mutat|input"):
                self.build()

    def test_detects_in_place_mutation_during_hashing(self):
        original = self.inputs["agent"]
        real_fstat = os.fstat
        target_inode = original.stat().st_ino
        calls = 0

        def mutating_fstat(fd):
            nonlocal calls
            info = real_fstat(fd)
            if info.st_ino == target_inode:
                calls += 1
            if info.st_ino == target_inode and calls == 2:
                with original.open("ab") as stream:
                    stream.write(b" changed")
            return real_fstat(fd)

        with mock.patch("scripts.runtime_catalog.os.fstat", side_effect=mutating_fstat):
            with self.assertRaisesRegex((ValueError, OSError), "chang|replac|mutat|input"):
                self.build()

    def test_refuses_existing_output_alias_output_symlink_and_symlink_parent(self):
        target = self.root / "result.json"
        target.write_text("owned elsewhere")
        with self.assertRaises((FileExistsError, ValueError)):
            write_catalog(version=VERSION, source_sha=SHA, artifacts=self.inputs, output=target)
        alias = self.root / "alias.json"
        alias.symlink_to(self.inputs["agent"])
        with self.assertRaises((ValueError, OSError)):
            write_catalog(version=VERSION, source_sha=SHA, artifacts=self.inputs, output=alias)
        link_dir = self.root / "linked-dir"
        link_dir.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            write_catalog(version=VERSION, source_sha=SHA, artifacts=self.inputs, output=link_dir / "new.json")
        (self.root / "sub").mkdir()
        nested_link = self.root / "nested-link"
        nested_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            write_catalog(version=VERSION, source_sha=SHA, artifacts=self.inputs, output=nested_link / "sub" / "new.json")
        with self.assertRaises((ValueError, OSError)):
            write_catalog(version=VERSION, source_sha=SHA, artifacts=self.inputs, output=self.root / "missing" / "new.json")
        not_directory = self.root / "not-directory"
        not_directory.write_text("file")
        with self.assertRaises((ValueError, OSError)):
            write_catalog(version=VERSION, source_sha=SHA, artifacts=self.inputs, output=not_directory / "new.json")
        with self.assertRaises((ValueError, OSError)):
            write_catalog(version=VERSION, source_sha=SHA, artifacts=self.inputs, output=self.inputs["agent"])

    def test_does_not_overwrite_destination_that_appears_during_promotion(self):
        output = self.root / "raced.json"
        real_link = os.link

        def create_then_link(src, dst, *args, **kwargs):
            output.write_text("foreign")
            return real_link(src, dst, *args, **kwargs)

        with mock.patch("scripts.runtime_catalog.os.link", side_effect=create_then_link):
            with self.assertRaises((FileExistsError, ValueError)):
                write_catalog(version=VERSION, source_sha=SHA, artifacts=self.inputs, output=output)
        self.assertEqual(output.read_text(), "foreign")
        self.assertFalse(list(self.root.glob(".*.tmp")))

    def test_rejects_catalog_self_reference_and_has_no_timestamp_or_digest_of_itself(self):
        catalog = self.build()
        catalog["artifacts"]["fixture"]["name"] = "ashnazg-runtime-v1.2.3.json"
        with self.assertRaisesRegex(ValueError, "name|catalog|fixture"):
            validate_catalog(catalog)
        output = self.root / f"ashnazg-runtime-v{VERSION}.json"
        write_catalog(version=VERSION, source_sha=SHA, artifacts=self.inputs, output=output)
        decoded = json.loads(output.read_bytes())
        self.assertNotIn("sha256", decoded)
        self.assertNotIn("timestamp", decoded)


if __name__ == "__main__":
    unittest.main()
