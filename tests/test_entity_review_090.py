"""The entity review of 0.9.0: defaults, forms and names.

- Entities that only repeat another one on a plant are created disabled:
  the totals of the only heat source and the burner status of a heat pump.
  Existing entities keep their setting.
- The valve positions VR4 and VW1 and the hot water heat-up, checked against
  the recorded defrosts and hot water charges of the reference heat pump.
- Cumulative local run times get long-term statistics.
- A setting of a room controller that offers no control is shown by its
  read-only sensor, enabled for new entities.
- Names follow the controller's wording; no visible name says "PointT".
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import rrc_contracts
from custom_components.bosch_buderus_heating.binary_sensor import (
    build_binary_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.local_only import (
    CATALOG,
    DURATION,
    FLAG,
    definition_for,
    for_reading,
    native_value,
    state_class,
)
from custom_components.bosch_buderus_heating.observations import (
    from_local_observation,
)
from custom_components.bosch_buderus_heating.pointt import Resource
from custom_components.bosch_buderus_heating.pointt.models import ResourceMetadata
from custom_components.bosch_buderus_heating.resource_catalog import (
    plant_duplicate_paths,
)
from custom_components.bosch_buderus_heating.rrc_contract_dhw import LEVEL_HIGH
from custom_components.bosch_buderus_heating.rrc_platforms import (
    generic_presentation,
    setting_without_control,
)
from custom_components.bosch_buderus_heating.select import build_select_descriptions
from custom_components.bosch_buderus_heating.sensor import build_sensor_descriptions
from custom_components.bosch_buderus_heating.write_policies import RRC_CONTEXT
from custom_components.bosch_buderus_heating.writes import (
    VENTILATION_FAN_LEVEL_POLICY,
    assess_control,
)
from tests.helpers.rrc_cloud import Clock, cloud_patches, load_tree
from tests.test_entry import _entry
from tests.test_local_api import read
from tests.test_rrc_w0_baseline import PACKAGE, PLANTS

COMPONENT = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"
VALVES = (
    Path(__file__).parent / "fixtures" / "local_capture" / "k40rf-valves-2026-11.json"
)
STARTS = "/heatSources/numberOfStarts"
RUN_TIME = "/heatSources/workingTime/totalSystem"
FLAME = "/heatSources/flameStatus"
DUPLICATES = {STARTS, RUN_TIME, FLAME}
PAN = re.compile(r"(?i)\bpan\b")

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


# ------------------------------------------------- duplicates on a plant


def _k40() -> dict[str, Resource]:
    return load_tree("captures/k40-cloud-2026-11-05.json")


def _enabled(resources: dict[str, Resource]) -> dict[str, bool]:
    return {
        item.resource_path: bool(item.entity_registry_enabled_default)
        for item in build_sensor_descriptions(resources)
        if item.resource_path in DUPLICATES
    }


def _resource(path: str, value: object, kind: str, **metadata: object) -> Resource:
    return Resource(
        path=path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type=kind, **metadata),  # type: ignore[arg-type]
    )


def test_a_single_heat_pump_creates_its_duplicates_disabled() -> None:
    resources = _k40()
    assert plant_duplicate_paths(resources) == DUPLICATES
    assert _enabled(resources) == dict.fromkeys(DUPLICATES, False)
    # The counters of the heat source itself stay enabled.
    enabled = {
        item.key
        for item in build_sensor_descriptions(resources)
        if item.entity_registry_enabled_default
    }
    assert {
        "heatSources:hs1:numberOfStarts:total",
        "heatSources:hs1:workingTime:total",
    } <= enabled


def test_a_second_heat_source_keeps_the_totals_and_the_burner() -> None:
    resources = _k40()
    resources["/heatSources/hs2/type"] = _resource(
        "/heatSources/hs2/type", "boiler", "stringValue"
    )
    assert plant_duplicate_paths(resources) == set()
    assert _enabled(resources) == dict.fromkeys(DUPLICATES, True)


def test_a_hybrid_manager_keeps_the_burner_status() -> None:
    resources = _k40()
    path = "/heatSources/hybrid/activeHeatSource"
    resources[path] = _resource(path, "heatpump", "stringValue")
    assert plant_duplicate_paths(resources) == {STARTS, RUN_TIME}
    assert _enabled(resources)[FLAME] is True


def test_a_boiler_keeps_its_burner_status() -> None:
    # A gas boiler reports no heat pump type: its flame is the burner.
    resources = {
        path: resource
        for path, resource in _k40().items()
        if not path.endswith("/heatPumpType")
    }
    assert plant_duplicate_paths(resources) == {STARTS, RUN_TIME}
    assert _enabled(resources)[FLAME] is True


def test_without_a_reported_heat_source_the_totals_stay() -> None:
    resources = {
        STARTS: _resource(STARTS, 10, "floatValue"),
        FLAME: _resource(FLAME, "on", "stringValue"),
    }
    assert plant_duplicate_paths(resources) == set()


def test_a_burner_status_reported_as_a_boolean_follows_the_same_rule() -> None:
    resources = _k40()
    resources[FLAME] = _resource(FLAME, True, "booleanValue")
    flags = {
        item.resource_path: item.entity_registry_enabled_default
        for item in build_binary_sensor_descriptions(resources)
    }
    assert flags.get(FLAME) is False


@pytest.fixture
def legacy_setup():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


async def _set_up_k40(hass: HomeAssistant, prepare=None):
    build, gateway = PLANTS["k40"]
    entry = _entry(hass)
    registry = er.async_get(hass)
    if prepare is not None:
        prepare(registry, entry)
    stack = cloud_patches(build(), Clock(), gateway)
    with stack:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        states = {
            item.unique_id: item.disabled_by
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
            if item.domain == "sensor"
        }
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    return states


async def test_existing_entities_keep_their_setting(
    hass: HomeAssistant, legacy_setup
) -> None:
    """An installation from before 0.9.0 keeps the enabled totals; one the
    user disabled stays disabled; only the new one is created disabled."""

    def prepare(registry: er.EntityRegistry, entry) -> None:
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            "gateway-one:heatSources:numberOfStarts",
            config_entry=entry,
            disabled_by=None,
        )
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            "gateway-one:heatSources:workingTime:totalSystem",
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER,
        )

    states = await _set_up_k40(hass, prepare)
    assert states["gateway-one:heatSources:numberOfStarts"] is None
    assert (
        states["gateway-one:heatSources:workingTime:totalSystem"]
        is er.RegistryEntryDisabler.USER
    )
    assert (
        states["gateway-one:heatSources:flameStatus"]
        is er.RegistryEntryDisabler.INTEGRATION
    )


async def test_a_new_installation_creates_the_duplicates_disabled(
    hass: HomeAssistant, legacy_setup
) -> None:
    states = await _set_up_k40(hass)
    for path in sorted(DUPLICATES):
        unique_id = "gateway-one:" + path.strip("/").replace("/", ":")
        assert states[unique_id] is er.RegistryEntryDisabler.INTEGRATION, path
    assert states["gateway-one:heatSources:hs1:numberOfStarts:total"] is None


# ------------------------------------------- valves and hot water heat-up


def _moments() -> list[dict[str, object]]:
    return json.loads(VALVES.read_text(encoding="utf-8"))["moments"]


def _reading(raw: dict[str, object]):
    return from_local_observation(
        read(raw), connection_id="local", target_id="plant", current=True
    )


def _flag(signal: str, value: str) -> dict[str, object]:
    path = "/signals/" + signal
    return {"id": path, "type": "stringValue", "writeable": 0, "value": value}


def _heat_up(value: int) -> dict[str, object]:
    # As the controller declares it (capture of 2026-11-08).
    return {
        "id": "/signals/SRC.IntDHW.HeatUpActive",
        "type": "integerValue",
        "writeable": 0,
        "value": value,
        "state": {"HEAT": 1, "POSTHEAT": 2, "FALSE": 0},
    }


def _shown(raw: dict[str, object]) -> object:
    """The value the entity shows, in the form its answer gives it."""
    definition = definition_for(str(raw["id"]))
    assert definition is not None
    resolved = for_reading(definition, read(raw))
    assert resolved is not None
    return native_value(resolved, _reading(raw))


@pytest.mark.parametrize("moment", _moments(), ids=lambda item: item["time"])
def test_the_valves_show_the_recorded_positions(moment) -> None:
    """VR4 is on exactly while the heat pump defrosts, VW1 while the hot water
    side is served (a charge, or the last stage of a defrost)."""
    defrost = moment["refrigerant"] == "defrost"
    four_way = _shown(_flag("SRC.CUHP.HP1.FourwayValve", moment["four_way_valve"]))
    assert four_way == ("on" if defrost else "off")
    dhw = _shown(_flag("SRC.CUHP.HP1.DHWValve", moment["dhw_valve"]))
    assert (dhw == "on") is (moment["dhw_valve"] == "true")
    if moment["refrigerant"] == "dhw":
        assert dhw == "on"
    if "dhw_valve_relay" in moment:
        relay = _shown(_flag("SRC.RelayStatus.3WayValve", moment["dhw_valve_relay"]))
        assert relay == dhw
    heat_up = _shown(_heat_up(moment["heat_up"]))
    # The heat-up runs during a charge, not while the heat pump defrosts.
    assert heat_up == ("on" if moment["refrigerant"] == "dhw" else "off")


def test_the_recorded_moments_cover_defrost_and_hot_water() -> None:
    phases = {moment["refrigerant"] for moment in _moments()}
    assert phases == {"heating", "defrost", "dhw"}
    assert any(
        moment["refrigerant"] == "defrost" and moment["dhw_valve"] == "true"
        for moment in _moments()
    )


@pytest.mark.parametrize(
    ("value", "shown"), [(0, "off"), (1, "on"), (2, "on"), (3, None)]
)
def test_the_hot_water_heat_up_is_on_or_off(value, shown) -> None:
    definition = definition_for("/signals/SRC.IntDHW.HeatUpActive")
    assert (definition.kind, definition.options, definition.enabled) == (
        FLAG,
        ("on", "off"),
        False,
    )
    assert _shown(_heat_up(value)) == shown


def test_the_valve_relay_shows_the_valve_position() -> None:
    relay = _reading(_flag("SRC.RelayStatus.3WayValve", "true"))
    from custom_components.bosch_buderus_heating.local_plant_view import (
        local_sensor,
    )

    observation = read(_flag("SRC.RelayStatus.3WayValve", "true"))
    sensor, reason = local_sensor(observation)
    assert reason == "ok"
    assert sensor.definition.translation_key == "dhw_valve_position"
    assert native_value(sensor.definition, relay) == "on"


# ------------------------------------------------------- run times


@pytest.mark.parametrize(
    "path",
    [
        "/signals/SRC.CUHP.HP1.Stats.CompressorCH",
        "/signals/SRC.CUHP.HP1.Stats.CompressorDHW",
        "/signals/SRC.CUHP.HP1.Stats.CompressorCooling",
        "/signals/HYBMAN.OpTimeCompressorHeating",
        "/signals/HYBMAN.OpTimeCompressorDHW",
        "/ventilation/zone1/applianceRunTime",
    ],
)
def test_cumulative_run_times_have_statistics(path) -> None:
    definition = definition_for(path)
    assert definition.kind == DURATION and definition.counter
    assert state_class(definition) is SensorStateClass.TOTAL_INCREASING


@pytest.mark.parametrize(
    "path",
    [
        "/ventilation/zone1/filter/remainingTime",
        "/signals/HYBMAN.TimeTillNextCompressorStart",
        "/signals/HYBMAN.TimeTillNextCompressorStop",
    ],
)
def test_remaining_times_have_none(path) -> None:
    definition = definition_for(path)
    assert definition.kind == DURATION and not definition.counter
    assert state_class(definition) is None


def test_only_run_times_are_counters() -> None:
    counters = {item.key for item in CATALOG if item.counter}
    assert counters == {
        "ventilation_run_time",
        "hybrid_compressor_hours_heating",
        "hybrid_compressor_hours_hot_water",
        "compressor_run_time_heating",
        "compressor_run_time_hot_water",
        "compressor_run_time_cooling",
    }


# ------------------------------------- room controller settings


def _circuit() -> dict[str, Resource]:
    return {**load_tree("rrc/ct200-shapes.json"), **load_tree("rrc/circuit.json")}


@pytest.mark.parametrize(
    ("path", "enabled"),
    [
        # A single offered season mode (#122) and a setting that is not
        # writable: the sensor is the only place the value is shown.
        ("/heatingCircuits/hc2/seasonOptMode", True),
        ("/heatingCircuits/hc2/suWiThreshold", True),
        # A control exists: its read-only mirror stays disabled.
        ("/heatingCircuits/hc1/seasonOptMode", False),
        ("/heatingCircuits/hc1/suWiThreshold", False),
        # Never a control (switched off by the manufacturer): unchanged.
        ("/heatingCircuits/hc1/buildingHeatup", False),
    ],
)
def test_a_setting_without_a_control_shows_its_sensor(path, enabled) -> None:
    resources = _circuit()
    shown = {
        item.resource_path: item
        for item in build_sensor_descriptions(resources, rrc_plant=True)
    }
    description = shown[path]
    assert description.entity_registry_enabled_default is enabled
    assert description.entity_category is EntityCategory.DIAGNOSTIC
    assert setting_without_control(resources[path]) is enabled


def test_a_control_with_an_odd_current_value_keeps_its_mirror_disabled() -> None:
    # Only the current value does not fit: the control still exists, so the
    # answer does not change with a single reading.
    resources = _circuit()
    path = "/heatingCircuits/hc1/seasonOptMode"
    resources[path] = replace(resources[path], value="not_a_mode")
    assessment = assess_control(resources[path], RRC_CONTEXT)
    assert assessment.current_value_issue is not None
    assert not setting_without_control(resources[path])


def test_the_window_detection_temperature_without_a_control_is_shown() -> None:
    # Reported read only (writeable 0): no number, so the sensor shows it.
    resources = {
        **load_tree("rrc/ct200-shapes.json"),
        **load_tree("rrc/zone_climate.json"),
    }
    path = "/system/openWindowDetection/temperature"
    assert resources[path].metadata.writable is False
    description = next(
        item
        for item in build_sensor_descriptions(resources, rrc_plant=True)
        if item.resource_path == path
    )
    assert description.entity_registry_enabled_default is True
    assert description.entity_category is EntityCategory.DIAGNOSTIC
    # Writable, the number exists and the sensor is a disabled duplicate.
    resources[path] = replace(
        resources[path], metadata=replace(resources[path].metadata, writable=True)
    )
    description = next(
        item
        for item in build_sensor_descriptions(resources, rrc_plant=True)
        if item.resource_path == path
    )
    assert description.entity_registry_enabled_default is False


def test_entities_of_their_own_keep_their_mirror_disabled() -> None:
    # The system pressure has an entity of its own and no write rule.
    path = "/system/appliance/systemPressure"
    resources = load_tree("rrc/ct200-shapes.json")
    assert rrc_contracts.is_mirror(path)
    assert not setting_without_control(resources[path])
    description = next(
        item
        for item in build_sensor_descriptions(resources, rrc_plant=True)
        if item.resource_path == path
    )
    assert description.entity_registry_enabled_default is False


def test_without_a_reading_the_mirror_stays_disabled() -> None:
    resources = _circuit()
    path = "/heatingCircuits/hc2/seasonOptMode"
    description = next(
        item
        for item in build_sensor_descriptions(resources, rrc_plant=True)
        if item.resource_path == path
    )
    plain = generic_presentation(description, Platform.SENSOR)
    assert plain.entity_registry_enabled_default is False


async def test_an_existing_setting_sensor_stays_disabled(
    hass: HomeAssistant, legacy_setup
) -> None:
    """Only new entities follow the new default: an entry registered disabled
    before 0.9.0 is not enabled by the update."""
    from tests.test_rrc_circuit import CircuitCloud, _setup, _teardown, plant

    existing = "gateway-one:heatingCircuits:hc2:seasonOptMode"
    new = "gateway-one:heatingCircuits:hc2:suWiThreshold"
    entry = _entry(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        existing,
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    with patch("tests.test_rrc_circuit._entry", return_value=entry):
        entry, _coordinator, _sleeps, stack, _clock = await _setup(
            hass, CircuitCloud(plant())
        )
    try:
        old = registry.async_get(
            registry.async_get_entity_id("sensor", DOMAIN, existing)
        )
        assert old.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        fresh = registry.async_get(registry.async_get_entity_id("sensor", DOMAIN, new))
        assert fresh.disabled_by is None
        assert fresh.entity_category is EntityCategory.DIAGNOSTIC
    finally:
        await _teardown(hass, entry, stack)


# ------------------------------------------------- other defaults


def test_the_ventilation_fan_level_select_is_created_disabled() -> None:
    assert VENTILATION_FAN_LEVEL_POLICY.enabled_by_default is False
    path = "/ventilation/zone1/exhaustFanLevel"
    resources = {
        path: _resource(
            path,
            "nom",
            "stringValue",
            writable=True,
            allowed_values=("off", "min", "red", "nom", "max", "dem"),
        )
    }
    descriptions = build_select_descriptions(resources)
    assert descriptions
    assert all(not item.entity_registry_enabled_default for item in descriptions)


def test_the_room_controller_hot_water_level_is_the_hot_water_temperature() -> None:
    path = "/dhwCircuits/dhw1/temperatureLevels/high"
    assert rrc_contracts.name(path, "en") == "Hot water temperature"
    assert rrc_contracts.name(path, "de") == "Warmwassertemperatur"
    assert rrc_contracts.entity_key(path, "number") == "rrc_hot_water_temperature"
    assert LEVEL_HIGH.startswith("^")
    for name, expected in (
        ("strings.json", "Hot water temperature"),
        ("translations/en.json", "Hot water temperature"),
        ("translations/de.json", "Warmwassertemperatur"),
    ):
        texts = json.loads((COMPONENT / name).read_text(encoding="utf-8"))
        number = texts["entity"]["number"]
        assert number["rrc_hot_water_temperature"]["name"] == expected
        # Other gateways keep their comfort start temperature.
        assert "hot_water_comfort" in number


# ------------------------------------------------------------ names


def _names(language: str) -> list[str]:
    names = [
        definition.names[0 if language == "de" else 1]
        for definition in CATALOG
        if definition.key != "spec"
    ]
    texts = json.loads(
        (COMPONENT / "translations" / f"{language}.json").read_text(encoding="utf-8")
    )
    for platform in texts["entity"].values():
        names.extend(item["name"] for item in platform.values() if "name" in item)
    return names


@pytest.mark.parametrize("language", ["en", "de"])
def test_no_entity_name_says_pointt(language) -> None:
    assert [name for name in _names(language) if "PointT" in name] == []


def test_german_names_use_the_controllers_word_for_the_compressor() -> None:
    names = _names("de")
    assert [name for name in names if "Verdichter" in name] == []
    assert sum("Kompressor" in name for name in names) >= 15


def test_one_word_for_the_drip_pan() -> None:
    english = [name for name in _names("en") if PAN.search(name)]
    assert english
    assert all("Drip pan" in name for name in english), english


@pytest.mark.parametrize(
    "key",
    [
        "pointt_api_requests_total",
        "pointt_api_requests_last_hour",
        "pointt_api_response_time_last_hour",
    ],
)
def test_the_request_sensors_keep_their_keys(key) -> None:
    # Only the names change: the unique IDs and translation keys stay.
    for language in ("en", "de"):
        texts = json.loads(
            (COMPONENT / "translations" / f"{language}.json").read_text("utf-8")
        )
        assert texts["entity"]["sensor"][key]["name"].startswith("Cloud")


def test_a_reviewed_signal_keeps_its_unique_id() -> None:
    from custom_components.bosch_buderus_heating.local_only_sensor import (
        local_only_unique_id,
    )

    path = "/signals/SRC.IntDHW.HeatUpActive"
    assert local_only_unique_id("gw", path) == (
        "gw:local_only:signals:SRC.IntDHW.HeatUpActive"
    )
