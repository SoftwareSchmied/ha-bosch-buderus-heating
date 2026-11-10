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
    rrc,
    sensor,
    water_softener,
)

PACKAGE = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
TRANSLATIONS = PACKAGE / "translations"
DIALOGS = ("controls", "holiday")
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
    pairs.extend(rrc._LABELS.values())
    pairs.extend(rrc._CONTROL_LABELS.values())
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
    # Every language with built texts also has Home Assistant translations;
    # a language may have only the latter and then builds English texts.
    assert {"en", "de"} <= set(i18n.LANGUAGES) <= set(files)
    english = json.loads(files["en"].read_text("utf-8"))
    assert english == json.loads((PACKAGE / "strings.json").read_text("utf-8"))
    for language, path in files.items():
        texts = dict(_flat(json.loads(path.read_text("utf-8"))))
        assert list(texts) == list(source), language
        for key, text in texts.items():
            assert text.strip(), (language, key)
            assert _markup(text) == _markup(source[key]), (language, key)


def _data_files() -> dict[str, dict[str, object]]:
    return {
        path.stem: json.loads(path.read_text("utf-8"))
        for path in sorted((PACKAGE / "i18n").glob("*.json"))
    }


def test_data_files_translate_every_built_text() -> None:
    source = code_texts()
    assert len(source) > 400
    data = _data_files()
    assert set(data) == set(i18n.LANGUAGES)
    assert set(i18n.CATALOGS) == set(data) - set(i18n.SOURCE_LANGUAGES)
    for language, content in data.items():
        if language in i18n.SOURCE_LANGUAGES:
            # Built German and English texts stay next to the code.
            assert set(content) == {"dialogs"}, language
            continue
        assert set(content) == {"dialogs", "texts"}, language
        catalog = content["texts"]
        assert isinstance(catalog, dict)
        assert set(catalog) == set(source), language
        for english, text in catalog.items():
            assert text.strip(), (language, english)
            assert _markup(text) == _markup(english), (language, english)
    # German is kept next to the code; it must use the same placeholders.
    for english, german in source.items():
        assert _markup(german) == _markup(english), english


@pytest.mark.parametrize("name", DIALOGS)
def test_dialog_texts_have_the_same_keys_in_every_language(name: str) -> None:
    texts = i18n.dialog_config(name)["texts"]
    assert isinstance(texts, dict)
    assert list(texts) == sorted(i18n.LANGUAGES)
    for language, entries in texts.items():
        assert list(entries) == list(texts["en"]), language
        for key, text in entries.items():
            assert text.strip(), (language, key)
            assert _markup(text) == _markup(texts["en"][key]), (language, key)
    # Every key the dialog script uses has a text.
    script = (PACKAGE / "www" / f"{name}-dialog.js").read_text("utf-8")
    used = set(re.findall(r'(?:_text|holidayText)\([^"]*"(\w+)"', script))
    assert used == set(texts["en"])


def test_code_has_no_branch_for_a_single_language() -> None:
    """A new language needs data files only, no code change."""
    others = {code.casefold() for code in i18n.LANGUAGES} - {"en", "de"}
    assert others
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Codes are lower case; "Fr" is the abbreviation of Friday.
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        if path == PACKAGE / "i18n" / "__init__.py":
            literals -= {"no", "nn", "nb"}  # the documented aliases
        assert not literals & others, path
    for name in DIALOGS:
        script = (PACKAGE / "www" / f"{name}-dialog.js").read_text("utf-8")
        assert not {code for code in others if f'"{code}"' in script}, name


def test_a_new_language_needs_only_its_data_file(tmp_path: Path) -> None:
    for path in (PACKAGE / "i18n").glob("*.json"):
        (tmp_path / path.name).write_text(path.read_text("utf-8"), "utf-8")
    (tmp_path / "nl.json").write_text(
        json.dumps(
            {
                "dialogs": {"holiday": {"date": "Datum"}},
                "texts": {"Heat pump": "Warmtepomp"},
            }
        ),
        "utf-8",
    )
    catalogs = i18n.load_catalogs(tmp_path)
    assert catalogs.languages == ("en", "de", *sorted({*i18n.LANGUAGES[2:], "nl"}))
    assert catalogs.texts["nl"]["Heat pump"] == "Warmtepomp"
    assert catalogs.dialogs["nl"]["holiday"]["date"] == "Datum"
    assert i18n.resolve_language("nl-BE", catalogs.languages) == "nl"


@pytest.mark.parametrize(
    ("tag", "languages", "code"),
    [
        ("pt-BR", ("en", "de", "pt-BR"), "pt-BR"),
        ("pt_br", ("en", "de", "pt-BR"), "pt-BR"),
        ("pt-BR", ("en", "de", "pt"), "pt"),
        ("pt-PT", ("en", "de", "pt-BR"), "en"),
        ("zh-hans", ("en", "de", "zh-Hans"), "zh-Hans"),
    ],
)
def test_regional_tags_find_the_closest_language(
    tag: str, languages: tuple[str, ...], code: str
) -> None:
    assert i18n.resolve_language(tag, languages) == code


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
        ("nn", "nb"),
        ("fi", "fi"),
        ("da", "da"),
        ("pt-BR", "pt"),
        ("pt-PT", "pt"),
        ("eo", "en"),
        ("", "en"),
        (None, "en"),
    ],
)
def test_language_code(tag: str | None, code: str) -> None:
    assert i18n.language_code(tag) == code


def test_localize_falls_back_to_english() -> None:
    assert i18n.localize("de-AT", "Wärmepumpe", "Heat pump") == "Wärmepumpe"
    assert i18n.localize("en", "Wärmepumpe", "Heat pump") == "Heat pump"
    assert i18n.localize("eo", "Wärmepumpe", "Heat pump") == "Heat pump"
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
        ("eo", "/heatingCircuits/hc1/actualSupplyTemperature", "Supply temperature"),
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


# The German and English names as they were before the data files existed.
_RRC_NAMES = {
    "temperatureActual": (
        ("Raumtemperatur", "Room temperature"),
        ("Raumtemperatur einstellen", "Set room temperature"),
    ),
    "temperatureHeatingSetpoint": (
        ("Aktiver Heizsollwert", "Active heating setpoint"),
        ("Aktiver Heizsollwert einstellen", "Set active heating setpoint"),
    ),
    "manualTemperatureHeating": (
        ("Manueller Heizsollwert", "Manual heating setpoint"),
        ("Manueller Heizsollwert einstellen", "Set manual heating setpoint"),
    ),
    "clockOverride/temperatureHeating": (
        ("Vorübergehender Heizsollwert", "Temporary heating setpoint"),
        ("Vorübergehender Heizsollwert einstellen", "Set temporary heating setpoint"),
    ),
    "userMode": (
        ("Heizungsbetriebsart", "Heating mode"),
        ("Heizungsbetriebsart einstellen", "Set heating mode"),
    ),
    "clockProgram": (
        ("Gewähltes Zeitprogramm", "Selected heating schedule"),
        ("Gewähltes Zeitprogramm einstellen", "Set selected heating schedule"),
    ),
}


@pytest.mark.parametrize("field", sorted(_RRC_NAMES))
def test_rrc_zone_names_in_every_language(field: str) -> None:
    path = f"/zones/zn2/{field}"
    for control, (german, english) in enumerate(_RRC_NAMES[field]):
        names = {
            language: rrc.name(path, language, control=bool(control))
            for language in ("de", "DE-ch", "en", "en-GB", "eo", *i18n.CATALOGS)
        }
        assert names["de"] == names["DE-ch"] == german
        assert names["en"] == names["en-GB"] == names["eo"] == english
        for language in i18n.CATALOGS:
            assert names[language] == i18n.CATALOGS[language][english]
            assert names[language] not in {german, english}, language
    assert rrc.name("/zones/znx/userMode", "sv") is None
    assert rrc.name("/zones/zn2/unknown", "sv", control=True) is None


def test_local_only_names_in_a_new_language() -> None:
    path = "/heatSources/externalInputs/input2/state"
    definition = local_only.definition_for(path)
    assert definition is not None
    assert local_only.entity_name(definition, path, "de") == "Externer Eingang 2"
    assert local_only.entity_name(definition, path, "fi") == "Ulkoinen tulo 2"
    assert local_only.entity_name(definition, path, "eo") == "External input 2"


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
