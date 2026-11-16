#!/usr/bin/env python3
"""Validate that Linux Kconfig preserved every requested fragment value."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


_SYMBOL = r"CONFIG_[A-Za-z0-9_]+"
_VALUE = r'(?:y|m|n|"(?:\\.|[^"\\])*"|-?[0-9]+|0[xX][0-9a-fA-F]+)'
_ASSIGNMENT = re.compile(rf"^({_SYMBOL})=({_VALUE})$")
_DISABLED = re.compile(rf"^# ({_SYMBOL}) is not set$")


class ConfigValidationError(ValueError):
    """Raised when a fragment is malformed or Kconfig did not preserve it."""


def _parse_config(text: str, label: str, *, fragment: bool) -> dict[str, str]:
    values: dict[str, str] = {}
    errors: list[str] = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            if disabled := _DISABLED.fullmatch(line):
                symbol = disabled.group(1)
                value = "n"
            else:
                continue
        elif match := _ASSIGNMENT.fullmatch(line):
            symbol, value = match.groups()
        else:
            errors.append(f"{label}:{line_number}: malformed assignment: {raw_line!r}")
            continue

        if symbol in values:
            errors.append(f"{label}:{line_number}: duplicate assignment for {symbol}")
            continue
        values[symbol] = value

    if errors:
        raise ConfigValidationError("\n".join(errors))
    return values


def validate_config_text(fragment_text: str, generated_text: str) -> None:
    """Require all explicit fragment assignments to match generated .config."""
    requested = _parse_config(fragment_text, "fragment", fragment=True)
    generated = _parse_config(generated_text, "generated config", fragment=False)
    errors: list[str] = []

    for symbol, expected in requested.items():
        if symbol not in generated:
            errors.append(
                f"{symbol}: missing from generated config (unknown symbol or not emitted by Kconfig); requested {expected}"
            )
        elif generated[symbol] != expected:
            errors.append(
                f"{symbol}: requested {expected}, generated {generated[symbol]}"
            )

    if errors:
        raise ConfigValidationError("\n".join(errors))


def validate_config_files(fragment: Path, generated: Path) -> None:
    try:
        fragment_text = fragment.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigValidationError(f"cannot read fragment {fragment}: {error}") from error
    try:
        generated_text = generated.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigValidationError(f"cannot read generated config {generated}: {error}") from error
    validate_config_text(fragment_text, generated_text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fragment", required=True, type=Path, help="requested kernel config fragment")
    parser.add_argument("--generated", required=True, type=Path, help=".config after olddefconfig")
    args = parser.parse_args(argv)
    try:
        validate_config_files(args.fragment, args.generated)
    except ConfigValidationError as error:
        print(f"kernel config validation failed:\n{error}", file=sys.stderr)
        return 1
    print(f"kernel config matches fragment: {args.fragment}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
