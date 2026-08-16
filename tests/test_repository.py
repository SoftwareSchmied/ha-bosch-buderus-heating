"""Tests for repository metadata and the Phase 0 package."""

from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "bosch_buderus_heating"


def load_json(path: Path) -> dict[str, object]:
    """Load a JSON object from the repository."""
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_stable_project_identity() -> None:
    """The public identifiers must remain aligned."""
    manifest = load_json(INTEGRATION / "manifest.json")
    hacs = load_json(ROOT / "hacs.json")
    strings = load_json(INTEGRATION / "strings.json")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert manifest["domain"] == "bosch_buderus_heating"
    assert manifest["name"] == "Bosch/Buderus Heating"
    assert manifest["version"] == "0.1.0"
    assert project["project"]["version"] == manifest["version"]
    assert manifest["requirements"] == []
    assert manifest["config_flow"] is True
    assert hacs["name"] == manifest["name"]
    assert hacs["zip_release"] is True
    assert hacs["filename"] == "bosch_buderus_heating.zip"
    assert strings["title"] == manifest["name"]


@pytest.mark.parametrize("language", ["de", "en"])
def test_translation_title(language: str) -> None:
    """German and English translation catalogs expose the stable title."""
    translation = load_json(INTEGRATION / "translations" / f"{language}.json")
    assert translation["title"] == "Bosch/Buderus Heating"


def test_brand_icon() -> None:
    """HACS receives an original square PNG brand icon."""
    icon = (INTEGRATION / "brand" / "icon.png").read_bytes()
    source = ET.parse(ROOT / "docs" / "assets" / "icon.svg")

    assert icon.startswith(b"\x89PNG\r\n\x1a\n")
    assert int.from_bytes(icon[16:20], "big") == 256
    assert int.from_bytes(icon[20:24], "big") == 256
    assert source.getroot().tag.endswith("svg")


@pytest.mark.parametrize(
    "path",
    [
        INTEGRATION / "strings.json",
        INTEGRATION / "translations" / "de.json",
        INTEGRATION / "translations" / "en.json",
    ],
)
def test_state_translation_keys_are_home_assistant_safe(path: Path) -> None:
    """State translation keys must satisfy hassfest's identifier grammar."""
    invalid: list[str] = []

    def visit(value: object, keys: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            if keys and keys[-1] == "state":
                invalid.extend(
                    key
                    for key in value
                    if re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", key) is None
                )
            for key, child in value.items():
                visit(child, (*keys, key))
        elif isinstance(value, list):
            for child in value:
                visit(child, keys)

    visit(load_json(path))

    assert invalid == []


@pytest.mark.parametrize(
    "path",
    [
        INTEGRATION / "strings.json",
        INTEGRATION / "translations" / "de.json",
        INTEGRATION / "translations" / "en.json",
    ],
)
def test_fixable_issue_translations_use_only_fix_flow(path: Path) -> None:
    """Hassfest treats issue descriptions and fix flows as alternatives."""
    issues = load_json(path)["issues"]

    assert isinstance(issues, dict)
    for issue in issues.values():
        assert isinstance(issue, dict)
        assert not ({"description", "fix_flow"} <= issue.keys())
