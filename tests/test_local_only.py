"""P45: readings only the local access provides become their own entities."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_only import (
    CATALOG,
    GENERIC,
    STATE,
    definition_for,
    unit_and_class,
)
from custom_components.bosch_buderus_heating.local_only_sensor import (
    BoschBuderusLocalOnlySensor,
    local_only_unique_id,
    track_local_only_sensors,
)
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.test_local_api import read
from tests.test_local_selection import TARGET, FakeRuntime
from tests.test_validity import _coordinator

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
EXAMPLES = json.loads(
    (Path(__file__).parent / "fixtures/local_api_examples.json").read_text(
        encoding="utf-8"
    )
)["examples"]

SETPOINT = "/heatSources/currentSupplySetpoint"
FLOW = "/heatSources/hs1/pumpVolumeFlow"
HOT_GAS = "/heatSources/hs1/refrigerant/hotGasTemp"
STATUS = "/heatSources/hs1/refrigerant/status"
FAN = "/heatSources/hs1/oduFanSpeed"
COMPRESSOR = "/heatSources/compressor/powerElecActual"
SIGNAL = "/signals/SRC.CUHP.HP1.ReturnTemp"
TEXT_SIGNAL = "/signals/GWEEBUS.Status"
PRIVATE = ("/signals/GWEEBUS.CEM.ID", "/signals/GWEEBUS.CEM.SKI")
RETURN = "/heatSources/returnTemperature"


def number(path, value, unit="C", kind="floatValue", **extra):
    raw = {"id": path, "type": kind, "writeable": 0, "value": value}
    if unit is not None:
        raw["unitOfMeasure"] = unit
    raw.update(extra)
    return read(raw)


def text(path, value, allowed=None):
    raw = {"id": path, "type": "stringValue", "writeable": 0, "value": value}
    if allowed is not None:
        raw["allowedValues"] = allowed
    return read(raw)


def coordinator_with(hass, observations, *resources, local_values=False, **kwargs):
    coordinator = _coordinator(hass, *resources)
    runtime = FakeRuntime(list(observations), enabled=local_values, **kwargs)
    coordinator.local_selection = LocalSelection(runtime, TARGET)
    return coordinator


def track(coordinator) -> list[BoschBuderusLocalOnlySensor]:
    added: list = []
    entry = SimpleNamespace(async_on_unload=Mock())
    track_local_only_sensors(entry, coordinator, added.extend)
    return added


def by_path(entities) -> dict[str, BoschBuderusLocalOnlySensor]:
    return {entity.resource_path: entity for entity in entities}


def standard_readings():
    return [
        number(SETPOINT, 34.5),
        number(FLOW, 1180, unit="l/h", kind="integerValue"),
        number(HOT_GAS, 61.25),
        text(STATUS, "heating", ["idle", "heating", "defrost"]),
        number(FAN, 45, unit="%", kind="integerValue"),
        number(SIGNAL, 28.5),
        text(TEXT_SIGNAL, "connected"),
        *(text(path, "secret-identifier") for path in PRIVATE),
    ]


# ---------------------------------------------------------------- creation


async def test_entities_for_readable_local_only_paths(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, standard_readings())
    entities = by_path(track(coordinator))
    assert set(entities) == {SETPOINT, FLOW, HOT_GAS, STATUS, FAN, SIGNAL}
    # Identifiers and text signals are never offered.
    assert not set(PRIVATE) & set(entities)
    assert TEXT_SIGNAL not in entities
    enabled = {p for p, e in entities.items() if e.entity_registry_enabled_default}
    assert enabled == {SETPOINT, FLOW, STATUS}
    assert entities[SIGNAL].entity_category is EntityCategory.DIAGNOSTIC
    assert entities[SETPOINT].entity_category is None
    assert entities[SETPOINT].unique_id == local_only_unique_id("gateway-one", SETPOINT)
    await coordinator.async_shutdown()


async def test_a_cloud_resource_keeps_its_own_entity(hass: HomeAssistant) -> None:
    cloud = Resource(
        COMPRESSOR,
        value=800.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="W"),
    )
    coordinator = coordinator_with(
        hass, [number(COMPRESSOR, 810.0, unit="W"), number(SETPOINT, 30.0)], cloud
    )
    assert set(by_path(track(coordinator))) == {SETPOINT}
    await coordinator.async_shutdown()


async def test_new_readable_paths_are_added_once(hass: HomeAssistant) -> None:
    runtime_readings = [number(SETPOINT, 30.0)]
    coordinator = coordinator_with(hass, runtime_readings)
    added: list = []
    entry = SimpleNamespace(async_on_unload=Mock())
    track_local_only_sensors(entry, coordinator, added.extend)
    assert [e.resource_path for e in added] == [SETPOINT]
    coordinator.local_selection._runtime.observations.append(number(HOT_GAS, 60.0))
    coordinator.async_update_listeners()
    coordinator.async_update_listeners()
    assert [e.resource_path for e in added] == [SETPOINT, HOT_GAS]
    await coordinator.async_shutdown()


async def test_nothing_without_a_local_access(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, standard_readings(), binding_enabled=False)
    assert track(coordinator) == []
    await coordinator.async_shutdown()


async def test_removed_local_access_removes_the_entities(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, standard_readings(), target="other-target")
    registry = er.async_get(hass)
    entry = coordinator.config_entry
    kept = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatSources:returnTemperature",
        config_entry=entry,
    )
    gone = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id("gateway-one", SETPOINT),
        config_entry=entry,
    )
    assert track(coordinator) == []
    assert registry.async_get(gone.entity_id) is None
    assert registry.async_get(kept.entity_id) is not None
    await coordinator.async_shutdown()


# ------------------------------------------------------------------ values


async def test_values_units_and_classes(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, standard_readings())
    entities = by_path(track(coordinator))
    setpoint = entities[SETPOINT]
    assert (setpoint.native_value, setpoint.native_unit_of_measurement) == (34.5, "°C")
    assert setpoint.device_class is SensorDeviceClass.TEMPERATURE
    assert setpoint.state_class is SensorStateClass.MEASUREMENT
    flow = entities[FLOW]
    assert (flow.native_value, flow.native_unit_of_measurement) == (1180, "L/h")
    assert flow.device_class is SensorDeviceClass.VOLUME_FLOW_RATE
    assert entities[FAN].native_unit_of_measurement == "%"
    status = entities[STATUS]
    assert status.device_class is SensorDeviceClass.ENUM
    assert status.native_value == "heating"
    assert "passive_defrost" in status.options
    assert status.translation_key == "refrigerant_status"
    # Generic signals: unit from the reading, no statistics.
    signal = entities[SIGNAL]
    assert (signal.native_value, signal.native_unit_of_measurement) == (28.5, "°C")
    assert signal.device_class is SensorDeviceClass.TEMPERATURE
    assert signal.state_class is None
    await coordinator.async_shutdown()


async def test_values_show_independently_of_the_local_values_option(
    hass: HomeAssistant,
) -> None:
    for option in (False, True):
        coordinator = coordinator_with(hass, standard_readings(), local_values=option)
        assert by_path(track(coordinator))[SETPOINT].native_value == 34.5
        await coordinator.async_shutdown()


@pytest.mark.parametrize(
    ("reading", "path"),
    [
        # Device states and markers are unknown, never a number.
        (number(HOT_GAS, -3276.8, state=[{"open": -32768}, {"short": 32767}]), HOT_GAS),
        (number(HOT_GAS, -32768, state=[{"open": -32768}]), HOT_GAS),
        (
            number(FLOW, 0, unit="l/h", kind="integerValue", state=[{"lowflow": 0}]),
            FLOW,
        ),
        (
            number(
                FLOW,
                65535,
                unit="l/h",
                kind="integerValue",
                state=[{"lowflow": 65535}],
            ),
            FLOW,
        ),
        # Another quantity is not shown (text in a number is already
        # rejected by the local decoder).
        (number(SETPOINT, 34.5, unit="W"), SETPOINT),
        # A state outside the reviewed options is unknown.
        (text(STATUS, "boost"), STATUS),
    ],
    ids=[
        "tenth_marker",
        "marker",
        "lowflow",
        "lowflow_65535",
        "other_quantity",
        "unknown_state",
    ],
)
async def test_unusable_readings_show_no_value(
    hass: HomeAssistant, reading, path
) -> None:
    coordinator = coordinator_with(hass, [reading])
    entity = by_path(track(coordinator))[path]
    assert entity.available
    assert entity.native_value is None
    await coordinator.async_shutdown()


async def test_without_a_current_reading_the_entity_is_unavailable(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(hass, [number(SETPOINT, 30.0)])
    entity = by_path(track(coordinator))[SETPOINT]
    coordinator.local_selection._runtime.observations.clear()
    assert not entity.available
    assert entity.native_value is None
    # Also while the cloud is unavailable, a current local reading is shown.
    coordinator.local_selection._runtime.observations.append(number(SETPOINT, 31.0))
    coordinator.last_update_success = False
    assert entity.available and entity.native_value == 31.0
    await coordinator.async_shutdown()


# ----------------------------------------------------------------- updates


async def test_only_readings_of_enabled_entities_count_as_changes(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(hass, [number(SETPOINT, 30.0), number(SIGNAL, 1.0)])
    selection = coordinator.local_selection
    runtime = selection._runtime
    assert selection.changed()
    assert not selection.changed()
    # A new reading of a path without an enabled entity: no change.
    runtime.observations[1] = read(
        {"id": SIGNAL, "type": "floatValue", "writeable": 0, "value": 2.0},
        observed_monotonic=99.0,
    )
    assert not selection.changed()
    # Once the entity is enabled, its readings count.
    selection.track_local_only(SIGNAL)
    assert selection.changed()
    runtime.observations[1] = read(
        {"id": SIGNAL, "type": "floatValue", "writeable": 0, "value": 3.0},
        observed_monotonic=120.0,
    )
    assert selection.changed()
    selection.untrack_local_only(SIGNAL)
    selection.changed()
    assert not selection.changed()
    # A newly readable path is a change (an entity may be added).
    runtime.observations.append(number(HOT_GAS, 60.0))
    assert selection.changed()
    await coordinator.async_shutdown()


# ---------------------------------------------------------------- contract


def _concrete(template: str) -> str:
    replacements = {
        "{heatSourceId}": "hs1",
        "{heatingCircuitId}": "hc1",
        "{dhwCircuitId}": "dhw1",
        "{inputId}": "input1",
        "{signalId}": "SRC.Example",
        "{ventilationZoneId}": "zone1",
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def test_reviewed_paths_are_documented_and_fit_the_examples() -> None:
    """Each reviewed path exists in the pinned specification; its example's
    unit fits the reviewed quantity and its states match the options."""
    templates = {row["template"]: row["response"] for row in EXAMPLES}
    checked = 0
    for template, response in templates.items():
        path = _concrete(template)
        definition = definition_for(path)
        if definition is None or definition.kind == GENERIC:
            continue
        checked += 1
        request_for_path(path)  # documented, readable address
        if definition.kind == STATE:
            # The reviewed states cover the documented example; they may add
            # states real gateways declare (C5).
            assert set(response.get("allowedValues") or ()) <= set(
                definition.options
            ), template
        else:
            assert unit_and_class(definition, response.get("unitOfMeasure")), template
    # Controller signals (C7, C10-b) are not part of the documented examples.
    reviewed = [
        d
        for d in CATALOG
        if d.kind != GENERIC
        and not d.unverified
        and not d.pattern.pattern.startswith(re.escape("/signals/"))
    ]
    assert checked == len(reviewed)


def test_the_state_translations_exist_in_every_language() -> None:
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        sensor = json.loads((INTEGRATION / name).read_text(encoding="utf-8"))["entity"][
            "sensor"
        ]
        for definition in CATALOG:
            if definition.translation_key is None:
                continue
            states = sensor[definition.translation_key]["state"]
            assert set(definition.options) <= set(states), (name, definition.key)


def test_identifier_signals_are_never_offered() -> None:
    for signal in ("GWEEBUS.CEM.ID", "GWEEBUS.CEM.SKI", "X.Serial", "Y.MAC", "Z.UUID"):
        assert definition_for(f"/signals/{signal}") is None
    assert definition_for("/signals/SC.HC1.FlowTempSetp") is not None
    assert definition_for("/signals/a/b") is None
    assert re.fullmatch(r"[a-z_]+", next(iter(CATALOG)).key)


# ------------------------------------------------- cases from the P45 review


async def test_a_cloud_resource_found_later_takes_over(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, [number(COMPRESSOR, 810.0, unit="W")])
    registry = er.async_get(hass)
    entity_id = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id("gateway-one", COMPRESSOR),
        config_entry=coordinator.config_entry,
    ).entity_id
    (entity,) = track(coordinator)
    assert entity.available
    coordinator.resources[COMPRESSOR] = Resource(
        COMPRESSOR,
        value=800.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="W"),
    )
    coordinator.async_update_listeners()
    assert not entity.available
    assert registry.async_get(entity_id) is None
    await coordinator.async_shutdown()


async def test_setup_removes_entries_the_cloud_offers_now(hass: HomeAssistant) -> None:
    cloud = Resource(
        COMPRESSOR,
        value=800.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="W"),
    )
    coordinator = coordinator_with(hass, [number(SETPOINT, 30.0)], cloud)
    registry = er.async_get(hass)
    entry = coordinator.config_entry
    orphan = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id("gateway-one", COMPRESSOR),
        config_entry=entry,
    )
    kept = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id("gateway-one", SETPOINT),
        config_entry=entry,
    )
    track(coordinator)
    assert registry.async_get(orphan.entity_id) is None
    assert registry.async_get(kept.entity_id) is not None
    await coordinator.async_shutdown()


async def test_a_local_access_removed_while_running_keeps_the_settings(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(hass, [number(SETPOINT, 30.0)])
    registry = er.async_get(hass)
    entity_id = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id("gateway-one", SETPOINT),
        config_entry=coordinator.config_entry,
    ).entity_id
    added = track(coordinator)
    runtime = coordinator.local_selection._runtime
    saved = dict(runtime.snapshot.bindings)
    runtime.snapshot = copy.copy(runtime.snapshot)
    runtime.snapshot.bindings = {}
    coordinator.async_update_listeners()
    assert registry.async_get(entity_id) is not None  # a reload cleans up
    assert not added[0].available
    runtime.snapshot = copy.copy(runtime.snapshot)
    runtime.snapshot.bindings = saved
    coordinator.async_update_listeners()
    assert added[0].available and added[0].native_value == 30.0
    await coordinator.async_shutdown()


async def test_generic_kelvin_is_no_temperature(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, [number("/signals/SRC.Spread", 5.0, unit="K")])
    (entity,) = track(coordinator)
    assert entity.native_unit_of_measurement == "K"
    assert entity.device_class is None
    await coordinator.async_shutdown()


async def test_local_only_readings_are_reused_while_unchanged(
    hass: HomeAssistant,
) -> None:
    coordinator = coordinator_with(hass, [])
    selection = coordinator.local_selection
    # The runtime keeps one published state per binding until the next one.
    state = SimpleNamespace(current_observations=tuple(standard_readings()))
    selection._runtime.local_state = lambda binding: state
    first = selection.local_only_observations()
    assert SETPOINT in first
    assert selection.local_only_observations() is first  # outside a pass, too
    state = SimpleNamespace(current_observations=(number(SETPOINT, 31.0),))
    assert set(selection.local_only_observations()) == {SETPOINT}
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    "name",
    [
        "CEM.DeviceID",
        "X.RemoteSKI",
        "X.MacAddress",
        "X.WlanSSID",
        "X.IPAddress",
        "X.Fingerprint",
        "X.Key",
        "X.Token",
        "X.Password",
        "X.CertHash",
    ],
)
def test_more_identifier_names_are_never_offered(name) -> None:
    assert definition_for(f"/signals/{name}") is None


async def test_reviewed_readings_suggest_a_fixed_precision(hass: HomeAssistant) -> None:
    """Independent of whether a reading had arrived at registration."""
    coordinator = coordinator_with(hass, standard_readings())
    entities = by_path(track(coordinator))
    assert entities[SETPOINT].suggested_display_precision == 1
    assert entities[FLOW].suggested_display_precision == 0
    assert entities[FAN].suggested_display_precision == 0
    assert entities[SIGNAL].suggested_display_precision is None
    await coordinator.async_shutdown()


# ----------------------------------------------- ventilation readings (P31)

VENT = "/ventilation/zone1"


def ventilation_readings():
    return [
        number(VENT + "/sensors/exhaustTemp", 8.5),
        number(VENT + "/sensors/supplyTemp", 19.0),
        number(VENT + "/sensors/internalHumidity", 48.0, unit="%"),
        number(VENT + "/maxRelativeHumidity", 61.0, unit="%"),
        number(VENT + "/sensors/internalAirQuality", 640.0, unit="ppm"),
        number(VENT + "/exhaustFanPower", 35.0, unit="%"),
        number(VENT + "/exhaustFanSpeed", 1450.0, unit="1/min"),
        number(VENT + "/sensors/supplyFanRotation", 1400.0, unit="rpm"),
        number(VENT + "/summerBypass/flapPower", 30.0, unit="%"),
        number(VENT + "/elAuxHeaterPower", 0.0, unit="%"),
        number(VENT + "/applianceRunTime", 1234.0, unit="mins"),
        number(VENT + "/filter/remainingTime", 90.0, unit="mins"),
        text(VENT + "/exhaustFanLevel", "nom", ["off", "min", "red", "nom"]),
    ]


async def test_ventilation_readings_are_offered_disabled(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, ventilation_readings())
    entities = by_path(track(coordinator))
    assert set(entities) == {reading.request.path for reading in ventilation_readings()}
    assert not any(e.entity_registry_enabled_default for e in entities.values())
    assert all(e.entity_category is None for e in entities.values())
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    ("suffix", "value", "unit", "device_class", "statistics"),
    [
        ("/sensors/exhaustTemp", 8.5, "°C", SensorDeviceClass.TEMPERATURE, True),
        ("/sensors/internalHumidity", 48.0, "%", SensorDeviceClass.HUMIDITY, True),
        ("/maxRelativeHumidity", 61.0, "%", SensorDeviceClass.HUMIDITY, True),
        ("/sensors/internalAirQuality", 640.0, "ppm", None, True),
        ("/exhaustFanPower", 35.0, "%", None, True),
        ("/exhaustFanSpeed", 1450.0, "rpm", None, True),
        # The documented unit is malformed: no class and no statistics.
        ("/sensors/supplyFanRotation", 1400.0, "rpm", None, False),
        ("/applianceRunTime", 1234.0, "min", SensorDeviceClass.DURATION, False),
        ("/filter/remainingTime", 90.0, "min", SensorDeviceClass.DURATION, False),
    ],
)
async def test_ventilation_units_and_classes(
    hass: HomeAssistant, suffix, value, unit, device_class, statistics
) -> None:
    coordinator = coordinator_with(hass, ventilation_readings())
    entity = by_path(track(coordinator))[VENT + suffix]
    assert entity.native_value == value
    assert entity.native_unit_of_measurement == unit
    assert entity.device_class == device_class
    assert (entity.state_class is SensorStateClass.MEASUREMENT) is statistics
    await coordinator.async_shutdown()


async def test_ventilation_fan_level_is_a_translated_state(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, ventilation_readings())
    level = by_path(track(coordinator))[VENT + "/exhaustFanLevel"]
    assert level.device_class is SensorDeviceClass.ENUM
    assert level.translation_key == "ventilation_fan_level"
    assert level.options == ["off", "min", "red", "nom", "max", "dem"]
    assert level.native_value == "nom"
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    ("suffix", "unit"),
    [
        ("/sensors/internalHumidity", "ppm"),
        ("/sensors/internalHumidity", None),
        ("/sensors/internalAirQuality", "%"),
        ("/exhaustFanSpeed", "%"),
        ("/exhaustFanPower", "W"),
        ("/applianceRunTime", "kWh"),
        ("/sensors/exhaustTemp", "%"),
    ],
)
async def test_ventilation_readings_in_another_unit_are_not_shown(
    hass: HomeAssistant, suffix, unit
) -> None:
    coordinator = coordinator_with(hass, [number(VENT + suffix, 5.0, unit=unit)])
    entity = by_path(track(coordinator))[VENT + suffix]
    assert entity.available
    assert entity.native_value is None
    assert entity.device_class is None
    await coordinator.async_shutdown()


async def test_supply_fan_rotation_with_its_documented_unit_shows_no_value(
    hass: HomeAssistant,
) -> None:
    path = VENT + "/sensors/supplyFanRotation"
    coordinator = coordinator_with(hass, [number(path, 1400.0, unit='rpm"')])
    entity = by_path(track(coordinator))[path]
    # The local decoder does not accept the malformed unit: no numeric claim.
    assert entity.available
    assert entity.native_value is None
    assert entity.native_unit_of_measurement is None
    assert entity.device_class is None and entity.state_class is None
    await coordinator.async_shutdown()


async def test_ventilation_reading_with_a_cloud_resource_keeps_the_cloud_entity(
    hass: HomeAssistant,
) -> None:
    supply = VENT + "/sensors/supplyTemp"
    cloud = Resource(
        supply,
        value=19.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )
    coordinator = coordinator_with(hass, ventilation_readings(), cloud)
    entities = by_path(track(coordinator))
    assert supply not in entities
    assert VENT + "/sensors/exhaustTemp" in entities
    await coordinator.async_shutdown()


async def test_unread_ventilation_paths_create_no_entities(hass: HomeAssistant) -> None:
    coordinator = coordinator_with(hass, [number(VENT + "/sensors/exhaustTemp", 8.0)])
    assert set(by_path(track(coordinator))) == {VENT + "/sensors/exhaustTemp"}
    await coordinator.async_shutdown()


async def test_official_ventilation_examples_show_no_marker_value(
    hass: HomeAssistant,
) -> None:
    """The examples report -32768 without declaring it as a state."""
    templates = {row["template"]: row["response"] for row in EXAMPLES}
    readings = [
        read(templates[f"/ventilation/{{ventilationZoneId}}/sensors/{name}"])
        for name in ("exhaustTemp", "extractTemp", "outdoorTemp", "supplyTemp")
    ]
    coordinator = coordinator_with(hass, readings)
    entities = by_path(track(coordinator))
    assert len(entities) == 4
    for entity in entities.values():
        assert entity.native_value is None, entity.resource_path
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (32767, "floatValue"),
        (-32768, "floatValue"),
        (3276.7, "floatValue"),
        (-3276.8, "floatValue"),
        (-32768.0, "floatValue"),
    ],
)
async def test_undeclared_temperature_markers_show_no_value(
    hass: HomeAssistant, value, kind
) -> None:
    path = VENT + "/sensors/outdoorTemp"
    coordinator = coordinator_with(hass, [number(path, value, kind=kind)])
    assert by_path(track(coordinator))[path].native_value is None
    await coordinator.async_shutdown()


async def test_other_quantities_keep_large_values(hass: HomeAssistant) -> None:
    # Only temperatures carry these markers; a large run time stays a value.
    path = VENT + "/applianceRunTime"
    coordinator = coordinator_with(hass, [number(path, 32767.0, unit="mins")])
    assert by_path(track(coordinator))[path].native_value == 32767.0
    await coordinator.async_shutdown()


# ------------------------------------------- P49b: value type, registered


async def test_a_generic_number_with_an_error_marker_is_offered(
    hass: HomeAssistant,
) -> None:
    """3A: the reported type decides, not the current value."""
    marker = number(SIGNAL, -32768, kind="integerValue", state=[{"open": -32768}])
    coordinator = coordinator_with(hass, [marker])
    entity = by_path(track(coordinator))[SIGNAL]
    assert not entity.entity_registry_enabled_default  # generic: disabled
    assert entity.available and entity.native_value is None  # "unknown"
    await coordinator.async_shutdown()


ROOM = "/heatingCircuits/hc1/roomtemperature"  # documented, no reviewed entry


def register_local_only(hass, coordinator, path):
    return er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        local_only_unique_id(coordinator.gateway.gateway_id, path),
        config_entry=coordinator.config_entry,
    )


async def test_a_registered_documented_reading_stays_served(
    hass: HomeAssistant,
) -> None:
    """4A: an entity the local plant view created keeps its local values."""
    readings = [number(ROOM, 21.25), number(SETPOINT, 34.5)]
    coordinator = coordinator_with(hass, readings)
    # C7: every reading becomes an entity (an unreviewed one disabled).
    added = by_path(track(coordinator))
    assert set(added) == {SETPOINT, ROOM}
    # Enabled like the cloud's entity of the same reading would be.
    assert added[ROOM].entity_registry_enabled_default
    await coordinator.async_shutdown()
    coordinator = coordinator_with(hass, readings)
    register_local_only(hass, coordinator, ROOM)
    entities = by_path(track(coordinator))
    assert set(entities) == {SETPOINT, ROOM}
    assert entities[ROOM].available and entities[ROOM].native_value == 21.25
    # The same entity as the local plant view's (unique ID and kind).
    assert entities[ROOM].unique_id == local_only_unique_id("gateway-one", ROOM)
    await coordinator.async_shutdown()


async def test_a_cloud_resource_still_takes_over_a_registered_reading(
    hass: HomeAssistant,
) -> None:
    cloud = Resource(
        ROOM,
        value=21.0,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )
    coordinator = coordinator_with(hass, [number(ROOM, 21.25)], cloud)
    entry = register_local_only(hass, coordinator, ROOM)
    assert ROOM not in by_path(track(coordinator))
    assert er.async_get(hass).async_get(entry.entity_id) is None
    await coordinator.async_shutdown()


async def test_registered_identifiers_are_never_served(hass: HomeAssistant) -> None:
    path = "/gateway/eth/ip/ipv4"
    coordinator = coordinator_with(hass, [text(path, "192.0.2.10")])
    register_local_only(hass, coordinator, path)
    assert path not in by_path(track(coordinator))
    assert path not in coordinator.local_selection.local_only_observations()
    await coordinator.async_shutdown()


async def test_a_volume_flow_in_litres_per_hour_is_shown_unscaled(
    hass: HomeAssistant,
) -> None:
    reading = number(
        FLOW, 734, unit="l/h", kind="integerValue", state=[{"lowflow": 65535}]
    )
    coordinator = coordinator_with(hass, [reading])
    flow = by_path(track(coordinator))[FLOW]
    assert (flow.native_value, flow.native_unit_of_measurement) == (734, "L/h")
    assert flow.device_class is SensorDeviceClass.VOLUME_FLOW_RATE
    await coordinator.async_shutdown()
