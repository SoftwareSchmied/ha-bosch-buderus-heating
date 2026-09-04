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
    assert manifest["version"] == "0.7.0-beta.2"
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


def test_heat_source_status_translations_match_vendor_app_terms() -> None:
    """Operational status labels follow the MyBuderus/HomeCom terminology."""
    german = load_json(INTEGRATION / "translations" / "de.json")
    sensor = german["entity"]["sensor"]

    assert sensor["compressor_status"]["name"] == "Status Kompressor"
    assert sensor["compressor_status"]["state"]["alarm"] == "Blockiert"
    assert sensor["compressor_status"]["state"]["cooling"] == "Kühlung Zuhause"
    assert sensor["electric_auxiliary_heater_status"]["state"]["defrost"] == ("Abtauen")


def test_heat_source_type_translations_are_complete() -> None:
    """Canonical heat-source types remain explicit in both UI languages."""
    source = load_json(INTEGRATION / "strings.json")["entity"]["sensor"]
    german = load_json(INTEGRATION / "translations" / "de.json")["entity"]["sensor"]
    english = load_json(INTEGRATION / "translations" / "en.json")["entity"]["sensor"]

    assert source["heat_source_type"]["state"] == {
        "heatpump": "Heat pump",
        "boiler": "Boiler",
        "hybrid": "Hybrid system",
    }
    assert english["heat_source_type"]["state"] == source["heat_source_type"]["state"]
    assert german["heat_source_type"]["state"] == {
        "heatpump": "Wärmepumpe",
        "boiler": "Heizkessel",
        "hybrid": "Hybridsystem",
    }


def test_schedule_type_translations_match_vendor_app_terms() -> None:
    """Schedule types use the terms found in both official app variants."""
    source = load_json(INTEGRATION / "strings.json")["entity"]["sensor"]
    german = load_json(INTEGRATION / "translations" / "de.json")["entity"]["sensor"]
    english = load_json(INTEGRATION / "translations" / "en.json")["entity"]["sensor"]

    assert source["heating_circuit_switch_program_mode"]["state"] == {
        "level": "Temperature level",
        "absolute": "Freely Adjustable Temperatures",
    }
    assert (
        english["heating_circuit_switch_program_mode"]["state"]
        == source["heating_circuit_switch_program_mode"]["state"]
    )
    assert german["heating_circuit_switch_program_mode"]["state"] == {
        "level": "Temperaturniveau",
        "absolute": "Frei einstellbare Temperaturen",
    }


def test_holiday_translations_are_clear_and_match_vendor_terms() -> None:
    """Holiday labels stay consistent with the official app terminology."""
    german = load_json(INTEGRATION / "translations" / "de.json")["entity"]
    english = load_json(INTEGRATION / "translations" / "en.json")["entity"]

    assert german["binary_sensor"]["holiday_active"]["name"] == ("Urlaubsmodus aktiv")
    assert german["sensor"]["next_holiday"]["name"] == "Nächster Urlaub"
    assert german["calendar"]["holiday_periods"]["name"] == "Urlaubszeiten"
    assert english["binary_sensor"]["holiday_active"]["name"] == ("Holiday mode active")
    assert english["sensor"]["next_holiday"]["name"] == "Next holiday"
    assert english["calendar"]["holiday_periods"]["name"] == "Holiday periods"


def test_holiday_options_are_translated_and_structurally_aligned() -> None:
    """The dynamic holiday dialog remains complete in both supported languages."""
    source = load_json(INTEGRATION / "strings.json")
    german = load_json(INTEGRATION / "translations" / "de.json")
    english = load_json(INTEGRATION / "translations" / "en.json")

    assert source["options"].keys() == german["options"].keys()
    assert source["options"].keys() == english["options"].keys()
    assert german["selector"]["holiday_heating_mode"]["options"] == {
        "saturday": "Wie Samstag",
        "fix_temperature": "Konstante Temperatur",
        "off": "AUS",
        "eco": "Absenken",
    }
    assert german["selector"]["holiday_dhw_mode"]["options"]["off_td"] == (
        "AUS bei thermischer Desinfektion"
    )
    assert german["options"]["step"]["holiday"]["data"] == {
        "holiday_assigned_to": "Anwenden auf",
        "holiday_heating_mode": "Heizung",
        "holiday_dhw_mode": "Warmwasser",
        "holiday_ventilation_mode": "Lüftung",
        "holiday_thermal_disinfection": "Thermische Desinfektion",
        "holiday_fix_temperature": "Konstante Temperatur",
    }
    for translations in (source, german, english):
        for selector in translations["selector"].values():
            assert all(
                re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", option)
                for option in selector["options"]
            )


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
