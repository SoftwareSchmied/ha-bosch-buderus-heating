"""Entity review before 0.9: C3, C5 and C8 (C4 is covered with the start counters)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.device import (
    device_info_for_resource,
    neutral_device_name,
    registered_device,
)
from custom_components.bosch_buderus_heating.local_only import (
    definition_for,
    native_value,
    state_options,
)
from custom_components.bosch_buderus_heating.observations import (
    from_local_observation,
)
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource
from tests.test_device import _coordinator
from tests.test_local_api import read

COMPONENT = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"
REFRIGERANT = "/heatSources/hs1/refrigerant/status"
# As the gateway of the reference heat pump declares it (firmware 15.00.01).
DECLARED = ["off", "heating", "cooling", "dhw", "pool", "pool_heat", "defrost", "alarm"]


def test_the_cylinder_temperature_is_disabled_by_default() -> None:
    definition = definition_for("/heatSources/dhw/cylinderTemperature")
    assert definition is not None and definition.enabled is False


def refrigerant(value, allowed=DECLARED):
    raw = {"id": REFRIGERANT, "type": "stringValue", "writeable": 0, "value": value}
    if allowed is not None:
        raw["allowedValues"] = allowed
    return from_local_observation(
        read(raw), connection_id="local", target_id="plant", current=True
    )


@pytest.mark.parametrize("value", ["off", "dhw", "pool", "pool_heat", "heating"])
def test_declared_refrigerant_states_are_shown(value) -> None:
    definition = definition_for(REFRIGERANT)
    reading = refrigerant(value)
    assert native_value(definition, reading) == value
    assert value in state_options(definition, reading)


def test_states_neither_reviewed_nor_declared_stay_unknown() -> None:
    definition = definition_for(REFRIGERANT)
    # Declared by the device: shown, and offered as an option.
    reading = refrigerant("standby", allowed=[*DECLARED, "standby"])
    assert native_value(definition, reading) == "standby"
    assert state_options(definition, reading)[-1] == "standby"
    # Not declared and not reviewed: unknown.
    assert native_value(definition, refrigerant("standby")) is None
    # Without any declaration the reviewed states still apply.
    assert native_value(definition, refrigerant("dhw", allowed=None)) == "dhw"
    options = state_options(definition, None)
    assert options[:9] == definition.options[:9]
    assert len(options) == len(set(options))


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", {"off": "Off", "dhw": "Hot water", "pool": "Pool"}),
        ("de", {"off": "Aus", "dhw": "Warmwasser", "pool_heat": "Poolheizung"}),
    ],
)
def test_the_new_states_are_translated(language, expected) -> None:
    for name in ("strings.json", f"translations/{language}.json"):
        if name == "strings.json" and language != "en":
            continue
        states = json.loads((COMPONENT / name).read_text(encoding="utf-8"))["entity"][
            "sensor"
        ]["refrigerant_status"]["state"]
        assert set(definition_for(REFRIGERANT).options) <= set(states)
        for key, text in expected.items():
            assert states[key] == text


# --------------------------------------------------------------------- C8


def test_a_new_device_is_named_without_the_brand(hass: HomeAssistant) -> None:
    coordinator = _coordinator(
        hass,
        brand="buderus",
        gateway=Gateway("gateway-one", model="MX400", device_type="k40"),
        resources=(Resource("/system/brand", "Buderus", True),),
    )
    info = device_info_for_resource(coordinator, "/gateway")
    assert info["name"] == "MX400"
    assert info["manufacturer"] == "Buderus"  # device information, not a label


def test_an_existing_device_keeps_its_name(hass: HomeAssistant) -> None:
    coordinator = _coordinator(
        hass,
        gateway=Gateway("gateway-one", model="MX400", device_type="k40"),
        resources=(Resource("/system/brand", "Buderus", True),),
    )
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, "gateway-one")},
        name="Buderus MX400",
    )
    assert registered_device(hass, "gateway-one")
    info = device_info_for_resource(coordinator, "/gateway")
    assert "name" not in info  # Home Assistant keeps the registry name
    assert info["model"] == "MX400"
    assert registry.async_get(device.id).name == "Buderus MX400"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("MX400", "MX400"),
        ("Heating system", "Heating"),
        ("Buderus Logamatic", "Logamatic"),
        ("Bosch/Buderus", "Heating"),
    ],
)
def test_neutral_device_names(model, expected) -> None:
    assert neutral_device_name(model) == expected


def test_the_local_state_sensor_offers_the_declared_states() -> None:
    from types import SimpleNamespace

    from custom_components.bosch_buderus_heating.local_only_sensor import (
        BoschBuderusLocalOnlySensor,
    )

    definition = definition_for(REFRIGERANT)
    reading = refrigerant("standby", allowed=[*DECLARED, "standby"])
    fake = SimpleNamespace(
        _definition=definition,
        _reading=lambda: reading,
        _options_seen=definition.options,
    )
    options = BoschBuderusLocalOnlySensor.options.fget(fake)
    assert options[: len(definition.options)] == list(definition.options)
    assert options[-1] == "standby"
    fake = SimpleNamespace(
        _definition=definition_for("/heatSources/dhw/cylinderTemperature"),
        _reading=lambda: None,
    )
    assert BoschBuderusLocalOnlySensor.options.fget(fake) is None


def test_declared_states_are_normalized_like_the_value() -> None:
    definition = definition_for(REFRIGERANT)
    reading = refrigerant("Pool Heat", allowed=["HEATING", "Pool Heat", "Stand-by"])
    assert native_value(definition, reading) == "pool_heat"
    assert native_value(definition, refrigerant("HEATING", allowed=["HEATING"])) == (
        "heating"
    )
    options = state_options(definition, reading)
    assert len(options) == len(set(options))
    assert "stand_by" in options and "Pool Heat" not in options


def test_options_only_grow_while_running() -> None:
    from types import SimpleNamespace

    from custom_components.bosch_buderus_heating.local_only_sensor import (
        BoschBuderusLocalOnlySensor,
    )

    definition = definition_for(REFRIGERANT)
    current = {"reading": refrigerant("standby", allowed=[*DECLARED, "standby"])}
    fake = SimpleNamespace(
        _definition=definition,
        _reading=lambda: current["reading"],
        _options_seen=definition.options,
    )
    first = BoschBuderusLocalOnlySensor.options.fget(fake)
    assert "standby" in first
    current["reading"] = None  # a gap between readings
    assert BoschBuderusLocalOnlySensor.options.fget(fake) == first
    current["reading"] = refrigerant("eco", allowed=["eco"])
    grown = BoschBuderusLocalOnlySensor.options.fget(fake)
    assert grown[: len(first)] == first and grown[-1] == "eco"
