"""Extract release notes for one version from the project changelog."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
_VERSION_HEADER = re.compile(r"^## \[(?P<version>[^]]+)](?: - .+)?$")
# "below" or "above", but not as a comparison such as "below 0.8.0" or
# "05.04.00 or above".
_DIRECTION = re.compile(
    r"(?<!\bor )(?<!\band )\b(?:below|above)\b(?!\s+(?:version\s+)?v?\d)", re.I
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_VERSION_FAMILY = r"\d+\.\d+\.x"


def _other_release_names(changelog: str, version: str) -> list[str]:
    """Return the names under which the text can mention other sections."""
    names: set[str] = set()
    for line in changelog.splitlines():
        match = _VERSION_HEADER.fullmatch(line)
        if match is None or match.group("version") in {version, "Unreleased"}:
            continue
        other = match.group("version")
        names.add(other)
        _, _, suffix = other.partition("-")
        if suffix:
            names.add(suffix)
    _, _, own_suffix = version.partition("-")
    names.discard(own_suffix)
    return sorted(names, key=len, reverse=True)


def cross_section_references(changelog: str, version: str, notes: str) -> list[str]:
    """Return the sentences of ``notes`` that point to another section.

    The notes of a version are published on their own, so a sentence such as
    "the notes of alpha.1 below apply as well" points to text that the reader
    of the release does not see.
    """
    alternatives = [
        *(re.escape(name) for name in _other_release_names(changelog, version)),
        _VERSION_FAMILY,
    ]
    other_release = re.compile(
        rf"(?<![\w.])(?:{'|'.join(alternatives)})(?!\w|\.\d)", re.I
    )
    text = " ".join(
        line.lstrip("> ").strip() for line in notes.splitlines() if line.strip()
    )
    return [
        sentence
        for sentence in _SENTENCE_END.split(text)
        if _DIRECTION.search(sentence) and other_release.search(sentence)
    ]


def extract_release_notes(changelog: str, version: str) -> str:
    """Return the non-empty, self-contained changelog section of a version."""
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
    references = cross_section_references(changelog, version, notes)
    if references:
        raise ValueError(
            f"CHANGELOG.md section for version {version!r} points to other "
            f"sections, which its release notes do not contain: {references[0]!r}"
        )
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
