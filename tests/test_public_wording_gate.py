"""The sentence on the origin of the component names appears only where it
belongs: once in English and once in German on the page of the system
components, at most once in the changelog and nowhere else.

Scanned: the changelog, the README, the documentation and every text file of
the integration and of the tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_PAGE = ROOT / "docs" / "system-components.md"
CHANGELOG = ROOT / "CHANGELOG.md"
_TEXT_SUFFIXES = {
    ".md",
    ".json",
    ".py",
    ".js",
    ".cjs",
    ".mjs",
    ".yaml",
    ".yml",
    ".txt",
    ".toml",
    ".html",
    ".css",
    "",
}
_SKIPPED_DIRS = {"__pycache__", "node_modules"}

# One match per sentence: the English and the German form with their usual
# variants. The singular ("make the change in the manufacturer app") names
# where a system is operated and stays allowed everywhere.
ORIGIN = re.compile(
    r"(?:as\s+used\s+in\s+the\s+)?manufacturer(?:'s)?\s+apps\b"
    r"|as\s+used\s+in\s+the\s+apps?\b"
    r"|(?:wie\s+(?:sie\s+)?in\s+den\s+)?hersteller-?\s*apps\b(?:\s+verwendet)?"
    r"|wie\s+(?:sie\s+)?in\s+den\s+apps\b",
    re.IGNORECASE,
)


def _public_files() -> list[Path]:
    files = [CHANGELOG, *sorted(ROOT.glob("README*")), ROOT / "info.md"]
    for folder in ("docs", "custom_components", "tests"):
        files += sorted(
            path
            for path in (ROOT / folder).rglob("*")
            if path.is_file()
            and path.suffix in _TEXT_SUFFIXES
            and not _SKIPPED_DIRS & set(path.parts)
        )
    return [path for path in files if path.is_file()]


def _text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _hits(text: str) -> list[str]:
    return [match.group(0) for match in ORIGIN.finditer(text)]


@pytest.fixture(scope="module")
def public_texts() -> dict[Path, str]:
    texts = {path: _text(path) for path in _public_files()}
    assert COMPONENT_PAGE in texts
    assert (
        ROOT / "custom_components" / "bosch_buderus_heating" / "system_components.json"
        in texts
    )
    return texts


def test_the_origin_sentence_only_on_the_component_page(public_texts) -> None:
    found = {
        str(path.relative_to(ROOT)): hits
        for path, text in public_texts.items()
        if path not in (COMPONENT_PAGE, CHANGELOG) and (hits := _hits(text))
    }
    assert found == {}


def test_the_changelog_has_the_origin_sentence_at_most_once(public_texts) -> None:
    assert len(_hits(public_texts[CHANGELOG])) <= 1


def test_the_component_page_has_it_once_per_language() -> None:
    english, separator, german = _text(COMPONENT_PAGE).partition("## Deutsch ")
    assert separator
    assert len(_hits(english)) == 1
    assert len(_hits(german)) == 1
