#!/usr/bin/env python3
"""Build an opt-in local ReDroid image with the experimental source HWC module."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

if __package__:
    from . import build_android_image as base
else:
    import build_android_image as base


ROOT = Path(__file__).resolve().parent.parent
RECIPE_DIR = ROOT / "runtime" / "hwc-image"
ALLOWED_HAL_PATHS = (
    "/vendor/lib64/hw/hwcomposer.redroid.so",
    "/system/lib64/hw/hwcomposer.redroid.so",
)
HWC_PROFILE = "experimental-static-configs-client-composition-no-governor"
CONTEXT_INPUTS = ("Dockerfile", "ashnazg-animation.rc", "NOTICES.md")
DESCRIPTOR_KEYS = {
    "schema", "recipe_version", "source_sha", "source_dirty", "upstream_base", "platform",
    "apk_inputs", "image_inspect", "notices", "qualification",
}
APK_NAMES = base.APK_NAMES
BASE_LABELS = {
    "org.ashnazg.base-image": base.BASE_IMAGE,
}
UPSTREAM_ENTRYPOINT = base.UPSTREAM_ENTRYPOINT
BOOT_CMD = base.BOOT_CMD


class HwcImageBuildError(RuntimeError):
    """The requested experimental image could not be built with verified inputs."""


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _command(runner: Callable, args: list[str], *, check: bool = True,
             timeout: int = 1800) -> CommandResult:
    try:
        result = runner(args, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HwcImageBuildError(f"command could not complete: {args[0]}") from exc
    response = CommandResult(result.returncode, result.stdout or "", result.stderr or "")
    if check and response.returncode:
        detail = response.stderr.strip() or response.stdout.strip()
        raise HwcImageBuildError(f"{args[0]} failed with exit {response.returncode}: {detail[:1000]}")
    return response


def validate_elf(contents: bytes, platform: str) -> str:
    """Return the ELF machine label after checking a 64-bit module matches the image."""
    expected = {"linux/amd64": (62, "x86_64"), "linux/arm64": (183, "aarch64")}
    if platform not in expected:
        raise ValueError("platform must be linux/amd64 or linux/arm64")
    if len(contents) < 20 or contents[:4] != b"\x7fELF" or contents[4] != 2 or contents[5] != 1:
        raise ValueError("HWC module must be a little-endian 64-bit ELF shared library")
    if struct.unpack_from("<H", contents, 16)[0] != 3:
        raise ValueError("HWC module ELF must be a shared object")
    machine = struct.unpack_from("<H", contents, 18)[0]
    wanted, label = expected[platform]
    if machine != wanted:
        raise ValueError(f"HWC module ELF machine {machine} does not match {platform}")
    return label


def select_hal_path(found: dict[str, bytes]) -> str:
    paths = [path for path in ALLOWED_HAL_PATHS if path in found]
    if len(paths) > 1:
        raise HwcImageBuildError(f"ambiguous pinned-base HWC layout; found multiple allowlisted paths: {paths}")
    if not paths:
        raise HwcImageBuildError("pinned base has no supported 64-bit hwcomposer.redroid.so path")
    return paths[0]


def _read_regular(path: Path, description: str) -> bytes:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"{description} must be an existing regular, non-symlink file: {path}")
    content = path.read_bytes()
    if not content:
        raise ValueError(f"{description} is empty: {path}")
    return content


def _validate_inputs(*, source_sha: str, version: str, platform: str, tag: str,
                     output: Path, agent_apk: Path, fixture_apk: Path,
                     hwc_module: Path | bytes) -> tuple[dict[str, bytes], dict[str, str], bool]:
    if version != base.VERSION:
        raise ValueError(f"version must be {base.VERSION} to remain compatible with the ReDroid provenance verifier")
    if tag != f"ashnazg-redroid:{version}":
        raise ValueError(f"image tag must be ashnazg-redroid:{version} for the existing ReDroid verifier")
    if platform not in base.BASE_PLATFORM_DIGESTS:
        raise ValueError("platform must be linux/amd64 or linux/arm64")
    if len(source_sha) != 40 or any(ch not in "0123456789abcdef" for ch in source_sha):
        raise ValueError("source SHA must be a full 40-character lowercase commit SHA")
    try:
        checkout_sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                                      text=True, check=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all"],
                               capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise HwcImageBuildError("cannot verify source SHA and clean working tree provenance") from exc
    if source_sha != checkout_sha:
        raise ValueError("source SHA must match this checkout's HEAD commit")
    if dirty:
        raise ValueError("HWC image builds require a clean source checkout")
    if output.exists() and (output.is_symlink() or not output.is_file()):
        raise ValueError("provenance output must be a regular file path")
    inputs: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    for name, path in (("agent", agent_apk), ("fixture", fixture_apk)):
        content = _read_regular(Path(path), f"{name} APK")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as apk:
                if "AndroidManifest.xml" not in apk.namelist():
                    raise ValueError
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            raise ValueError(f"{name} APK is not a valid APK archive with AndroidManifest.xml: {path}") from exc
        inputs[APK_NAMES[name]] = content
        hashes[name] = hashlib.sha256(content).hexdigest()
    module = hwc_module if isinstance(hwc_module, bytes) else _read_regular(Path(hwc_module), "HWC module")
    validate_elf(module, platform)
    inputs["hwcomposer.default.so"] = module
    protected = {Path(agent_apk).resolve(), Path(fixture_apk).resolve()}
    if not isinstance(hwc_module, bytes):
        protected.add(Path(hwc_module).resolve())
    protected.update((RECIPE_DIR / name).resolve() for name in CONTEXT_INPUTS)
    if output.resolve() in protected:
        raise ValueError("provenance output cannot replace a build input")
    return inputs, hashes, False


def _remove_owned_probe(*, docker: str, name: str, token: str,
                        container_id: str | None, runner: Callable) -> None:
    # An ambiguous create may have succeeded, or its generated name may belong
    # to someone else. Only a matching random label grants cleanup ownership.
    result = _command(runner, [docker, "inspect", container_id or name], check=False, timeout=30)
    if result.returncode:
        detail = (result.stderr + result.stdout).lower()
        if any(marker in detail for marker in ("no such object", "no such container", "not found")):
            return
        raise HwcImageBuildError("could not verify probe cleanup ownership; no container was removed")
    try:
        documents = json.loads(result.stdout)
        if not isinstance(documents, list) or len(documents) != 1:
            raise ValueError
        document = documents[0]
        actual_id = document["Id"]
        labels = document["Config"]["Labels"]
        if not isinstance(actual_id, str) or not re.fullmatch(r"[0-9a-f]{64}", actual_id):
            raise ValueError
        if not isinstance(labels, dict):
            raise ValueError
    except (ValueError, TypeError, KeyError) as exc:
        raise HwcImageBuildError("could not verify probe cleanup ownership; no container was removed") from exc
    if labels.get("org.ashnazg.hwc.probe-id") != token:
        if container_id is None:
            return  # Failed create collided with a foreign container; preserve it.
        raise HwcImageBuildError("probe cleanup ownership did not match; no container was removed")
    if container_id is not None and actual_id != container_id:
        raise HwcImageBuildError("probe cleanup ID did not match; no container was removed")
    # Immutable ID avoids deleting a replacement that takes the generated name.
    # --volumes also removes anonymous data allocated by the base image's VOLUME.
    _command(runner, [docker, "rm", "-f", "--volumes", actual_id], timeout=30)


def _probe_hal_path(*, docker: str, platform: str, runner: Callable) -> str:
    """Inspect allowlisted paths in a stopped, uniquely named container from the pinned base."""
    name = f"ashnazg-hwc-probe-{uuid.uuid4().hex}"
    token = uuid.uuid4().hex
    container_id = None
    try:
        result = _command(runner, [docker, "create", "--platform", platform, "--name", name,
                                   "--label", f"org.ashnazg.hwc.probe-id={token}", "--entrypoint", "/init", base.BASE_IMAGE], timeout=30)
        value = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise HwcImageBuildError("Docker did not return a valid ID for the owned pinned-base probe container")
        container_id = value
        found: dict[str, bytes] = {}
        with tempfile.TemporaryDirectory(prefix="ashnazg-hwc-probe-") as temp:
            for index, path in enumerate(ALLOWED_HAL_PATHS):
                target = Path(temp) / f"candidate-{index}.so"
                copied = _command(runner, [docker, "cp", f"{container_id}:{path}", str(target)], check=False, timeout=30)
                if copied.returncode == 0:
                    if not target.is_file() or target.is_symlink():
                        raise HwcImageBuildError(f"Docker returned no regular file for pinned-base path {path}")
                    found[path] = target.read_bytes()
                elif not any(phrase in (copied.stderr + copied.stdout).lower()
                             for phrase in ("no such file", "could not find", "not found")):
                    raise HwcImageBuildError(f"Docker could not inspect pinned-base path {path}: "
                                             f"{(copied.stderr or copied.stdout).strip()[:500]}")
        selected = select_hal_path(found)
        validate_elf(found[selected], platform)
        return selected
    finally:
        _remove_owned_probe(docker=docker, name=name, token=token,
                            container_id=container_id, runner=runner)


def _inspect_metadata(raw: str, *, platform: str, tag: str, source_sha: str,
                      version: str, apk_sha256: dict[str, str]) -> dict:
    return base._inspect_metadata(raw, platform=platform, tag=tag, source_sha=source_sha,
                                  version=version, apk_sha256=apk_sha256)


def build_hwc_image(*, source_sha: str, version: str, platform: str, tag: str,
                    agent_apk: Path, fixture_apk: Path, hwc_module: Path | bytes,
                    output: Path, docker: str = "docker", runner: Callable = subprocess.run) -> dict:
    inputs, apk_sha256, source_dirty = _validate_inputs(
        source_sha=source_sha, version=version, platform=platform, tag=tag, output=output,
        agent_apk=agent_apk, fixture_apk=fixture_apk, hwc_module=hwc_module)
    recipe_bytes = {}
    for name in CONTEXT_INPUTS:
        source = RECIPE_DIR / name
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError(f"required HWC recipe input is missing: {source}")
        recipe_bytes[name] = source.read_bytes()
    selected_path = _probe_hal_path(docker=docker, platform=platform, runner=runner)
    hashes = {name: hashlib.sha256(contents).hexdigest() for name, contents in inputs.items()}
    selected_hashes = {name: hashlib.sha256(content).hexdigest() for name, content in recipe_bytes.items()}
    all_hashes = selected_hashes | hashes
    all_hashes["hwcomposer.default.so"] = hashes["hwcomposer.default.so"]
    labels = {
        "org.ashnazg.base-image": base.BASE_IMAGE,
        "org.ashnazg.version": version,
        "org.ashnazg.source-sha": source_sha,
        "org.ashnazg.platform": platform,
        "org.ashnazg.agent-sha256": apk_sha256["agent"],
        "org.ashnazg.fixture-sha256": apk_sha256["fixture"],
        "org.ashnazg.hwc-sha256": hashes["hwcomposer.default.so"],
        "org.ashnazg.hwc-paths": json.dumps([selected_path], separators=(",", ":")),
        "org.ashnazg.hwc-profile": HWC_PROFILE,
        "org.ashnazg.hwc-context-sha256": json.dumps(all_hashes, sort_keys=True, separators=(",", ":")),
    }
    with tempfile.TemporaryDirectory(prefix="ashnazg-hwc-context-") as temp:
        context = Path(temp)
        for name in CONTEXT_INPUTS:
            (context / name).write_bytes(recipe_bytes[name])
        for name, content in inputs.items():
            (context / name).write_bytes(content)
        for file in context.iterdir():
            file.chmod(0o644 if file.suffix == ".apk" else (0o755 if file.name == "hwcomposer.default.so" else 0o644))
            os.utime(file, (0, 0))
        args = [docker, "build", "--platform", platform, "--tag", tag,
                "--build-arg", f"SOURCE_SHA={source_sha}", "--build-arg", f"IMAGE_VERSION={version}",
                "--build-arg", f"AGENT_SHA256={apk_sha256['agent']}",
                "--build-arg", f"FIXTURE_SHA256={apk_sha256['fixture']}",
                "--build-arg", f"HWC_SHA256={hashes['hwcomposer.default.so']}",
                "--build-arg", f"HWC_PATHS={labels['org.ashnazg.hwc-paths']}",
                "--build-arg", f"HWC_PROFILE={HWC_PROFILE}",
                "--build-arg", f"HWC_CONTEXT_SHA256={labels['org.ashnazg.hwc-context-sha256']}",
                "--build-arg", f"HWC_DEST={selected_path}", str(context)]
        _command(runner, args)
        inspect = _command(runner, [docker, "image", "inspect", tag], timeout=30)
    metadata = _inspect_metadata(inspect.stdout, platform=platform, tag=tag, source_sha=source_sha,
                                 version=version, apk_sha256=apk_sha256)
    actual_labels = metadata["config"]["labels"]
    if any(actual_labels.get(key) != value for key, value in labels.items()):
        raise HwcImageBuildError("built image inspect metadata is missing or mismatches HWC provenance labels")
    descriptor = {
        "schema": 1,
        "recipe_version": version,
        "source_sha": source_sha,
        "source_dirty": source_dirty,
        "upstream_base": {"image": base.BASE_IMAGE, "index_digest": base.BASE_INDEX,
                          "platform_digest": base.BASE_PLATFORM_DIGESTS[platform]},
        "platform": platform,
        "apk_inputs": {
            "agent": {"context_path": APK_NAMES["agent"], "sha256": apk_sha256["agent"]},
            "fixture": {"context_path": APK_NAMES["fixture"], "sha256": apk_sha256["fixture"]},
        },
        "image_inspect": metadata,
        "notices": "runtime/hwc-image/NOTICES.md",
        "qualification": "experimental HWC image; runtime acceptance requires the source module dump marker; no governor, performance, or redistribution claim",
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor_fd, temporary = tempfile.mkstemp(prefix=output.name + ".", dir=output.parent)
    try:
        with os.fdopen(descriptor_fd, "w", encoding="utf-8") as stream:
            json.dump(descriptor, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return descriptor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", required=True, choices=sorted(base.BASE_PLATFORM_DIGESTS))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--agent-apk", required=True, type=Path)
    parser.add_argument("--fixture-apk", required=True, type=Path)
    parser.add_argument("--hwc-module", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--docker", default="docker")
    args = vars(parser.parse_args(argv))
    try:
        descriptor = build_hwc_image(**args)
    except (HwcImageBuildError, OSError, ValueError) as exc:
        print(f"build_hwc_image: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"image": descriptor["image_inspect"]["id"], "provenance": str(args["output"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
