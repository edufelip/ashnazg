#!/usr/bin/env python3
"""Run Gradle in CI with bounded retries on transient HTTP/429 download errors."""

from __future__ import annotations

import argparse
from collections import deque
import os
from pathlib import Path
import subprocess
import sys
import time

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = (15.0, 45.0)
TRANSIENT_MARKERS = (
    "status code 429",
    "too many requests",
    "status code 502",
    "status code 503",
    "status code 504",
    "could not get resource",
    "could not get 'https://",
    "could not get \"https://",
    "could not head resource",
    "could not head 'https://",
    "could not head \"https://",
    "could not transfer artifact",
    "read timed out",
    "connect timed out",
    "connection reset",
    "remote host terminated the handshake",
)


def is_transient_failure(output: str) -> bool:
    """Return True only when Gradle output indicates an upstream transport/rate-limit error."""
    lowered = output.lower()
    return any(marker in lowered for marker in TRANSIENT_MARKERS)


def parse_backoff(raw: str) -> list[float]:
    if not raw.strip():
        return []
    values: list[float] = []
    for part in raw.split(","):
        delay = float(part.strip())
        if delay < 0:
            raise ValueError("backoff delays must be non-negative")
        values.append(delay)
    return values


def run_gradle_with_retries(
    *,
    workdir: Path,
    gradle_args: list[str],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: list[float] | None = None,
    gradle_bin: Path | None = None,
) -> int:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if not gradle_args:
        raise ValueError("at least one Gradle task or flag is required")
    env_bin = os.environ.get("ASHNAZG_CI_GRADLE_BIN")
    if gradle_bin is not None:
        executable = gradle_bin.resolve()
    elif env_bin:
        candidate = Path(env_bin)
        executable = candidate.resolve() if candidate.is_file() or os.sep in env_bin else candidate
    else:
        executable = (workdir / "gradlew").resolve()
    workdir = workdir.resolve()
    delays = list(DEFAULT_BACKOFF_SECONDS if backoff_seconds is None else backoff_seconds)

    for attempt in range(1, max_attempts + 1):
        tail: deque[str] = deque(maxlen=400)
        with subprocess.Popen(
            [str(executable), *gradle_args],
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                tail.append(line)
            returncode = process.wait()
        if returncode == 0:
            return 0
        if attempt >= max_attempts or not is_transient_failure("".join(tail)):
            return returncode
        delay = delays[min(attempt - 1, len(delays) - 1)] if delays else 0.0
        print(
            f"[ci_gradle] transient dependency download failure; retrying in {delay:.1f}s "
            f"(attempt {attempt + 1}/{max_attempts})",
            file=sys.stderr,
            flush=True,
        )
        if delay > 0:
            time.sleep(delay)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--backoff-seconds", default=",".join(str(x) for x in DEFAULT_BACKOFF_SECONDS))
    parser.add_argument("gradle_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    gradle_args = list(args.gradle_args)
    if gradle_args[:1] == ["--"]:
        gradle_args = gradle_args[1:]
    try:
        backoff = parse_backoff(args.backoff_seconds)
        return run_gradle_with_retries(
            workdir=args.workdir,
            gradle_args=gradle_args,
            max_attempts=args.max_attempts,
            backoff_seconds=backoff,
        )
    except (OSError, ValueError) as exc:
        print(f"ci_gradle error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
