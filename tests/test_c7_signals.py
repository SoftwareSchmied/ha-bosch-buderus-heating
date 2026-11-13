"""C7: every local reading becomes an entity; unneeded ones are disabled.

Answers in the shape of the reference heat pump's local signals (firmware
15.00.01). Identifiers, network data and structures never become entities.
"""

from __future__ import annotations

from datetime import date
from functools import partial
from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfTime

from custom_components.bosch_buderus_heating.local_only import (
    DURATION,
    FLAG,
    FLOW,
    GENERIC,
    MODE,
    POWER,
    STATE,
    native_value,
    state_class,
    unit_and_class,
)
from custom_components.bosch_buderus_heating.local_only_sensor import (
    BoschBuderusInstallationDateSensor,
    local_only_unique_id,
)
from custom_components.bosch_buderus_heating.local_plant_view import (
    build_local_plant_view,
    local_reading,
    local_sensor,
)
from tests.test_local_api import read


def answer(path, value, *, kind=None, unit=None, state=None, allowed=None):
    raw = {
        "id": path,
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
    if allowed is not None:
        raw["allowedValues"] = allowed
    return read(raw)


def signal(name, value, **kwargs):
    return answer(f"/signals/{name}", value, **kwargs)


def shown(observation):
    sensor, reason = local_sensor(observation)
    assert sensor is not None, reason
    return sensor, native_value(sensor.definition, local_reading(observation))


# ----------------------------------------------------------------- C7-1


@pytest.mark.parametrize(
    ("name", "enabled"),
    [
        ("SRC.CUHP.HMI.CompressorActive", True),
        ("SRC.CUHP.HP1.CompressorStatus", False),  # the duplicate
        ("SRC.CUHP.HP1.ElectricalHeaterStep3", True),
        ("SRC.CUHP.HP1.ElectricalHeaterStep4", False),
        ("SRC.CUHP.HP1.InverterOverheated", True),
        ("SRC.RelayStatus.GasValve1", False),  # no reviewed meaning
    ],
)
def test_text_true_false_becomes_an_on_off_state(name, enabled) -> None:
    sensor, value = shown(signal(name, "true"))
    assert sensor.definition.kind == FLAG and sensor.enabled is enabled
    assert sensor.device_class is SensorDeviceClass.ENUM
    assert sensor.options == ("on", "off")
    assert sensor.definition.translation_key == "on_off_state"
    assert value == "on"
    assert shown(signal(name, "false"))[1] == "off"
    assert shown(signal(name, "FALSE"))[1] == "off"


def test_other_text_stays_unknown_or_is_never_shown() -> None:
    # A reviewed on/off signal with another text: unknown.
    assert shown(signal("SRC.CUHP.Cooling.Active", "maybe"))[1] is None
    # Free text without declared values could carry an identifier: none.
    assert local_sensor(signal("SRC.Some.Text", "abc"))[0] is None
    assert local_sensor(answer("/gateway/eth/ip/ipv4", "192.0.2.10"))[1] == "private"


def test_existing_generic_entities_keep_their_unique_id() -> None:
    path = "/signals/SRC.CUHP.HMI.CompressorActive"
    assert local_only_unique_id("gw", path) == (
        "gw:local_only:signals:SRC.CUHP.HMI.CompressorActive"
    )
    assert shown(signal("SRC.CUHP.HMI.CompressorActive", "true"))[0].unique_suffix == (
        "local_only:signals:SRC.CUHP.HMI.CompressorActive"
    )


# ----------------------------------------------------------------- C7-2


def test_a_name_map_without_a_unit_is_a_state() -> None:
    season = signal(
        "SC.SeasonOpt.Mode", 1, state={"HEATING": 1, "IDLE": 2, "COOLING": 3}
    )
    sensor, value = shown(season)
    assert sensor.definition.kind == STATE and sensor.enabled
    assert value == "heating"
    assert sensor.definition.translation_key == "signal_season_mode"
    sensor, value = shown(
        signal("SRC.CUHP.CH.GSOState", 2, state={"IDLE": 0, "HEAT": 1, "COOL": 2})
    )
    assert (value, sensor.enabled) == ("cool", True)
    sensor, value = shown(
        signal("SC.HolidayNew.3.State", 1, state={"IDLE": 0, "PLANNED": 1, "ACTIVE": 2})
    )
    assert sensor.definition.kind == STATE and not sensor.enabled
    assert sensor.options == ("idle", "planned", "active")
    assert sensor.definition.translation_key == "signal_holiday_slot"
    assert value == "planned"
    sensor, value = shown(
        signal("SC.DHW1.SD.ExtraActive", 1, state={"TRUE": 1, "FALSE": 0})
    )
    # O107: a TRUE/FALSE map is an on/off state like a true/false text.
    assert (value, sensor.definition.translation_key) == ("on", "on_off_state")
    assert (sensor.definition.kind, sensor.options) == (FLAG, ("on", "off"))
    assert shown(signal("SC.DHW1.SD.ExtraActive", 0, state={"TRUE": 1, "FALSE": 0}))[
        1
    ] == ("off")
    # Declared true/false text values: the same on/off state.
    sensor, value = shown(signal("SRC.Some.Flag", "FALSE", allowed=["TRUE", "FALSE"]))
    assert (sensor.definition.kind, value) == (FLAG, "off")
    assert sensor.definition.translation_key == "on_off_state"
    sensor, value = shown(
        signal(
            "SRC.CUHP.SmartGridMode",
            1,
            state={"Not_used": 0, "EVU_block_active": 1, "Normal_operation": 2},
        )
    )
    assert (sensor.definition.kind, value, sensor.enabled) == (MODE, "blocked", True)


def test_marker_maps_and_maps_with_a_unit_stay_numbers() -> None:
    markers = {"NA_OPEN": -3276.8, "NA_SHORT": 3276.7}
    sensor, value = shown(signal("SRC.OutdoorTemp", 6.4, unit="C", state=markers))
    assert sensor.definition.kind == GENERIC and not sensor.enabled  # C7-5
    assert (value, sensor.unit) == (6.4, "°C")
    assert shown(signal("SRC.OutdoorTemp", -3276.8, unit="C", state=markers))[1] is None
    # With a unit, any declared state is no measurement (review C7): unknown.
    setpoint = {"OFF_HEAT": 0, "OFF_COOL": 90}
    assert shown(signal("SC.HC1.FlowTempSetp", 90, unit="C", state=setpoint))[1] is None
    assert shown(signal("SC.HC1.FlowTempSetp", 0, unit="C", state=setpoint))[1] is None
    assert shown(signal("SC.HC1.FlowTempSetp", 23, unit="C", state=setpoint))[1] == 23
    flow = {"LOW_FLOW": 65535}
    sensor, value = shown(
        signal("SRC.HC1.FlowCtrl.PumpVolumeFlow", 65535, unit="l/h", state=flow)
    )
    assert value is None and sensor.definition.kind == FLOW
    # C2: unchanged, "lowflow" stays unknown.
    sensor, value = shown(
        answer(
            "/heatSources/hs1/pumpVolumeFlow",
            65535,
            unit="l/h",
            state={"lowflow": 65535},
        )
    )
    assert value is None and sensor.enabled


# ----------------------------------------------------------------- C7-3


def date_sensor(day, month, year):
    readings = {}
    for name, value in (("Day", day), ("Month", month), ("Year", year)):
        readings[f"/signals/SC.InstallationDate.{name}"] = local_reading(
            signal(
                f"SC.InstallationDate.{name}",
                value,
                unit=name.lower(),
                state={"INVALID2": 255, "INVALID": 0},
            )
        )
    coordinator = SimpleNamespace(
        local_selection=SimpleNamespace(local_only_reading=readings.get)
    )
    fake = SimpleNamespace(coordinator=coordinator)
    fake._part = partial(BoschBuderusInstallationDateSensor._part, fake)
    return BoschBuderusInstallationDateSensor.native_value.fget(fake)


def test_the_installation_date_is_derived_from_its_parts() -> None:
    assert date_sensor(1, 8, 60) == date(2060, 8, 1)
    assert date_sensor(0, 8, 60) is None  # INVALID
    assert date_sensor(1, 255, 60) is None  # INVALID2
    assert date_sensor(31, 2, 60) is None  # no real date
    # The parts stay entities, disabled, showing their number.
    sensor, value = shown(
        signal(
            "SC.InstallationDate.Year",
            60,
            unit="year",
            state={"INVALID2": 255, "INVALID": 0},
        )
    )
    assert (value, sensor.enabled, sensor.definition.kind) == (60, False, GENERIC)


# ----------------------------------------------------------------- C7-4


def test_reviewed_measurements() -> None:
    sensor, value = shown(signal("SRC.CUHP.HP1.Stats.CompressorCH", 1250704, unit="s"))
    definition = sensor.definition
    assert definition.kind == DURATION and sensor.enabled
    assert definition.suggested_unit == UnitOfTime.HOURS
    # Since 0.9.0 a cumulative run time is a growing total with long-term
    # statistics, like the cloud's working times.
    assert state_class(definition) is SensorStateClass.TOTAL_INCREASING
    assert value == 1250704
    sensor, _ = shown(signal("SRC.CUHP.HP1.CompressorPower", 2.4, unit="kW"))
    assert sensor.definition.kind == POWER and sensor.device_class is (
        SensorDeviceClass.POWER
    )
    assert state_class(sensor.definition) is SensorStateClass.MEASUREMENT
    sensor, _ = shown(signal("SC.DampOutdTemp", 6.2, unit="C"))
    assert sensor.enabled and sensor.device_class is SensorDeviceClass.TEMPERATURE
    # Internal timers: no statistics.
    sensor, _ = shown(signal("SRC.RelativeTime", 80664, unit="mins"))
    assert not sensor.enabled and state_class(sensor.definition) is None
    assert unit_and_class(sensor.definition, "mins")[1] is SensorDeviceClass.DURATION


# ----------------------------------------------------------------- C7-6


def test_external_inputs_and_the_room_controller_assignment() -> None:
    sensor, value = shown(
        answer("/heatSources/externalInputs/input2/state", "off", allowed=["off", "on"])
    )
    assert (sensor.definition.kind, value, sensor.enabled) == (STATE, "off", False)
    assert sensor.definition.translation_key == "on_off_state"
    sensor, value = shown(
        answer(
            "/devices/uhc/assignedTo",
            "unassigned",
            allowed=["unassigned", "hc1", "hc2", "hc3", "hc4"],
        )
    )
    assert (sensor.definition.kind, value, sensor.enabled) == (
        STATE,
        "unassigned",
        False,
    )


def test_the_local_plant_view_applies_the_same_rules() -> None:
    view = build_local_plant_view(
        [
            signal("SRC.CUHP.HP1.DHWValve", "false"),
            signal("SC.SeasonOpt.Mode", 2, state={"HEATING": 1, "IDLE": 2}),
            signal("SRC.Free.Text", "value"),
        ],
    )
    kinds = {item.path: item.definition.kind for item in view.local_sensors}
    assert kinds == {
        "/signals/SRC.CUHP.HP1.DHWValve": FLAG,
        "/signals/SC.SeasonOpt.Mode": STATE,
    }
    assert view.skipped == {"text": 1}


async def test_the_installation_date_entity_is_added_with_its_parts(hass) -> None:
    from tests.test_local_only import coordinator_with, track

    parts = [
        signal(
            f"SC.InstallationDate.{name}",
            value,
            unit=name.lower(),
            state={"INVALID2": 255, "INVALID": 0},
        )
        for name, value in (("Day", 1), ("Month", 8), ("Year", 60))
    ]
    coordinator = coordinator_with(hass, parts)
    entities = track(coordinator)
    (derived,) = [
        item
        for item in entities
        if isinstance(item, BoschBuderusInstallationDateSensor)
    ]
    assert derived.unique_id == "gateway-one:installation_date"
    assert derived.native_value == date(2060, 8, 1)
    assert derived.entity_registry_enabled_default
    assert len(entities) == 4  # and the three parts, disabled
    assert not any(
        item.entity_registry_enabled_default for item in entities if item is not derived
    )
    await coordinator.async_shutdown()


def test_a_unitless_marker_map_stays_a_number() -> None:
    markers = {"INVALID": 0, "INVALID2": 255}
    sensor, value = shown(signal("SRC.Some.Counter", 42, state=markers))
    assert sensor.definition.kind == GENERIC and value == 42
    assert shown(signal("SRC.Some.Counter", 255, state=markers))[1] is None


# ------------------------------------------------------------- review fixes


def entity(hass, path, observations=()):
    from custom_components.bosch_buderus_heating.local_only import definition_for
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        BoschBuderusLocalOnlySensor,
    )
    from tests.test_local_only import coordinator_with

    coordinator = coordinator_with(hass, list(observations))
    definition = definition_for(path)
    if observations:
        definition = local_sensor(observations[0])[0].definition
    return BoschBuderusLocalOnlySensor(coordinator, path, definition)


async def test_a_run_time_registers_before_its_first_reading(hass) -> None:
    from homeassistant.helpers import entity_registry as er
    from homeassistant.setup import async_setup_component

    from tests.ha_test_helpers import add_without_device

    assert await async_setup_component(hass, "sensor", {})
    sensor = entity(hass, "/signals/SRC.CUHP.HP1.Stats.CompressorCH")
    assert sensor.native_unit_of_measurement == "s"
    assert sensor.device_class is SensorDeviceClass.DURATION
    await add_without_device(hass, sensor)  # no ValueError
    await hass.async_block_till_done()
    registered = er.async_get(hass).async_get(sensor.entity_id)
    assert registered is not None
    assert registered.options["sensor.private"]["suggested_unit_of_measurement"] == (
        UnitOfTime.HOURS
    )


@pytest.mark.parametrize(
    ("path", "value", "category"),
    [
        ("/signals/SRC.CUHP.HMI.CompressorActive", "true", None),
        ("/signals/SRC.CUHP.HP1.CompressorStatus", "true", None),  # reviewed
        ("/signals/SRC.RelayStatus.GasValve1", "true", "diagnostic"),
        ("/signals/SC.SeasonOpt.Mode", 1, None),
        ("/signals/SC.HolidayNew.1.State", 1, "diagnostic"),
        ("/signals/SRC.OutdoorTemp", 6.4, "diagnostic"),
        ("/signals/SC.DampOutdTemp", 6.2, None),
    ],
)
async def test_unreviewed_readings_stay_diagnostic(hass, path, value, category) -> None:
    state = (
        {"IDLE": 0, "PLANNED": 1, "ACTIVE": 2}
        if "Holiday" in path
        else {"HEATING": 1, "IDLE": 2, "COOLING": 3}
        if "Season" in path
        else None
    )
    observation = answer(path, value, unit="C" if "Temp" in path else None, state=state)
    sensor = entity(hass, path, [observation])
    shown_category = sensor.entity_category
    assert (shown_category.value if shown_category else None) == category


async def test_the_installation_date_goes_with_the_local_access(hass) -> None:
    from homeassistant.helpers import entity_registry as er

    from custom_components.bosch_buderus_heating.const import DOMAIN
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        _remove_registered,
        installation_date_unique_id,
    )
    from tests.test_local_only import coordinator_with

    coordinator = coordinator_with(hass, [])
    registry = er.async_get(hass)
    created = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        installation_date_unique_id("gateway-one"),
        config_entry=coordinator.config_entry,
    )
    _remove_registered(coordinator)  # the cloud offers paths: kept
    assert registry.async_get(created.entity_id) is not None
    _remove_registered(coordinator, everything=True)  # local access removed
    assert registry.async_get(created.entity_id) is None
    await coordinator.async_shutdown()


async def test_an_unknown_unit_is_never_shown(hass) -> None:
    observation = signal(
        "SC.InstallationDate.Month",
        8,
        unit="month",
        state={"INVALID2": 255, "INVALID": 0},
    )
    assert local_sensor(observation)[0].unit is None
    sensor = entity(hass, "/signals/SC.InstallationDate.Month", [observation])
    assert sensor.native_value == 8
    assert sensor.native_unit_of_measurement is None
    assert sensor.device_class is None


async def test_a_state_outside_the_grown_options_is_unknown(hass) -> None:
    path = "/signals/SC.HolidayNew.1.State"
    observation = signal(
        "SC.HolidayNew.1.State", 1, state={"IDLE": 0, "PLANNED": 1, "ACTIVE": 2}
    )
    sensor = entity(hass, path, [observation])
    assert sensor.native_value == "planned"
    sensor._options_seen = tuple(f"x{number}" for number in range(64))
    assert sensor.native_value is None


@pytest.mark.parametrize(
    ("name", "private"),
    [
        ("Wifi.Ssid", True),
        ("Cem.Ski", True),
        ("Gw.MacAddress", True),
        ("Lan.Ip", True),
        ("Lan.IpV4", True),
        ("X.SerialNo", True),
        ("GWEEBUS.CEM.ID", True),
        ("SRC.CUHP.HP1.DripPanHeater", False),
        ("SRC.CUHP.HP1.CrankcaseHeater", False),
        ("X.Humid", False),
    ],
)
def test_identifier_names_in_any_spelling(name, private) -> None:
    from custom_components.bosch_buderus_heating.local_only import identifier_name

    assert identifier_name(name) is private


async def test_without_a_fixed_unit_no_unit_is_suggested(hass) -> None:
    from dataclasses import replace

    from custom_components.bosch_buderus_heating.local_only import definition_for
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        BoschBuderusLocalOnlySensor,
    )
    from tests.test_local_only import coordinator_with

    path = "/signals/SRC.CUHP.HP1.Stats.CompressorCH"
    loose = replace(definition_for(path), default_unit=None)
    sensor = BoschBuderusLocalOnlySensor(coordinator_with(hass, []), path, loose)
    assert sensor.suggested_unit_of_measurement is None


async def test_with_the_cloud_a_named_setpoint_state_is_no_temperature(hass) -> None:
    path = "/signals/SC.HC1.FlowTempSetp"
    setpoint = {"OFF_HEAT": 0, "OFF_COOL": 90}
    off = signal("SC.HC1.FlowTempSetp", 90, unit="C", state=setpoint)
    assert entity(hass, path, [off]).native_value is None
    value = signal("SC.HC1.FlowTempSetp", 23, unit="C", state=setpoint)
    assert entity(hass, path, [value]).native_value == 23


async def test_a_state_never_shows_a_unit_stored_by_an_earlier_number(hass) -> None:
    """LOE-2: the same ID was a number in °C; now a state map without a unit."""
    from homeassistant.helpers import entity_registry as er
    from homeassistant.setup import async_setup_component

    from custom_components.bosch_buderus_heating.const import DOMAIN
    from custom_components.bosch_buderus_heating.local_only import STATE
    from tests.ha_test_helpers import add_without_device
    from tests.test_local_only import coordinator_with

    name = "SRC.CUHP.HP1.SomeMode"
    observation = signal(name, 1, kind="integerValue", state={"OFF": 0, "ON_LOW": 1})
    assert await async_setup_component(hass, "sensor", {})
    sensor = entity(hass, f"/signals/{name}", [observation])
    assert sensor._definition.kind == STATE
    registry = er.async_get(hass)
    old = registry.async_get_or_create("sensor", "sensor", sensor.unique_id)
    registry.async_update_entity_options(old.entity_id, DOMAIN, {"native_unit": "°C"})
    sensor.coordinator.async_update_listeners()
    await add_without_device(hass, sensor)  # no ValueError for a unit
    await hass.async_block_till_done()
    state = hass.states.get(old.entity_id)
    assert state is not None and state.state == "on_low"
    assert "unit_of_measurement" not in state.attributes
    assert sensor.native_unit_of_measurement is None
    # The stored unit of the earlier number is cleared, not stored again.
    assert registry.async_get(old.entity_id).options[DOMAIN] == {"native_unit": None}
    await sensor.coordinator.async_shutdown()
    # Also without a current reading a state has no unit, whatever is stored.
    registry.async_update_entity_options(old.entity_id, DOMAIN, {"native_unit": "°C"})
    later = entity(hass, f"/signals/{name}", [observation])
    later.registry_entry = registry.async_get(old.entity_id)
    later.coordinator = coordinator_with(hass, [])
    assert later.native_unit_of_measurement is None


async def test_a_current_number_without_a_unit_shows_none(hass) -> None:
    """LOE-2: the held unit stands in only while there is no reading."""
    from tests.test_local_only import coordinator_with

    name = "SRC.CUHP.HP1.UnreviewedNumber"
    sensor = entity(hass, f"/signals/{name}", [signal(name, 21.5, unit="C")])
    assert sensor.native_unit_of_measurement == "°C"
    sensor.coordinator = coordinator_with(
        hass, [signal(name, 22.5, unit="unknown_unit")]
    )
    assert sensor.native_value == 22.5
    assert sensor.native_unit_of_measurement is None
    assert sensor.state_class is None  # no statistics under the old unit
    sensor.coordinator = coordinator_with(hass, [])
    assert sensor.native_unit_of_measurement == "°C"  # held while there is none


# ----------------------------------------------------------------- review D11/D12


def test_every_named_generic_controller_signal_keeps_its_statistics() -> None:
    """D11: statistics of a named signal must not hinge on its key spelling.

    Every generic controller signal except the installation date parts is a
    second source of a measurement; each one must be in the statistics set,
    so a renamed key can never silently drop the state class.
    """
    from custom_components.bosch_buderus_heating.local_only import (
        _SIGNAL_KEYS,
        CONTROLLER_SIGNALS,
    )

    named = {
        definition.key
        for definition in CONTROLLER_SIGNALS
        if definition.kind == GENERIC
        and not definition.key.startswith("installation_date_")
    }
    assert named
    assert named <= _SIGNAL_KEYS
    assert not {k for k in _SIGNAL_KEYS if k.startswith("installation_date_")}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, "on"),
        (0.0, "off"),
        (-0.0, "off"),
        (0.5, None),
        (2.0, None),
        (float("nan"), None),
        (float("inf"), None),
    ],
)
def test_float_on_off_values_are_states_like_integers(value, expected) -> None:
    """D12: an on/off signal answered as 1.0/0.0 is on/off, not unknown."""
    from decimal import Decimal

    from custom_components.bosch_buderus_heating.local_only import flag_state

    assert flag_state(value) == expected
    if value == value and abs(value) != float("inf"):
        assert flag_state(Decimal(str(value))) == expected


def test_a_float_flag_answer_is_shown_as_on_off() -> None:
    """D12: the same through a decoded local answer of type floatValue."""
    name = "SRC.CUHP.HMI.CompressorActive"
    sensor, value = shown(signal(name, 1.0))
    assert sensor.definition.kind == FLAG
    assert value == "on"
    assert shown(signal(name, 0.0))[1] == "off"
    assert shown(signal(name, 0.5))[1] is None
