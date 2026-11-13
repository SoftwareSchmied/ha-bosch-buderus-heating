"""Corrected names of 0.9.1: what the readings measure, IDs unchanged.

- The heat sources' supply temperature (``/heatSources/actualSupplyTemperature``)
  is the system supply ("system supply flow temperature" in the API
  documentation). On the reference heat pump it is the heating side's flow
  behind the buffer cylinder; the heat pump's own flow is TC1. A heating
  circuit's supply temperature keeps its name.
- TR3 and TR4 are liquid line temperatures ("liquid pipe heating/cooling"
  in the API documentation), not the condensing temperature (JR1).
- The hot water levels keep their level names, which match the mode labels:
  ``eco`` is Eco+, ``low`` is Eco, ``high`` is Comfort. Their temperatures
  are the start thresholds of a charge, and the ``off`` level's value is its
  setpoint; the names say so.

Only names change: unique IDs, translation keys, entity IDs of existing
entities and the default activation stay.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.bosch_buderus_heating.control_presentation import (
    control_label,
)
from custom_components.bosch_buderus_heating.i18n import LANGUAGES
from custom_components.bosch_buderus_heating.local_only import (
    definition_for,
    entity_name,
)
from custom_components.bosch_buderus_heating.local_only_sensor import (
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.pointt import Resource
from custom_components.bosch_buderus_heating.pointt.models import ResourceMetadata
from custom_components.bosch_buderus_heating.resource_catalog import resource_name
from custom_components.bosch_buderus_heating.sensor import sensor_unique_suffix
from tests.ha_test_helpers import add_without_device

COMPONENT = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
SUPPLY = "/heatSources/actualSupplyTemperature"
CIRCUIT_SUPPLY = "/heatingCircuits/hc1/actualSupplyTemperature"
TR3 = "/heatSources/hs1/refrigerant/liquidPipeHeatingTemp"
TR4 = "/heatSources/hs1/refrigerant/liquidPipeCoolingTemp"
OLD_ENGLISH = (
    "Condenser temperature heating (TR3)",
    "Condenser temperature cooling (TR4)",
    "Eco+ start temperature",
    "Eco start temperature",
    "Comfort start temperature",
)
NEW_ENGLISH = (
    "System supply temperature",
    "Liquid line temperature heating (TR3)",
    "Liquid line temperature cooling (TR4)",
    "Start threshold Eco+",
    "Start threshold Eco",
    "Start threshold Comfort",
    "Setpoint level off",
)
OTHER_LANGUAGES = [code for code in LANGUAGES if code not in ("en", "de")]


def _catalog(code: str) -> dict[str, str]:
    path = COMPONENT / "i18n" / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8"))["texts"]


# ---------------------------------------------------------------- the names


def test_the_heat_sources_supply_is_the_system_supply() -> None:
    assert resource_name(SUPPLY, language="en") == "System supply temperature"
    assert resource_name(SUPPLY, language="de") == "Vorlauftemperatur Anlage"
    # The supply temperature of a heating circuit is that circuit's own.
    assert resource_name(CIRCUIT_SUPPLY, language="en") == "Supply temperature"
    assert resource_name(CIRCUIT_SUPPLY, language="de") == "Vorlauftemperatur"


def test_the_unused_translation_of_the_legacy_key_says_the_same() -> None:
    """The legacy key ``supply_temperature`` names nothing itself, but its
    texts must not contradict the name the entity shows."""
    for name, language in (
        ("strings.json", "en"),
        ("translations/en.json", "en"),
        ("translations/de.json", "de"),
    ):
        texts = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        shown = texts["entity"]["sensor"]["supply_temperature"]["name"]
        assert shown == resource_name(SUPPLY, language=language)


@pytest.mark.parametrize(
    ("path", "names"),
    [
        (
            TR3,
            (
                "Temperatur Flüssigkeitsleitung Heizen (TR3)",
                "Liquid line temperature heating (TR3)",
            ),
        ),
        (
            TR4,
            (
                "Temperatur Flüssigkeitsleitung Kühlen (TR4)",
                "Liquid line temperature cooling (TR4)",
            ),
        ),
    ],
)
def test_tr3_and_tr4_are_liquid_line_temperatures(path, names) -> None:
    definition = definition_for(path)
    assert definition is not None
    assert entity_name(definition, path, "de") == names[0]
    assert entity_name(definition, path, "en") == names[1]


@pytest.mark.parametrize("code", OTHER_LANGUAGES)
def test_every_language_has_the_corrected_names(code) -> None:
    texts = _catalog(code)
    for english in NEW_ENGLISH:
        assert texts.get(english), (code, english)
        assert texts[english] != english, (code, english)
    for english in OLD_ENGLISH:
        assert english not in texts, (code, english)
    assert resource_name(SUPPLY, language=code) == texts[NEW_ENGLISH[0]]
    assert entity_name(definition_for(TR3), TR3, code) == texts[NEW_ENGLISH[1]]


@pytest.mark.parametrize(
    ("level", "english", "german"),
    [
        ("eco", "Start threshold Eco+", "Startschwelle Eco+"),
        ("low", "Start threshold Eco", "Startschwelle Eco"),
        ("high", "Start threshold Comfort", "Startschwelle Komfort"),
    ],
)
def test_the_hot_water_levels_name_their_start_thresholds(
    level, english, german
) -> None:
    path = f"/dhwCircuits/dhw1/temperatureLevels/{level}"
    assert resource_name(path, language="en") == english
    assert resource_name(path, language="de") == german
    assert control_label(path, "fallback", "en") == english
    assert control_label(path, "fallback", "de") == german


def test_the_off_level_names_its_setpoint() -> None:
    path = "/dhwCircuits/dhw1/temperatureLevels/off"
    assert resource_name(path, language="en") == "Setpoint level off"
    assert resource_name(path, language="de") == "Sollwert Stufe Aus"


def test_the_number_controls_name_the_start_thresholds() -> None:
    for name, labels in (
        ("translations/en.json", ("Start threshold Eco+", "Start threshold Eco")),
        ("translations/de.json", ("Startschwelle Eco+", "Startschwelle Eco")),
    ):
        numbers = json.loads((COMPONENT / name).read_text(encoding="utf-8"))["entity"][
            "number"
        ]
        assert (
            numbers["hot_water_eco_plus"]["name"],
            numbers["hot_water_eco"]["name"],
        ) == labels
        assert "threshold" in numbers["hot_water_comfort"]["name"].lower() or (
            numbers["hot_water_comfort"]["name"] == "Startschwelle Komfort"
        )


def test_the_hot_water_mode_labels_match_the_levels() -> None:
    for name, language, labels in (
        ("translations/en.json", "en", ("Eco", "Eco+", "Comfort")),
        ("translations/de.json", "de", ("Eco", "Eco+", "Komfort")),
    ):
        texts = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        for platform in ("select", "sensor"):
            state = texts["entity"][platform]["hot_water_operation_mode"]["state"]
            assert (state["low"], state["eco"], state["high"]) == labels, language


# ------------------------------------------------------- identities stay


def test_identities_and_defaults_stay() -> None:
    tr3, tr4 = definition_for(TR3), definition_for(TR4)
    assert (tr3.key, tr3.enabled) == ("liquid_line_heating", True)
    assert (tr4.key, tr4.enabled) == ("liquid_line_cooling", False)
    assert local_only_unique_id("gw", TR3) == (
        "gw:local_only:heatSources:hs1:refrigerant:liquidPipeHeatingTemp"
    )
    assert local_only_unique_id("gw", TR4) == (
        "gw:local_only:heatSources:hs1:refrigerant:liquidPipeCoolingTemp"
    )


def _supply_sensor(hass: HomeAssistant):
    from tests.test_sensor import _sensor

    resource = Resource(
        path=SUPPLY,
        value=26.2,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )
    return _sensor(hass, resource)


async def test_an_existing_supply_entity_keeps_its_entity_id(
    hass: HomeAssistant,
) -> None:
    """Registered under the former name, the entity keeps its ID and unique ID
    and only shows the corrected name."""
    assert await async_setup_component(hass, "sensor", {})
    sensor = _supply_sensor(hass)
    assert (
        sensor_unique_suffix(sensor.entity_description) == "gateway:supply_temperature"
    )
    assert sensor.unique_id == "gateway-one:gateway:supply_temperature"
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        "sensor",
        sensor.unique_id,
        suggested_object_id="k40_heat_pump_supply_temperature",
        original_name="Heat pump \N{EN DASH} Supply temperature",
    )
    await add_without_device(hass, sensor)
    await hass.async_block_till_done()
    assert sensor.entity_id == old.entity_id
    assert old.entity_id == "sensor.k40_heat_pump_supply_temperature"
    entry = registry.async_get(old.entity_id)
    assert entry is not None
    assert entry.unique_id == old.unique_id
    assert entry.original_name.endswith("System supply temperature")
    assert hass.states.get(old.entity_id).state == "26.2"


async def test_an_existing_tr3_entity_keeps_its_entity_id(
    hass: HomeAssistant,
) -> None:
    from tests.test_c7_signals import entity
    from tests.test_local_only import number

    assert await async_setup_component(hass, "sensor", {})
    sensor = entity(hass, TR3, [number(TR3, 24.2)])
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        "sensor",
        sensor.unique_id,
        suggested_object_id="mx400_heat_pump_liquid_line_heating",
        original_name="Heat pump \N{EN DASH} Condenser temperature heating (TR3)",
    )
    await add_without_device(hass, sensor)
    await hass.async_block_till_done()
    assert sensor.entity_id == "sensor.mx400_heat_pump_liquid_line_heating"
    entry = registry.async_get(old.entity_id)
    assert entry is not None
    assert entry.unique_id == local_only_unique_id("gateway-one", TR3)
    assert entry.original_name.endswith("Liquid line temperature heating (TR3)")


async def test_an_existing_hot_water_level_keeps_its_entity_id(
    hass: HomeAssistant,
) -> None:
    from tests.test_sensor import _sensor

    assert await async_setup_component(hass, "sensor", {})
    sensor = _sensor(
        hass,
        Resource(
            path="/dhwCircuits/dhw1/temperatureLevels/eco",
            value=40.0,
            has_value=True,
            metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
        ),
    )
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        "sensor",
        sensor.unique_id,
        suggested_object_id="k40_hot_water_eco_start_temperature",
        original_name="Hot water \N{EN DASH} Eco+ start temperature",
    )
    await add_without_device(hass, sensor)
    await hass.async_block_till_done()
    assert sensor.entity_id == "sensor.k40_hot_water_eco_start_temperature"
    entry = registry.async_get(old.entity_id)
    assert entry is not None
    assert entry.unique_id == old.unique_id
    assert entry.original_name.endswith("Start threshold Eco+")
    assert hass.states.get(old.entity_id).state == "40.0"
