"""Extract release notes for one version from the project changelog."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
_VERSION_HEADER = re.compile(r"^## \[(?P<version>[^]]+)](?: - .+)?$")


def extract_release_notes(changelog: str, version: str) -> str:
    """Return the non-empty changelog section for a released version."""
    lines = changelog.splitlines()
    start: int | None = None
    end = len(lines)

    for index, line in enumerate(lines):
        match = _VERSION_HEADER.fullmatch(line)
        if match is None:
            continue
        if start is None:
            if match.group("version") == version:
                start = index + 1
            continue
        end = index
        break

    if start is None:
        raise ValueError(f"CHANGELOG.md has no section for version {version!r}")
    notes = "\n".join(lines[start:end]).strip()
    if not notes:
        raise ValueError(f"CHANGELOG.md section for version {version!r} is empty")
    return f"{notes}\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--changelog", type=Path, default=CHANGELOG)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    """Write the selected changelog section to the requested output file."""
    args = _parser().parse_args()
    notes = extract_release_notes(
        args.changelog.read_text(encoding="utf-8"), args.version
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
