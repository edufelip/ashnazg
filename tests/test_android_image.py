import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import build_android_image

SOURCE_SHA = subprocess.run(
    ["git", "-C", str(build_android_image.ROOT), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True,
).stdout.strip()


class AndroidImageRecipeTests(unittest.TestCase):
    @staticmethod
    def write_apk(path, contents):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("AndroidManifest.xml", contents)
            archive.writestr("classes.dex", b"dex")

    def test_recipe_keeps_upstream_entrypoint_and_sets_boot_defaults(self):
        dockerfile = (build_android_image.RECIPE_DIR / "Dockerfile").read_text()
        animation = (build_android_image.RECIPE_DIR / "ashnazg-animation.rc").read_text()

        self.assertNotRegex(dockerfile, r"(?m)^\s*ENTRYPOINT\b")
        for value in (
            "androidboot.redroid_width=720",
            "androidboot.redroid_height=1280",
            "androidboot.redroid_dpi=320",
        ):
            self.assertIn(value, dockerfile)
        self.assertIn("debug.sf.nobootanimation 1", animation)
        self.assertIn("/system/app/AshnazgAgent/AshnazgAgent.apk", dockerfile)
        self.assertIn("/system/app/AshnazgFixture/AshnazgFixture.apk", dockerfile)
        self.assertIn("/usr/share/doc/ashnazg-redroid/NOTICES.md", dockerfile)

    def test_build_stages_only_explicit_inputs_and_records_inspect_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = root / "agent.apk"
            fixture = root / "fixture.apk"
            self.write_apk(agent, b"agent-apk")
            self.write_apk(fixture, b"fixture-apk")
            agent_digest = hashlib.sha256(agent.read_bytes()).hexdigest()
            fixture_digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
            output = root / "out" / "provenance.json"
            commands = []

            def runner(args, **kwargs):
                commands.append(args)
                if args[1:3] == ["build", "--platform"]:
                    context = Path(args[-1])
                    self.assertEqual(
                        sorted(path.relative_to(context).as_posix() for path in context.rglob("*")),
                        ["AshnazgAgent.apk", "AshnazgFixture.apk", "Dockerfile", "NOTICES.md", "ashnazg-animation.rc"],
                    )
                    self.assertTrue(all(path.stat().st_mtime == 0 for path in context.iterdir()))
                    return build_android_image.CommandResult(0, "", "")
                return build_android_image.CommandResult(0, json.dumps([{
                    "Id": "sha256:" + "a" * 64,
                    "RepoTags": ["ashnazg-redroid:0.2.0"],
                    "Os": "linux",
                    "Architecture": "amd64",
                    "Config": {
                        "Entrypoint": build_android_image.UPSTREAM_ENTRYPOINT,
                        "Cmd": build_android_image.BOOT_CMD,
                        "Labels": {
                            "org.ashnazg.base-image": build_android_image.BASE_IMAGE,
                            "org.ashnazg.version": "0.2.0",
                            "org.ashnazg.source-sha": SOURCE_SHA,
                            "org.ashnazg.platform": "linux/amd64",
                            "org.ashnazg.agent-sha256": agent_digest,
                            "org.ashnazg.fixture-sha256": fixture_digest,
                        },
                    },
                    "RootFS": {"Layers": ["sha256:" + "c" * 64]},
                }]), "")

            result = build_android_image.build_android_image(
                source_sha=SOURCE_SHA,
                version="0.2.0",
                platform="linux/amd64",
                tag="ashnazg-redroid:0.2.0",
                agent_apk=agent,
                fixture_apk=fixture,
                output=output,
                runner=runner,
            )

            self.assertEqual(result["image_inspect"]["id"], "sha256:" + "a" * 64)
            self.assertEqual(result["apk_inputs"]["agent"]["sha256"], agent_digest)
            self.assertEqual(result["apk_inputs"]["fixture"]["sha256"], fixture_digest)
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[1][:4], ["docker", "image", "inspect", "ashnazg-redroid:0.2.0"])

    def test_rejects_bad_version_sha_tag_platform_and_missing_apks_before_docker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = root / "agent.apk"
            fixture = root / "fixture.apk"
            self.write_apk(agent, b"a")
            self.write_apk(fixture, b"f")
            calls = []
            options = dict(
                source_sha=SOURCE_SHA,
                version="0.2.0",
                platform="linux/amd64",
                tag="ashnazg-redroid:0.2.0",
                agent_apk=agent,
                fixture_apk=fixture,
                output=root / "prov.json",
                runner=lambda *a, **k: calls.append(a),
            )
            invalid_options = (
                {"version": "0.3.0"},
                {"source_sha": "deadbeef"},
                {"tag": "docker.io/ashnazg-redroid:0.2.0"},
                {"tag": "ashnazg-redroid:0.2.1"},
                {"platform": "linux/s390x"},
                {"source_sha": "c" * 40},
                {"agent_apk": root / "absent.apk"},
            )
            for invalid in invalid_options:
                with self.subTest(invalid=invalid), self.assertRaises((ValueError, FileNotFoundError)):
                    build_android_image.build_android_image(**(options | invalid))
            self.assertEqual(calls, [])

    def test_rejects_apk_without_manifest_before_docker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            invalid_apk = root / "not-an-apk.apk"
            invalid_apk.write_bytes(b"not a zip archive")
            valid_apk = root / "valid.apk"
            self.write_apk(valid_apk, b"manifest")
            called = []
            with self.assertRaisesRegex(ValueError, "valid APK archive"):
                build_android_image.build_android_image(
                    source_sha=SOURCE_SHA,
                    version="0.2.0",
                    platform="linux/amd64",
                    tag="ashnazg-redroid:0.2.0",
                    agent_apk=invalid_apk,
                    fixture_apk=valid_apk,
                    output=root / "prov.json",
                    runner=lambda *a, **k: called.append(a),
                )
            self.assertEqual(called, [])

    def test_rejects_inspect_output_without_provenance_labels_or_actual_platform(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = root / "agent.apk"
            fixture = root / "fixture.apk"
            self.write_apk(agent, b"a")
            self.write_apk(fixture, b"f")
            responses = iter([
                build_android_image.CommandResult(0),
                build_android_image.CommandResult(0, json.dumps([{
                    "Id": "sha256:" + "a" * 64,
                    "RepoTags": ["ashnazg-redroid:0.2.0"],
                    "Os": "linux",
                    "Architecture": "arm64",
                    "Config": {"Entrypoint": build_android_image.UPSTREAM_ENTRYPOINT,
                               "Cmd": build_android_image.BOOT_CMD, "Labels": {}},
                }]), ""),
            ])
            with self.assertRaisesRegex(build_android_image.AndroidImageBuildError, "platform"):
                build_android_image.build_android_image(
                    source_sha=SOURCE_SHA,
                    version="0.2.0",
                    platform="linux/amd64",
                    tag="ashnazg-redroid:0.2.0",
                    agent_apk=agent,
                    fixture_apk=fixture,
                    output=root / "prov.json",
                    runner=lambda *a, **k: next(responses),
                )
            self.assertFalse((root / "prov.json").exists())

    def test_rejects_inspect_output_without_required_provenance_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = root / "agent.apk"
            fixture = root / "fixture.apk"
            self.write_apk(agent, b"agent")
            self.write_apk(fixture, b"fixture")
            responses = iter([
                build_android_image.CommandResult(0),
                build_android_image.CommandResult(0, json.dumps([{
                    "Id": "sha256:" + "a" * 64,
                    "RepoTags": ["ashnazg-redroid:0.2.0"],
                    "Os": "linux",
                    "Architecture": "amd64",
                    "Config": {"Entrypoint": build_android_image.UPSTREAM_ENTRYPOINT,
                               "Cmd": build_android_image.BOOT_CMD, "Labels": {}},
                    "RootFS": {"Layers": ["sha256:" + "b" * 64]},
                }]), ""),
            ])
            with self.assertRaisesRegex(build_android_image.AndroidImageBuildError, "provenance labels"):
                build_android_image.build_android_image(
                    source_sha=SOURCE_SHA,
                    version="0.2.0",
                    platform="linux/amd64",
                    tag="ashnazg-redroid:0.2.0",
                    agent_apk=agent,
                    fixture_apk=fixture,
                    output=root / "prov.json",
                    runner=lambda *a, **k: next(responses),
                )
            self.assertFalse((root / "prov.json").exists())


if __name__ == "__main__":
    unittest.main()
