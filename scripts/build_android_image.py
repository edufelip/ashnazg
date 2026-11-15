#!/usr/bin/env python3
"""Build a local, provenance-recorded Android 15 image from explicit APK inputs."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent.parent
RECIPE_DIR = ROOT / "runtime" / "redroid-image"
VERSION = "0.2.0"
BASE_INDEX = "sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad"
BASE_IMAGE = f"redroid/redroid@{BASE_INDEX}"
BASE_PLATFORM_DIGESTS = {
    "linux/amd64": "sha256:25879821f13ffd38d5d6047d832379b96cf72ec520508b45036dca25cdc3737f",
    "linux/arm64": "sha256:b21c57533662275928832602f7c09b83cb198aa9b271135ae49d978857d6795f",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
UPSTREAM_ENTRYPOINT = ["/init", "qemu=1", "androidboot.hardware=redroid"]
BOOT_CMD = ["androidboot.redroid_width=720", "androidboot.redroid_height=1280", "androidboot.redroid_dpi=320", "androidboot.redroid_fps=60", "androidboot.redroid_gpu_mode=guest", "androidboot.use_memfd=1"]
APK_NAMES = {"agent": "AshnazgAgent.apk", "fixture": "AshnazgFixture.apk"}


class AndroidImageBuildError(RuntimeError):
    """The requested local image could not be built with trustworthy provenance."""


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _run(runner: Callable, args: list[str]) -> CommandResult:
    try:
        result = runner(args, capture_output=True, text=True, check=False, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AndroidImageBuildError(f"command could not complete: {args[0]}") from exc
    response = CommandResult(result.returncode, result.stdout or "", result.stderr or "")
    if response.returncode:
        detail = response.stderr.strip() or response.stdout.strip()
        raise AndroidImageBuildError(f"{args[0]} failed with exit {response.returncode}: {detail[:1000]}")
    return response


def _read_apk(path: Path, label: str) -> tuple[bytes, str]:
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError
        content = path.read_bytes()
    except OSError as exc:
        raise FileNotFoundError(f"{label} APK must be an existing regular, non-symlink file: {path}") from exc
    if not content:
        raise ValueError(f"{label} APK is empty: {path}")
    if path.suffix.lower() != ".apk":
        raise ValueError(f"{label} APK input must use the .apk extension: {path}")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if "AndroidManifest.xml" not in archive.namelist():
                raise ValueError
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        raise ValueError(f"{label} APK is not a valid APK archive with AndroidManifest.xml: {path}") from exc
    return content, hashlib.sha256(content).hexdigest()


def _validate_inputs(*, source_sha: str, version: str, platform: str, tag: str,
                     output: Path, agent_apk: Path, fixture_apk: Path) -> tuple[dict[str, bytes], dict[str, str], bool]:
    if version != VERSION:
        raise ValueError(f"version must be {VERSION} for this recipe")
    if not SHA_RE.fullmatch(source_sha):
        raise ValueError("source SHA must be a full 40-character lowercase commit SHA")
    try:
        checkout_sha = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
            text=True, check=True, timeout=5,
        ).stdout.strip()
        source_dirty = bool(subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        raise AndroidImageBuildError("cannot verify source SHA and working tree provenance") from exc
    if source_sha != checkout_sha:
        raise ValueError("source SHA must match this checkout's HEAD commit")
    if platform not in BASE_PLATFORM_DIGESTS:
        raise ValueError("platform must be linux/amd64 or linux/arm64")
    if tag != f"ashnazg-redroid:{version}":
        raise ValueError(f"image tag must be the local tag ashnazg-redroid:{version}")
    output = Path(output)
    if output.exists() and output.is_symlink():
        raise ValueError("provenance output cannot be a symlink")
    if output.exists() and not output.is_file():
        raise ValueError("provenance output must be a regular file path")
    inputs = {"agent": Path(agent_apk), "fixture": Path(fixture_apk)}
    contents, hashes = {}, {}
    for name, path in inputs.items():
        contents[name], hashes[name] = _read_apk(path, name)
    if output.exists() and output.resolve() in {path.resolve() for path in inputs.values()}:
        raise ValueError("provenance output cannot replace an APK input")
    return contents, hashes, source_dirty


def _inspect_metadata(raw: str, *, platform: str, tag: str, source_sha: str,
                      version: str, apk_sha256: dict[str, str]) -> dict:
    try:
        decoded = json.loads(raw)
        if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict):
            raise ValueError
        image = decoded[0]
        config = image["Config"]
        labels = config["Labels"]
        os_name, architecture = platform.split("/", 1)
    except (ValueError, TypeError, KeyError) as exc:
        raise AndroidImageBuildError("Docker returned incomplete image inspect metadata") from exc
    if not isinstance(config, dict) or not isinstance(labels, dict):
        raise AndroidImageBuildError("Docker returned incomplete image inspect metadata")
    if config.get("Entrypoint") != UPSTREAM_ENTRYPOINT:
        raise AndroidImageBuildError("built image changed the pinned upstream entrypoint")
    if config.get("Cmd") != BOOT_CMD:
        raise AndroidImageBuildError("built image inspect command does not match the Android boot defaults")
    expected_labels = {
        "org.ashnazg.base-image": BASE_IMAGE,
        "org.ashnazg.version": version,
        "org.ashnazg.source-sha": source_sha,
        "org.ashnazg.platform": platform,
        "org.ashnazg.agent-sha256": apk_sha256["agent"],
        "org.ashnazg.fixture-sha256": apk_sha256["fixture"],
    }
    if image.get("Os") != os_name or image.get("Architecture") != architecture:
        raise AndroidImageBuildError("built image inspect platform does not match the requested platform")
    repo_tags = image.get("RepoTags")
    if not isinstance(repo_tags, list) or tag not in repo_tags:
        raise AndroidImageBuildError("built image inspect metadata is missing the requested local tag")
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise AndroidImageBuildError("built image inspect metadata is missing or mismatches required provenance labels")
    if not isinstance(image.get("Id"), str) or not image["Id"].startswith("sha256:"):
        raise AndroidImageBuildError("built image inspect metadata has no image ID")
    rootfs_metadata = image.get("RootFS")
    rootfs = rootfs_metadata.get("Layers") if isinstance(rootfs_metadata, dict) else None
    if not isinstance(rootfs, list) or not rootfs or any(not isinstance(layer, str) for layer in rootfs):
        raise AndroidImageBuildError("built image inspect metadata has no filesystem layer digests")
    return {
        "id": image["Id"],
        "repo_tags": repo_tags,
        "os": image["Os"],
        "architecture": image["Architecture"],
        "variant": image.get("Variant"),
        "config": {
            "entrypoint": config.get("Entrypoint"),
            "cmd": config.get("Cmd"),
            "user": config.get("User"),
            "working_dir": config.get("WorkingDir"),
            "labels": labels,
        },
        "rootfs_layers": rootfs,
    }


def build_android_image(*, source_sha: str, version: str, platform: str, tag: str,
                        agent_apk: Path, fixture_apk: Path, output: Path,
                        docker: str = "docker", runner: Callable = subprocess.run) -> dict:
    """Build in an isolated explicit-input context and write verified inspect provenance."""
    contents, apk_sha256, source_dirty = _validate_inputs(
        source_sha=source_sha, version=version, platform=platform, tag=tag,
        output=output, agent_apk=agent_apk, fixture_apk=fixture_apk,
    )
    required_recipe_files = ("Dockerfile", "ashnazg-animation.rc", "NOTICES.md")
    for name in required_recipe_files:
        path = RECIPE_DIR / name
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"required recipe/provenance input is missing: {path}")

    with tempfile.TemporaryDirectory(prefix="ashnazg-redroid-context-") as temp:
        context = Path(temp)
        for name in required_recipe_files:
            shutil.copyfile(RECIPE_DIR / name, context / name)
        for key, filename in APK_NAMES.items():
            (context / filename).write_bytes(contents[key])
        for path in context.iterdir():
            path.chmod(0o644)
            os.utime(path, (0, 0))
        build_args = [
            docker, "build", "--platform", platform,
            "--tag", tag,
            "--build-arg", f"SOURCE_SHA={source_sha}",
            "--build-arg", f"IMAGE_VERSION={version}",
            "--build-arg", f"AGENT_SHA256={apk_sha256['agent']}",
            "--build-arg", f"FIXTURE_SHA256={apk_sha256['fixture']}",
            str(context),
        ]
        _run(runner, build_args)
        inspect = _run(runner, [docker, "image", "inspect", tag])

    metadata = _inspect_metadata(inspect.stdout, platform=platform, tag=tag,
                                 source_sha=source_sha, version=version,
                                 apk_sha256=apk_sha256)
    provenance = {
        "schema": 1,
        "recipe_version": version,
        "source_sha": source_sha,
        "source_dirty": source_dirty,
        "upstream_base": {
            "image": BASE_IMAGE,
            "index_digest": BASE_INDEX,
            "platform_digest": BASE_PLATFORM_DIGESTS[platform],
        },
        "platform": platform,
        "apk_inputs": {
            "agent": {"context_path": APK_NAMES["agent"], "sha256": apk_sha256["agent"]},
            "fixture": {"context_path": APK_NAMES["fixture"], "sha256": apk_sha256["fixture"]},
        },
        "image_inspect": metadata,
        "notices": "runtime/redroid-image/NOTICES.md",
        "qualification": "recipe build only; no boot/runtime, license clearance, redistribution, or performance claim",
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=output.name + '.', dir=output.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(provenance, stream, indent=2, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary_name, output)
    finally:
        if os.path.exists(temporary_name): os.unlink(temporary_name)
    return provenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", required=True, choices=sorted(BASE_PLATFORM_DIGESTS))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--agent-apk", required=True, type=Path)
    parser.add_argument("--fixture-apk", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--docker", default="docker")
    args = vars(parser.parse_args(argv))
    try:
        provenance = build_android_image(**args)
    except (AndroidImageBuildError, FileNotFoundError, ValueError) as exc:
        print(f"build_android_image: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"image": provenance["image_inspect"]["id"], "provenance": str(args["output"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
