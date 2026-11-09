"""C10-b: additional local signals of hybrid systems with an HM200.

Synthetic signal answers only: no real hybrid system was available. Every
signal is disabled by default; the gateway's own unit and states win over the
expected ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.bosch_buderus_heating.local_only import (
    CATALOG,
    DURATION,
    FLAG,
    HYBRID_SIGNALS,
    MODE,
    TEMPERATURE,
    definition_for,
    entity_name,
    for_reading,
    native_value,
    state_class,
    state_options,
    unit_and_class,
)
from custom_components.bosch_buderus_heating.local_plant_view import (
    local_reading,
    local_sensor,
)
from tests.test_local_api import read

COMPONENT = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"


def signal(name, value=None, *, kind=None, unit=None, state=None):
    raw = {
        "id": f"/signals/HYBMAN.{name}",
        "type": kind
        or (
            "stringValue"
            if isinstance(value, str)
            else "floatValue"
            if isinstance(value, float)
            else "integerValue"
        ),
        "writeable": 0,
        "value": value,
    }
    if unit is not None:
        raw["unitOfMeasure"] = unit
    if state is not None:
        raw["state"] = state
    return read(raw)


def shown(observation):
    sensor, reason = local_sensor(observation)
    assert sensor is not None, reason
    reading = local_reading(observation)
    return sensor, native_value(sensor.definition, reading), reading


def test_every_hybrid_signal_is_disabled_and_named() -> None:
    assert len(HYBRID_SIGNALS) == 35
    for definition in HYBRID_SIGNALS:
        assert definition.enabled is False and definition.unverified
        assert definition in CATALOG
        for alternate in (definition.alternate,):
            assert alternate is None or alternate.enabled is False
        path = definition.pattern.pattern.replace("\\", "")
        assert definition_for(path) is definition
        german, english = (entity_name(definition, path, lang) for lang in ("de", "en"))
        assert german and english and "HYBMAN" not in german + english
    # Other signals stay generic.
    assert definition_for("/signals/SRC.CUHP.Stats.ControlUnit").key == "signal"


def test_a_temperature_uses_the_expected_unit_only_without_its_own() -> None:
    sensor, value, _ = shown(signal("ODUMONITOR.TR3Temp", 7.5, unit="C"))
    assert value == 7.5
    assert (sensor.unit, sensor.device_class) == ("°C", SensorDeviceClass.TEMPERATURE)
    assert not sensor.enabled
    # No unit in the answer: the expected one stands in.
    sensor, value, _ = shown(signal("TempFlowHeatPump", 41.0))
    assert (sensor.unit, sensor.device_class) == ("°C", SensorDeviceClass.TEMPERATURE)
    # The answer declares another unit: the gateway's own unit wins.
    sensor, value, _ = shown(signal("TempFlowHeatPump", 41, unit="%"))
    assert (sensor.unit, sensor.device_class) == ("%", None)
    assert value == 41


def test_an_ambiguous_signal_is_either_a_temperature_or_a_flag() -> None:
    temperature = signal("HighestPermittedTemp", 55.0, unit="C")
    flag = signal("HighestPermittedTemp", "true")
    mapped = signal("HighestPermittedTemp", 1, state={"off": 0, "on": 1})
    base = definition_for("/signals/HYBMAN.HighestPermittedTemp")
    assert for_reading(base, temperature).kind == TEMPERATURE
    assert for_reading(base, flag).kind == FLAG
    assert for_reading(base, mapped).kind == FLAG
    sensor, value, _ = shown(temperature)
    assert (value, sensor.device_class) == (55.0, SensorDeviceClass.TEMPERATURE)
    sensor, value, _ = shown(flag)
    assert (value, sensor.device_class) == ("on", SensorDeviceClass.ENUM)
    assert sensor.options == ("on", "off")
    assert shown(mapped)[1] == "on"
    # One entity per signal: both forms share the path's unique ID.
    assert shown(temperature)[0].unique_suffix == shown(flag)[0].unique_suffix


@pytest.mark.parametrize(
    "answer",
    [
        # An error marker at start-up decides nothing (review C10-b).
        {"value": -32768, "state": {"open": -32768}},
        {"value": 0},
        {"value": 1},
        {"value": 1.0},
    ],
)
def test_without_decisive_evidence_no_entity_is_created(answer) -> None:
    observation = signal("HighestPermittedTemp", **answer)
    base = definition_for("/signals/HYBMAN.HighestPermittedTemp")
    assert for_reading(base, observation) is None
    assert local_sensor(observation) == (None, "undecided")
    # The registered form keeps the entity as it was created.
    assert for_reading(base, observation, registered=FLAG).kind == FLAG
    assert for_reading(base, observation, registered=TEMPERATURE).kind == TEMPERATURE
    # A marker with a temperature unit: the unit decides.
    with_unit = signal("HighestPermittedTemp", unit="C", **answer)
    assert for_reading(base, with_unit).kind == TEMPERATURE
    # Later a text answer never turns a registered temperature into a flag.
    text = signal("HighestPermittedTemp", "false")
    assert for_reading(base, text, registered=TEMPERATURE).kind == TEMPERATURE


async def test_the_registered_form_survives_a_restart(hass) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.bosch_buderus_heating.const import DOMAIN
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        local_only_unique_id,
        registered_form,
    )

    registry = er.async_get(hass)
    flag_path = "/signals/HYBMAN.HighestPermittedTemp"
    temperature_path = "/signals/HYBMAN.LowOutdoorTempStopHeating"
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id("gw", flag_path),
        capabilities={"options": ["on", "off"]},
        original_device_class="enum",
    )
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id("gw", temperature_path),
        capabilities={"state_class": "measurement"},
        original_device_class="temperature",
    )
    assert registered_form(hass, "gw", flag_path) == FLAG
    assert registered_form(hass, "gw", temperature_path) == TEMPERATURE
    assert registered_form(hass, "gw", "/signals/HYBMAN.PumpOn") is None
    assert registered_form(hass, "other", flag_path) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", "on"), ("false", "off"), ("on", "on"), ("maybe", None)],
)
def test_on_off_signals(value, expected) -> None:
    sensor, shown_value, _ = shown(signal("PumpOn", value))
    assert sensor.definition.kind == FLAG
    assert shown_value == expected


def test_the_smart_grid_mode_prefers_the_declared_states() -> None:
    states = {
        "Not_used": 0,
        "EVU_block_active": 1,
        "Normal_operation": 2,
        "Smart_grid": 3,
        "Force_smart_grid": 4,
    }
    expected = {0: "not_used", 1: "blocked", 2: "normal", 3: "preferred", 4: "forced"}
    for raw, mode in expected.items():
        sensor, value, reading = shown(signal("SmartGridMode", raw, state=states))
        assert sensor.definition.kind == MODE
        assert value == mode
        assert set(state_options(sensor.definition, reading)) == set(
            sensor.definition.options
        )
    # LOE-3: without a state map the same numbers as the declaration: 0 is
    # not used, 1..4 the SG-Ready modes (not verified without a map).
    for raw, mode in expected.items():
        assert shown(signal("SmartGridMode", raw))[1] == mode
    assert shown(signal("SmartGridMode", 5))[1] is None
    assert shown(signal("SmartGridMode", 7))[1] is None


def test_timers_and_run_times() -> None:
    sensor, value, _ = shown(signal("TimeTillNextCompressorStart", 12, unit="min"))
    assert (value, sensor.unit) == (12, "min")
    assert sensor.device_class is SensorDeviceClass.DURATION
    assert shown(signal("TimeTillNextCompressorStart", 0))[1] == 0
    # Nothing is invented for an answer without a number.
    empty = signal("TimeTillNextCompressorStop", "", kind="stringValue")
    assert (
        native_value(
            definition_for("/signals/HYBMAN.TimeTillNextCompressorStop"),
            local_reading(empty),
        )
        is None
    )
    hours = definition_for("/signals/HYBMAN.OpTimeCompressorHeating")
    assert hours.kind == DURATION
    assert unit_and_class(hours, None) == ("h", SensorDeviceClass.DURATION)
    # Like every local run time: no statistics, so a lower count after a
    # device reset never becomes a negative consumption.
    assert state_class(hours) is None
    temperature = definition_for("/signals/HYBMAN.TempSysReturn")
    assert state_class(temperature) is SensorStateClass.MEASUREMENT


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_smart_grid_states_are_translated(language) -> None:
    states = json.loads(
        (COMPONENT / f"translations/{language}.json").read_text(encoding="utf-8")
    )["entity"]["sensor"]["smart_grid_mode"]["state"]
    assert set(states) == {"blocked", "normal", "preferred", "forced", "not_used"}
    assert states["forced"] == ("Forced" if language == "en" else "Erzwungen")


def test_a_long_declared_name_is_unknown_and_never_an_option() -> None:
    long_name = "Mode_" + "x" * 70
    observation = signal("SmartGridMode", 5, state={long_name: 5, "Smart_grid": 3})
    sensor, value, reading = shown(observation)
    assert value is None
    options = state_options(sensor.definition, reading)
    assert all(len(item) <= 64 for item in options)
    assert (
        shown(signal("SmartGridMode", 3, state={long_name: 5, "Smart_grid": 3}))[1]
        == "preferred"
    )


def test_text_answers_behave_like_generic_text_signals() -> None:
    # Text with declared values: a state with those values.
    declared = signal("EmergencyModeStatus", "Active Mode", kind="stringValue")
    assert local_sensor(declared) == (None, "text")
    with_values = read(
        {
            "id": "/signals/HYBMAN.EmergencyModeStatus",
            "type": "stringValue",
            "writeable": 0,
            "value": "Active Mode",
            "allowedValues": ["Inactive", "Active Mode"],
        }
    )
    sensor, value, _ = shown(with_values)
    assert sensor.options == ("inactive", "active_mode")
    assert value == "active_mode"
    # The smart grid mode as an SG-Ready name; other text without values: none.
    assert shown(signal("SmartGridMode", "Normal"))[1] == "normal"
    assert local_sensor(signal("SmartGridMode", "something")) == (
        None,
        "text",
    )


def test_a_state_beyond_the_option_limit_is_unknown() -> None:
    states = {f"m{index}": index for index in range(70)}
    sensor, value, reading = shown(signal("SmartGridMode", 69, state=states))
    options = state_options(sensor.definition, reading)
    assert len(options) == 64 and "m69" not in options
    assert value is None  # never a state outside the options
    assert shown(signal("SmartGridMode", 3, state=states))[1] == "m3"
