"""Every language has the same texts, placeholders and links as English."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from custom_components.bosch_buderus_heating import (
    connection_status,
    control_presentation,
    faults,
    i18n,
    local_only,
    notifications,
    resource_catalog,
    sensor,
    water_softener,
)

PACKAGE = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
TRANSLATIONS = PACKAGE / "translations"
DIALOGS = ("controls-dialog.js", "holiday-dialog.js")
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
_LINK = re.compile(r"\]\(([^)]*)\)")
_HTML = re.compile(r"</?[a-zA-Z][^>]*>")


def _flat(data: dict[str, object], prefix: str = "") -> Iterator[tuple[str, str]]:
    for key, value in data.items():
        if isinstance(value, dict):
            yield from _flat(value, f"{prefix}{key}.")
        else:
            assert isinstance(value, str), prefix + key
            yield prefix + key, value


def _markup(text: str) -> tuple[list[str], list[str], list[str]]:
    return (
        sorted(_PLACEHOLDER.findall(text)),
        sorted(_LINK.findall(text)),
        sorted(_HTML.findall(text)),
    )


def _literal_pairs() -> Iterator[tuple[str, str]]:
    """German and English literals of every ``localize`` call in the package."""
    for path in PACKAGE.rglob("*.py"):
        if "i18n" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "localize" or len(node.args) != 3:
                continue
            german, english = node.args[1], node.args[2]
            if isinstance(german, ast.Constant) and isinstance(english, ast.Constant):
                yield german.value, english.value


def _local_only_definitions() -> Iterator[local_only.LocalOnlyDefinition]:
    seen: set[int] = set()
    pending: list[object] = list(vars(local_only).values())
    while pending:
        item = pending.pop()
        if isinstance(item, local_only.LocalOnlyDefinition):
            if id(item) not in seen:
                seen.add(id(item))
                yield item
                pending.append(item.alternate)
        elif isinstance(item, tuple | list):
            pending.extend(item)
        elif isinstance(item, dict):
            pending.extend(item.values())


def code_texts() -> dict[str, str]:
    """English source texts the integration builds itself, with their German."""
    pairs: list[tuple[str, str]] = list(_literal_pairs())
    catalog = resource_catalog
    for german, english in (
        (catalog._GERMAN_NAMES, catalog._ENGLISH_NAMES),
        (catalog._GERMAN_PATH_NAMES, catalog._ENGLISH_PATH_NAMES),
        (catalog._SUBKEY_NAMES, catalog._ENGLISH_SUBKEY_NAMES),
        (notifications._TEXT["de"], notifications._TEXT["en"]),
        (connection_status._TEXTS["de"], connection_status._TEXTS["en"]),
        (sensor._SOURCE_SUFFIX["de"], sensor._SOURCE_SUFFIX["en"]),
    ):
        assert set(german) == set(english)
        pairs.extend((german[key], english[key]) for key in english)
    pairs.extend(catalog._APP_PATH_NAMES.values())
    pairs.extend(control_presentation._LABELS.values())
    pairs.extend(
        (texts["de"], texts["en"])
        for table in (faults._KNOWN_SUMMARIES, faults._SEVERITY_LABELS)
        for texts in table.values()
    )
    pairs.extend(
        (item.name_de, item.name_en) for item in water_softener.SOFTENER_CAPABILITIES
    )
    pairs.extend(
        definition.names
        for definition in _local_only_definitions()
        if definition.key != "spec"
    )
    return {english: german for german, english in pairs}


def test_translation_files_have_the_source_keys_and_markup() -> None:
    source = dict(_flat(json.loads((PACKAGE / "strings.json").read_text("utf-8"))))
    files = {path.stem: path for path in TRANSLATIONS.glob("*.json")}
    assert set(files) == set(i18n.LANGUAGES)
    english = json.loads(files["en"].read_text("utf-8"))
    assert english == json.loads((PACKAGE / "strings.json").read_text("utf-8"))
    for language, path in files.items():
        texts = dict(_flat(json.loads(path.read_text("utf-8"))))
        assert list(texts) == list(source), language
        for key, text in texts.items():
            assert text.strip(), (language, key)
            assert _markup(text) == _markup(source[key]), (language, key)


def test_code_catalogs_translate_every_built_text() -> None:
    source = code_texts()
    assert len(source) > 400
    assert set(i18n.CATALOGS) == set(i18n.LANGUAGES) - {"de", "en"}
    for language, catalog in i18n.CATALOGS.items():
        assert set(catalog) == set(source), language
        for english, text in catalog.items():
            assert text.strip(), (language, english)
            assert _markup(text) == _markup(english), (language, english)
    # German is kept next to the code; it must use the same placeholders.
    for english, german in source.items():
        assert _markup(german) == _markup(english), english


def _dialog_texts(name: str) -> dict[str, dict[str, str]]:
    script = (PACKAGE / "www" / name).read_text("utf-8")
    match = re.search(
        r"^const [A-Z]+_TEXTS = (\{.*?\n\});$", script, re.MULTILINE | re.DOTALL
    )
    assert match is not None, name
    return json.loads(match.group(1))


@pytest.mark.parametrize("name", DIALOGS)
def test_dialog_dictionaries_have_the_same_keys(name: str) -> None:
    texts = _dialog_texts(name)
    assert list(texts) == list(i18n.LANGUAGES)
    for language, entries in texts.items():
        assert list(entries) == list(texts["en"]), language
        for key, text in entries.items():
            assert text.strip(), (language, key)
            assert _markup(text) == _markup(texts["en"][key]), (language, key)


@pytest.mark.parametrize(
    ("tag", "code"),
    [
        ("de", "de"),
        ("de-CH", "de"),
        ("DE", "de"),
        ("en-GB", "en"),
        ("sv", "sv"),
        ("sv_FI", "sv"),
        ("nb", "nb"),
        ("no", "nb"),
        ("nb-NO", "nb"),
        ("nn", "en"),
        ("fi", "fi"),
        ("da", "da"),
        ("pt-BR", "en"),
        ("fr", "en"),
        ("", "en"),
        (None, "en"),
    ],
)
def test_language_code(tag: str | None, code: str) -> None:
    assert i18n.language_code(tag) == code


def test_localize_falls_back_to_english() -> None:
    assert i18n.localize("de-AT", "Wärmepumpe", "Heat pump") == "Wärmepumpe"
    assert i18n.localize("en", "Wärmepumpe", "Heat pump") == "Heat pump"
    assert i18n.localize("fr", "Wärmepumpe", "Heat pump") == "Heat pump"
    assert i18n.localize("sv", "Wärmepumpe", "Heat pump") == "Värmepump"
    assert i18n.localize("no", "Wärmepumpe", "Heat pump") == "Varmepumpe"
    assert i18n.localize("fi", "Wärmepumpe", "Heat pump") == "Lämpöpumppu"
    assert i18n.localize("da", "Wärmepumpe", "Heat pump") == "Varmepumpe"
    # A text without a translation yet stays English.
    assert i18n.localize("sv", "Neu", "Not in any catalog") == "Not in any catalog"
    assert i18n.localize_all("de", {"a": "ein"}, {"a": "on", "b": "off"}) == {
        "a": "ein",
        "b": "off",
    }
    assert i18n.localize_all("da", {"a": "ein"}, {"a": "on", "b": "off"}) == {
        "a": i18n.CATALOGS["da"]["on"],
        "b": i18n.CATALOGS["da"]["off"],
    }


# ------------------------------------------------ texts in a new language


@pytest.mark.parametrize(
    ("language", "path", "expected"),
    [
        (
            "sv",
            "/heatingCircuits/hc1/actualSupplyTemperature",
            "Framledningstemperatur",
        ),
        ("fi", "/heatingCircuits/hc1/actualSupplyTemperature", "Menolämpötila"),
        (
            "sv",
            "/zones/zone1/heating/temporaryRoomSetpoint",
            "Värme: Tillfälligt börvärde",
        ),
        ("fr", "/heatingCircuits/hc1/actualSupplyTemperature", "Supply temperature"),
    ],
)
def test_resource_names_follow_the_system_language(
    language: str, path: str, expected: str
) -> None:
    assert resource_catalog.resource_name(path, language=language) == expected


def test_group_and_control_names_in_a_new_language(hass) -> None:
    from custom_components.bosch_buderus_heating.control_presentation import (
        control_name,
    )
    from custom_components.bosch_buderus_heating.device import grouped_entity_name
    from custom_components.bosch_buderus_heating.pointt import Resource
    from tests.test_device import _coordinator

    mode = "/heatingCircuits/hc1/operationMode"
    hass.config.language = "sv-SE"
    coordinator = _coordinator(
        hass,
        resources=(
            Resource(mode, "auto", True),
            Resource("/heatSources/hs1/heatPumpType", "air_water", True),
            Resource("/heatSources/hs1/actualModulation", 40, True),
        ),
    )
    assert grouped_entity_name(coordinator, mode, "X") == "Värmekrets 1 \N{EN DASH} X"
    assert (
        grouped_entity_name(coordinator, "/system/brand", "X")
        == "Anläggning \N{EN DASH} X"
    )
    assert (
        grouped_entity_name(coordinator, "/heatSources/hs1/actualModulation", "X")
        == "Värmepump \N{EN DASH} X"
    )
    # The generic group is shortened like "HK 1" and "HC 1".
    assert control_name(coordinator, mode, "X") == "VK 1: Driftläge"
    hass.config.language = "fi"
    assert control_name(coordinator, mode, "X") == "LP 1: Käyttötila"
    hass.config.language = "de"
    assert control_name(coordinator, mode, "X") == "HK 1: Betriebsart"


def test_local_only_names_in_a_new_language() -> None:
    path = "/heatSources/externalInputs/input2/state"
    definition = local_only.definition_for(path)
    assert definition is not None
    assert local_only.entity_name(definition, path, "de") == "Externer Eingang 2"
    assert local_only.entity_name(definition, path, "fi") == "Ulkoinen tulo 2"
    assert local_only.entity_name(definition, path, "pt-BR") == "External input 2"


def test_documented_local_readings_are_named_by_the_shared_catalog() -> None:
    path = "/zones/zone1/heating/temporaryRoomSetpoint"
    definition = local_only.LocalOnlyDefinition(
        re.compile(re.escape(path)), "spec", local_only.GENERIC, False, ("", "")
    )
    # A composed name is translated part by part, not looked up as a whole.
    assert local_only.entity_name(definition, path, "sv") == (
        "Värme: Tillfälligt börvärde"
    )


def test_fault_texts_in_a_new_language() -> None:
    fault = faults.ActiveFault.__new__(faults.ActiveFault)
    object.__setattr__(fault, "code", "4711")
    object.__setattr__(fault, "subcode", None)
    unknown = i18n.CATALOGS["da"]["Unknown fault (code {code})"]
    assert faults.fault_summary(fault, "da") == unknown.format(code="4711")
    assert (
        faults.fault_severity_label(faults.FaultSeverity.WARNING, "nb")
        == (i18n.CATALOGS["nb"]["Warning"])
    )
    assert (
        faults.no_active_faults_label("sv") == i18n.CATALOGS["sv"]["No active faults"]
    )


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("de", "03.10.2026"),
        ("fi", "03.10.2026"),
        ("sv", "2026-10-03"),
        ("en", "2026-10-03"),
    ],
)
async def test_notification_dates_follow_the_language(
    hass, language: str, expected: str
) -> None:
    from datetime import UTC, datetime

    await hass.config.async_set_time_zone("UTC")
    when = datetime(2026, 10, 3, 12, tzinfo=UTC)
    text = notifications.FaultNotifications._time(None, when, language)  # type: ignore[arg-type]
    assert text.startswith(expected + " 12:00:00")
