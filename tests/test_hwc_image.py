import hashlib
import json
import struct
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import build_hwc_image, redroid


class HwcImageTests(unittest.TestCase):
    @staticmethod
    def write_apk(path, marker):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("AndroidManifest.xml", marker)
            archive.writestr("classes.dex", b"dex")

    @staticmethod
    def elf(machine):
        header = bytearray(64)
        header[:4] = b"\x7fELF"
        header[4] = 2  # ELFCLASS64
        header[5] = 1  # little endian
        struct.pack_into("<H", header, 16, 3)  # ET_DYN
        struct.pack_into("<H", header, 18, machine)
        return bytes(header)

    def test_accepts_only_expected_64_bit_elf_architecture(self):
        self.assertEqual(build_hwc_image.validate_elf(self.elf(62), "linux/amd64"), "x86_64")
        self.assertEqual(build_hwc_image.validate_elf(self.elf(183), "linux/arm64"), "aarch64")
        for contents, platform in (
            (self.elf(183), "linux/amd64"),
            (self.elf(62), "linux/arm64"),
            (self.elf(3), "linux/amd64"),
            (b"not ELF", "linux/amd64"),
        ):
            with self.subTest(platform=platform, contents=contents[:10]), self.assertRaises(ValueError):
                build_hwc_image.validate_elf(contents, platform)

    def test_recipe_retains_parent_entrypoint_and_known_boot_command(self):
        dockerfile = (build_hwc_image.RECIPE_DIR / "Dockerfile").read_text()
        self.assertNotRegex(dockerfile, r"(?m)^\s*ENTRYPOINT\b")
        self.assertIn(json.dumps(build_hwc_image.BOOT_CMD), dockerfile)
        for label in ("org.ashnazg.hwc-sha256", "org.ashnazg.hwc-paths", "org.ashnazg.hwc-profile"):
            self.assertIn(label, dockerfile)
        self.assertIn("${HWC_DEST}", dockerfile)
        self.assertEqual(build_hwc_image.UPSTREAM_ENTRYPOINT,
                         ["/init", "qemu=1", "androidboot.hardware=redroid"])

    def test_rejects_candidate_outside_allowlist_and_ambiguous_layout(self):
        with self.assertRaisesRegex(build_hwc_image.HwcImageBuildError, "supported"):
            build_hwc_image.select_hal_path({"/system/lib64/hw/hwcomposer.default.so": b"x"})
        candidate = build_hwc_image.ALLOWED_HAL_PATHS[0]
        with self.assertRaisesRegex(build_hwc_image.HwcImageBuildError, "ambiguous"):
            build_hwc_image.select_hal_path({candidate: b"a", build_hwc_image.ALLOWED_HAL_PATHS[1]: b"b"})

    def test_build_records_extra_labels_and_hashes_every_staged_input(self):
        source_sha = "a" * 40
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent, fixture = root / "agent.apk", root / "fixture.apk"
            self.write_apk(agent, b"agent")
            self.write_apk(fixture, b"fixture")
            output = root / "provenance.json"
            commands = []
            ownership = {}

            def runner(args, **kwargs):
                commands.append(args)
                if args[1:3] == ["create", "--platform"]:
                    ownership["token"] = args[args.index("--label") + 1].split("=", 1)[1]
                    return build_hwc_image.CommandResult(0, "d" * 64)
                if args[1] == "inspect":
                    return build_hwc_image.CommandResult(0, json.dumps([{
                        "Id": "d" * 64, "Config": {"Labels": {"org.ashnazg.hwc.probe-id": ownership["token"]}}
                    }]))
                if args[1] == "cp":
                    source = args[2].split(":", 1)[1]
                    target = Path(args[3])
                    if source == build_hwc_image.ALLOWED_HAL_PATHS[0]:
                        target.write_bytes(self.elf(62))
                        return build_hwc_image.CommandResult(0)
                    return build_hwc_image.CommandResult(1, "", "not found")
                if args[1:3] == ["build", "--platform"]:
                    context = Path(args[-1])
                    paths = sorted(path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file())
                    self.assertEqual(paths, sorted((*build_hwc_image.CONTEXT_INPUTS,
                        "AshnazgAgent.apk", "AshnazgFixture.apk", "hwcomposer.default.so",
                    )))
                    return build_hwc_image.CommandResult(0)
                if args[1:4] == ["image", "inspect", "ashnazg-redroid:0.2.0"]:
                    context_hashes = {
                        name: hashlib.sha256((build_hwc_image.RECIPE_DIR / name).read_bytes()).hexdigest()
                        for name in build_hwc_image.CONTEXT_INPUTS
                    }
                    context_hashes.update({
                        "AshnazgAgent.apk": hashlib.sha256(agent.read_bytes()).hexdigest(),
                        "AshnazgFixture.apk": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                        "hwcomposer.default.so": hashlib.sha256(self.elf(62)).hexdigest(),
                    })
                    labels = dict(build_hwc_image.BASE_LABELS)
                    labels.update({
                        "org.ashnazg.version": "0.2.0",
                        "org.ashnazg.source-sha": source_sha,
                        "org.ashnazg.platform": "linux/amd64",
                        "org.ashnazg.agent-sha256": hashlib.sha256(agent.read_bytes()).hexdigest(),
                        "org.ashnazg.fixture-sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                        "org.ashnazg.hwc-sha256": hashlib.sha256(self.elf(62)).hexdigest(),
                        "org.ashnazg.hwc-paths": json.dumps([build_hwc_image.ALLOWED_HAL_PATHS[0]]),
                        "org.ashnazg.hwc-profile": build_hwc_image.HWC_PROFILE,
                        "org.ashnazg.hwc-context-sha256": json.dumps(context_hashes, sort_keys=True, separators=(",", ":")),
                    })
                    return build_hwc_image.CommandResult(0, json.dumps([{
                        "Id": "sha256:" + "b" * 64,
                        "RepoTags": ["ashnazg-redroid:0.2.0"], "Os": "linux", "Architecture": "amd64",
                        "Config": {"Entrypoint": build_hwc_image.UPSTREAM_ENTRYPOINT,
                                   "Cmd": build_hwc_image.BOOT_CMD, "Labels": labels},
                        "RootFS": {"Layers": ["sha256:" + "c" * 64]},
                    }]))
                if args[1:3] == ["rm", "-f"]:
                    return build_hwc_image.CommandResult(0)
                self.fail(f"unexpected command: {args}")

            git_responses = iter([
                type("GitResult", (), {"stdout": source_sha})(),
                type("GitResult", (), {"stdout": ""})(),
            ])
            with patch.object(build_hwc_image.subprocess, "run", side_effect=lambda *a, **k: next(git_responses)):
                result = build_hwc_image.build_hwc_image(
                    source_sha=source_sha, version="0.2.0", platform="linux/amd64", tag="ashnazg-redroid:0.2.0",
                    agent_apk=agent, fixture_apk=fixture, hwc_module=self.elf(62), output=output, runner=runner,
                )

            self.assertFalse(set(result) - build_hwc_image.DESCRIPTOR_KEYS)
            labels = result["image_inspect"]["config"]["labels"]
            self.assertEqual(labels["org.ashnazg.hwc-paths"], json.dumps([build_hwc_image.ALLOWED_HAL_PATHS[0]]))
            self.assertEqual(json.loads(labels["org.ashnazg.hwc-context-sha256"]).keys(),
                             set((*build_hwc_image.CONTEXT_INPUTS, "AshnazgAgent.apk", "AshnazgFixture.apk", "hwcomposer.default.so")))
            self.assertTrue(any(command[:3] == ["docker", "rm", "-f"] for command in commands))
            self.assertEqual(json.loads(output.read_text()), result)
            metadata = result["image_inspect"]
            inspect_raw = json.dumps([{
                "Id": metadata["id"], "RepoTags": metadata["repo_tags"],
                "Os": metadata["os"], "Architecture": metadata["architecture"],
                "Variant": metadata["variant"],
                "Config": {
                    "Entrypoint": metadata["config"]["entrypoint"],
                    "Cmd": metadata["config"]["cmd"],
                    "User": metadata["config"]["user"],
                    "WorkingDir": metadata["config"]["working_dir"],
                    "Labels": metadata["config"]["labels"],
                },
                "RootFS": {"Layers": metadata["rootfs_layers"]},
            }])
            self.assertEqual(redroid.validate_image_provenance(
                output, architecture="amd64", checkout_sha=source_sha,
                checkout_dirty=False, inspect_raw=inspect_raw), metadata["id"])

    def test_create_collision_never_removes_the_existing_container(self):
        commands = []
        def runner(args, **kwargs):
            commands.append(args)
            if args[1] == "create":
                return build_hwc_image.CommandResult(1, "", "name is already in use")
            if args[1] == "inspect":
                return build_hwc_image.CommandResult(0, json.dumps([{
                    "Id": "f" * 64, "Config": {"Labels": {"org.ashnazg.hwc.probe-id": "foreign"}}
                }]))
            if args[1] == "rm":
                return build_hwc_image.CommandResult(0)
            self.fail(f"unexpected command: {args}")
        with self.assertRaises(build_hwc_image.HwcImageBuildError):
            build_hwc_image._probe_hal_path(docker="docker", platform="linux/amd64", runner=runner)
        self.assertFalse(any(command[1] == "rm" for command in commands))

    def test_probe_create_allows_base_image_pull_timeout(self):
        timeouts = {}

        def runner(args, **kwargs):
            timeouts[args[1]] = kwargs.get("timeout")
            if args[1] == "create":
                ownership_token = args[args.index("--label") + 1].split("=", 1)[1]
                timeouts["token"] = ownership_token
                return build_hwc_image.CommandResult(0, "d" * 64)
            if args[1] == "inspect":
                return build_hwc_image.CommandResult(0, json.dumps([{
                    "Id": "d" * 64, "Config": {"Labels": {"org.ashnazg.hwc.probe-id": timeouts["token"]}}
                }]))
            if args[1] == "cp":
                if args[2].endswith(build_hwc_image.ALLOWED_HAL_PATHS[0]):
                    Path(args[3]).write_bytes(self.elf(62))
                    return build_hwc_image.CommandResult(0)
                return build_hwc_image.CommandResult(1, "", "not found")
            if args[1] == "rm":
                return build_hwc_image.CommandResult(0)
            self.fail(f"unexpected command: {args}")

        build_hwc_image._probe_hal_path(docker="docker", platform="linux/amd64", runner=runner)
        self.assertGreaterEqual(timeouts["create"], 1800)

    def test_ambiguous_create_cleans_only_the_matching_label_and_immutable_id(self):
        ownership = {}
        removed = []
        def runner(args, **kwargs):
            if args[1] == "create":
                ownership["token"] = args[args.index("--label") + 1].split("=", 1)[1]
                raise subprocess.TimeoutExpired(args, 30)
            if args[1] == "inspect":
                return build_hwc_image.CommandResult(0, json.dumps([{
                    "Id": "d" * 64, "Config": {"Labels": {"org.ashnazg.hwc.probe-id": ownership["token"]}}
                }]))
            if args[1] == "rm":
                removed.append(args)
                return build_hwc_image.CommandResult(0)
            self.fail(f"unexpected command: {args}")
        with self.assertRaisesRegex(build_hwc_image.HwcImageBuildError, "could not complete"):
            build_hwc_image._probe_hal_path(docker="docker", platform="linux/amd64", runner=runner)
        self.assertEqual(removed, [["docker", "rm", "-f", "--volumes", "d" * 64]])

    def test_probe_cleanup_failure_cannot_report_a_successful_probe(self):
        ownership = {}
        def runner(args, **kwargs):
            if args[1] == "create":
                if "--label" in args:
                    ownership["token"] = args[args.index("--label") + 1].split("=", 1)[1]
                return build_hwc_image.CommandResult(0, "d" * 64)
            if args[1] == "inspect":
                return build_hwc_image.CommandResult(0, json.dumps([{
                    "Id": "d" * 64, "Config": {"Labels": {"org.ashnazg.hwc.probe-id": ownership["token"]}}
                }]))
            if args[1] == "cp":
                if args[2].endswith(build_hwc_image.ALLOWED_HAL_PATHS[0]):
                    Path(args[3]).write_bytes(self.elf(62))
                    return build_hwc_image.CommandResult(0)
                return build_hwc_image.CommandResult(1, "", "not found")
            if args[1] == "rm":
                return build_hwc_image.CommandResult(1, "", "removal denied")
            self.fail(f"unexpected command: {args}")
        with self.assertRaisesRegex(build_hwc_image.HwcImageBuildError, "removal denied"):
            build_hwc_image._probe_hal_path(docker="docker", platform="linux/amd64", runner=runner)

    def test_requires_clean_source_and_rejects_bad_module_before_docker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent, fixture = root / "agent.apk", root / "fixture.apk"
            self.write_apk(agent, b"agent")
            self.write_apk(fixture, b"fixture")
            calls = []
            git_responses = iter([
                type("GitResult", (), {"stdout": "a" * 40})(),
                type("GitResult", (), {"stdout": "?? dirty\n"})(),
            ])
            with patch.object(build_hwc_image.subprocess, "run", side_effect=lambda *a, **k: next(git_responses)):
                with self.assertRaisesRegex(ValueError, "clean"):
                    build_hwc_image.build_hwc_image(
                        source_sha="a" * 40, version="0.2.0", platform="linux/amd64", tag="ashnazg-redroid:0.2.0",
                        agent_apk=agent, fixture_apk=fixture, hwc_module=self.elf(62),
                        output=root / "out.json", runner=lambda *a, **k: calls.append(a),
                    )
            self.assertEqual(calls, [])

    def test_rejects_wrong_architecture_module_before_docker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent, fixture = root / "agent.apk", root / "fixture.apk"
            self.write_apk(agent, b"agent")
            self.write_apk(fixture, b"fixture")
            git_responses = iter([
                type("GitResult", (), {"stdout": "a" * 40})(),
                type("GitResult", (), {"stdout": ""})(),
            ])
            calls = []
            with patch.object(build_hwc_image.subprocess, "run", side_effect=lambda *a, **k: next(git_responses)):
                with self.assertRaisesRegex(ValueError, "does not match linux/amd64"):
                    build_hwc_image.build_hwc_image(
                        source_sha="a" * 40, version="0.2.0", platform="linux/amd64",
                        tag="ashnazg-redroid:0.2.0", agent_apk=agent, fixture_apk=fixture,
                        hwc_module=self.elf(183), output=root / "out.json",
                        runner=lambda *a, **k: calls.append(a),
                    )
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
