"""Read-only heating and hot-water switch programs (PointT `switchPrograms/A`)."""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, PropertyMock, patch
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.bosch_buderus_heating import _remove_retired_entities
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.discovery import _optional_children
from custom_components.bosch_buderus_heating.holidays import HOLIDAY_TIMEZONE_PATH
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    PointTClient,
    Resource,
)
from custom_components.bosch_buderus_heating.pointt.models import ResourceMetadata
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.resource_catalog import (
    CapabilityMaturity,
    PollGroup,
    capability_maturity,
    poll_group,
    resource_name,
    supports_entity,
)
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    _track_resource_sensors,
    build_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.switch_programs import (
    DAYS,
    SwitchProgramAssessment,
    assess_switch_program,
    is_dhw_switch_program_path,
    is_switch_program_path,
)

FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures/contracts/connectkey/switch-programs.json"
    ).read_text(encoding="utf-8")
)
CASES = {case["id"]: case for case in FIXTURE["cases"]}
HEATING = CASES["heating_program_a"]["payload"]
DHW = CASES["dhw_program_a"]["payload"]


def _resource(payload: dict[str, Any]) -> Resource:
    return parse_resource(payload, path=payload["id"])


def _changed(payload: dict[str, Any], **changes: Any) -> Resource:
    data = copy.deepcopy(payload)
    for key, value in changes.items():
        if value is ...:
            data.pop(key, None)
        else:
            data[key] = value
    return _resource(data)


def _with_point(payload: dict[str, Any], point: object) -> Resource:
    data = copy.deepcopy(payload)
    data["switchPoints"] = [point]
    return _resource(data)


def test_fixture_provenance_is_anonymized() -> None:
    provenance = FIXTURE["provenance"]
    text = json.dumps(FIXTURE)

    assert provenance["kind"] == "anonymized_capture"
    assert provenance["physical_device_verified"] is True
    assert '"uri"' not in text
    assert "gateway" not in text.casefold().replace("mx400 gateway", "")


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_fixture_cases_are_read_as_expected(case_id: str) -> None:
    case = CASES[case_id]
    resource = _resource(case["payload"])
    assessment = assess_switch_program(resource)

    assert resource.unknown_fields == frozenset()
    assert assessment.supported
    assert list(assessment.levels) == case["expected"]["levels"]
    assert assessment.schedule()["Mo"] == case["expected"]["monday"]
    assert assessment.schedule()["Sa"] == case["expected"]["saturday"]
    assert assessment.max_switch_points == case["payload"]["maxNbOfSwitchPoints"]
    assert (
        assessment.max_switch_points_per_day
        == case["payload"]["maxNbOfSwitchPointsPerDay"]
    )
    assert assessment.time_raster == 15


def test_parser_retains_program_fields_only_for_switch_programs() -> None:
    program = _resource(HEATING)
    other = parse_resource(
        {
            "id": "/heatingCircuits/hc1/activeSwitchProgram",
            "type": "stringValue",
            "value": "A",
            "switchPoints": [],
        },
        path="/heatingCircuits/hc1/activeSwitchProgram",
    )

    assert set(program.program_fields) == {
        "setpointProperty",
        "maxNbOfSwitchPoints",
        "maxNbOfSwitchPointsPerDay",
        "switchPointTimeRaster",
        "switchPoints",
    }
    assert other.program_fields == {}
    assert other.unknown_fields == frozenset({"switchPoints"})


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/heatingCircuits/hc1/switchPrograms/A", True),
        ("/heatingCircuits/hc2/switchPrograms/B", True),
        ("/dhwCircuits/dhw1/switchPrograms/A", True),
        ("/heatingCircuits/hc1/switchPrograms/C", False),
        ("/heatingCircuits/hc0/switchPrograms/A", False),
        ("/heatingCircuits/hc1/switchPrograms/nameA", False),
        ("/heatingCircuits/hc1/switchPrograms", False),
        ("/dhwCircuits/dhw1/switchPrograms/cp", False),
        ("/zones/zone1/switchPrograms/A", False),
        ("/heatingCircuits/hc1/switchPrograms/A/", False),
    ],
)
def test_program_paths_are_exact(path: str, expected: bool) -> None:
    assert is_switch_program_path(path) is expected


def test_dhw_paths_are_recognized() -> None:
    assert is_dhw_switch_program_path("/dhwCircuits/dhw1/switchPrograms/A")
    assert not is_dhw_switch_program_path("/heatingCircuits/hc1/switchPrograms/A")
    assert not is_dhw_switch_program_path("/dhwCircuits/dhw1/switchPrograms/cp")


@pytest.mark.parametrize(
    ("resource", "reason"),
    [
        (Resource(path="/heatingCircuits/hc1/name"), "not_switch_program"),
        (_changed(HEATING, type="stringValue"), "unsupported_schema"),
        (_changed(HEATING, value="A"), "unsupported_schema"),
        (_changed(HEATING, values=[1]), "unsupported_schema"),
        (_changed(HEATING, extraField=1), "unsupported_schema"),
        (
            _changed(
                HEATING,
                references=[{"id": "/heatingCircuits/hc1/switchPrograms/A/x"}],
            ),
            "unsupported_schema",
        ),
        (_changed(HEATING, setpointProperty=...), "unsupported_setpoint_property"),
        (
            _changed(
                HEATING, setpointProperty={"id": "/dhwCircuits/dhw1/temperatureLevels"}
            ),
            "unsupported_setpoint_property",
        ),
        (_changed(HEATING, setpointProperty="levels"), "unsupported_setpoint_property"),
        (_changed(HEATING, maxNbOfSwitchPoints=0), "invalid_limits"),
        (_changed(HEATING, maxNbOfSwitchPoints=True), "invalid_limits"),
        (_changed(HEATING, maxNbOfSwitchPoints=1000), "invalid_limits"),
        (_changed(HEATING, maxNbOfSwitchPointsPerDay=...), "invalid_limits"),
        (_changed(HEATING, switchPointTimeRaster=7), "invalid_limits"),
        (_changed(HEATING, switchPointTimeRaster=15.0), "invalid_limits"),
        (_changed(HEATING, switchPoints=...), "missing_switch_points"),
        (_changed(HEATING, switchPoints={"Mo": []}), "missing_switch_points"),
        (_changed(HEATING, switchPoints=[]), "no_switch_points"),
        (_changed(HEATING, maxNbOfSwitchPoints=13), "too_many_switch_points"),
        (_changed(HEATING, maxNbOfSwitchPointsPerDay=1), "too_many_switch_points"),
    ],
)
def test_unexpected_shapes_are_not_supported(resource: Resource, reason: str) -> None:
    assert assess_switch_program(resource) == SwitchProgramAssessment(False, reason)


@pytest.mark.parametrize(
    "point",
    [
        "Mo 06:00",
        {"dayOfWeek": "Mo", "time": 360},
        {"dayOfWeek": "Mo", "time": 360, "setpoint": "eco", "extra": 1},
        {"dayOfWeek": "Monday", "time": 360, "setpoint": "eco"},
        {"dayOfWeek": 0, "time": 360, "setpoint": "eco"},
        {"dayOfWeek": "Mo", "time": -15, "setpoint": "eco"},
        {"dayOfWeek": "Mo", "time": 1440, "setpoint": "eco"},
        {"dayOfWeek": "Mo", "time": 365, "setpoint": "eco"},
        {"dayOfWeek": "Mo", "time": "06:00", "setpoint": "eco"},
        {"dayOfWeek": "Mo", "time": True, "setpoint": "eco"},
        {"dayOfWeek": "Mo", "time": 360, "setpoint": "Eco"},
        {"dayOfWeek": "Mo", "time": 360, "setpoint": "my program"},
        {"dayOfWeek": "Mo", "time": 360, "setpoint": 1},
        {"dayOfWeek": "Mo", "time": 360, "setpoint": "x" * 33},
    ],
)
def test_invalid_switch_points_are_rejected(point: object) -> None:
    assessment = assess_switch_program(_with_point(HEATING, point))

    assert assessment == SwitchProgramAssessment(False, "invalid_switch_point")


def test_duplicate_switch_points_are_rejected() -> None:
    data = copy.deepcopy(HEATING)
    data["switchPoints"] = [
        {"dayOfWeek": "Mo", "time": 360, "setpoint": "comfort2"},
        {"dayOfWeek": "Mo", "time": 360, "setpoint": "eco"},
    ]

    assert assess_switch_program(_resource(data)) == SwitchProgramAssessment(
        False, "duplicate_switch_point"
    )


def test_unsorted_points_keep_their_meaning() -> None:
    data = copy.deepcopy(HEATING)
    data["switchPoints"] = list(reversed(data["switchPoints"]))
    assessment = assess_switch_program(_resource(data))

    assert assessment.supported
    assert assessment.schedule()["Mo"] == ["06:00 comfort2", "21:30 eco"]
    assert list(assessment.schedule()) == ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


@pytest.mark.parametrize(
    ("day", "minute", "level", "next_minutes"),
    [
        (0, 0, "eco", 360),
        (0, 359, "eco", 1),
        (0, 360, "comfort2", 930),
        (0, 1289, "comfort2", 1),
        (0, 1290, "eco", 510),
        (4, 1439, "eco", 451),
        (6, 1439, "eco", 361),
    ],
)
def test_active_level_and_next_switch_follow_the_week(
    day: int, minute: int, level: str, next_minutes: int
) -> None:
    assessment = assess_switch_program(_resource(HEATING))

    assert assessment.active_level(day, minute) == level
    assert assessment.next_switch_in_minutes(day, minute) == next_minutes


def test_a_single_point_is_in_effect_all_week() -> None:
    data = copy.deepcopy(DHW)
    data["switchPoints"] = [{"time": 300, "setpoint": "low", "dayOfWeek": "We"}]
    assessment = assess_switch_program(_resource(data))

    assert assessment.active_level(0, 0) == "low"
    assert assessment.next_switch_in_minutes(2, 300) == 7 * 24 * 60
    assert assessment.next_switch_in_minutes(2, 299) == 1


def test_unsupported_assessment_has_no_level() -> None:
    assessment = SwitchProgramAssessment(False, "no_switch_points")

    assert assessment.active_level(0, 0) is None
    assert assessment.next_switch_in_minutes(0, 0) is None
    assert assessment.schedule() == {}
    assert assessment.levels == ()


def test_catalog_discovery_and_polling() -> None:
    program = _resource(DHW)
    empty = Resource(
        path="/dhwCircuits/dhw1/switchPrograms/A",
        metadata=ResourceMetadata(resource_type="switchProgram", writable=True),
    )

    assert "/dhwCircuits/dhw1/switchPrograms/A" in _optional_children(
        "/dhwCircuits/dhw1"
    )
    assert capability_maturity(program.path) is CapabilityMaturity.UNDERSTOOD
    assert poll_group(program) is PollGroup.SLOW
    assert supports_entity(program)
    assert not supports_entity(empty)
    assert resource_name(program.path) == "Zeitprogramm A"
    assert resource_name(program.path, language="en") == "Time program A"


def _program_sensor(
    hass: HomeAssistant, resource: Resource, *, available: bool = True
) -> BoschBuderusSensor:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass,
        AsyncMock(),
        Gateway("gateway-one", device_type="MX400", firmware_version="1.2.3"),
        entry,
    )
    coordinator.resources = {resource.path: resource}
    coordinator.update_interval = None
    coordinator.data = {
        resource.path: ResourceSnapshot(resource, available, datetime.now(UTC))
    }
    coordinator.last_update_success = True
    (description,) = build_sensor_descriptions(coordinator.resources)
    sensor = BoschBuderusSensor(coordinator, description)
    sensor.hass = hass
    sensor.entity_id = "sensor.time_program_a"
    return sensor


async def test_heating_sensor_state_and_attributes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to("2026-11-02 05:00:00+00:00")
    sensor = _program_sensor(hass, _resource(HEATING))
    description = sensor.entity_description

    assert description.value_kind == "switch_program"
    assert description.device_class is SensorDeviceClass.ENUM
    assert description.translation_key == "heating_switch_program"
    assert description.entity_category is EntityCategory.DIAGNOSTIC
    assert description.options == ["comfort1", "comfort2", "comfort3", "eco", "off"]
    assert description.native_unit_of_measurement is None
    assert description.state_class is None
    assert sensor.unique_id == "gateway-one:heatingCircuits:hc1:switchPrograms:A"
    assert sensor.native_value == "comfort2"
    attributes = sensor.extra_state_attributes
    assert attributes is not None
    assert attributes["switch_points"]["Mo"] == ["06:00 comfort2", "21:30 eco"]
    assert list(attributes["switch_points"]) == list(DAYS)
    assert attributes["max_switch_points"] == 14
    assert attributes["max_switch_points_per_day"] == 2
    assert attributes["time_raster_minutes"] == 15
    assert "uri" not in json.dumps(attributes)

    freezer.move_to("2026-11-02 04:59:00+00:00")
    assert sensor.native_value == "eco"


async def test_dhw_sensor_adds_device_specific_levels(hass: HomeAssistant) -> None:
    data = copy.deepcopy(DHW)
    data["switchPoints"][0]["setpoint"] = "boost1"
    sensor = _program_sensor(hass, _resource(data))

    assert sensor.entity_description.translation_key == "dhw_switch_program"
    assert sensor.entity_description.options == [
        "low",
        "eco",
        "high",
        "off",
        "boost1",
    ]


async def test_unavailable_program_has_no_state(hass: HomeAssistant) -> None:
    sensor = _program_sensor(hass, _resource(HEATING), available=False)

    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None
    sensor._update_switch_timer()
    assert sensor._switch_timer is None


async def test_schema_change_after_creation_hides_attributes(
    hass: HomeAssistant,
) -> None:
    sensor = _program_sensor(hass, _resource(HEATING))
    broken = _changed(HEATING, switchPoints=[])
    sensor.coordinator.data = {
        broken.path: ResourceSnapshot(broken, True, datetime.now(UTC))
    }

    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None
    sensor._update_switch_timer()
    assert sensor._switch_timer is None


async def test_state_refreshes_at_the_next_switch_point(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to("2026-11-02 04:58:30+00:00")
    sensor = _program_sensor(hass, _resource(HEATING))
    sensor.async_write_ha_state = Mock()  # type: ignore[method-assign]

    await sensor.async_added_to_hass()
    assert sensor._switch_timer is not None
    assert sensor.native_value == "eco"

    freezer.move_to("2026-11-02 04:59:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    sensor.async_write_ha_state.assert_not_called()

    freezer.move_to("2026-11-02 05:00:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert sensor.async_write_ha_state.call_count == 1
    assert sensor.native_value == "comfort2"
    assert sensor._switch_timer is not None

    sensor._handle_coordinator_update()
    assert sensor._switch_timer is not None

    sensor._cancel_switch_timer()
    assert sensor._switch_timer is None
    sensor._cancel_switch_timer()


async def test_invalid_programs_create_no_sensor() -> None:
    broken = _changed(HEATING, switchPoints=[])

    assert build_sensor_descriptions({broken.path: broken}) == ()


async def test_known_programs_are_not_retired(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    valid = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:heatingCircuits:hc1:switchPrograms:A",
        config_entry=entry,
    )
    empty = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:dhwCircuits:dhw1:switchPrograms:A",
        config_entry=entry,
    )
    program = _resource(HEATING)
    empty_program = Resource(
        path="/dhwCircuits/dhw1/switchPrograms/A",
        metadata=ResourceMetadata(resource_type="switchProgram", writable=True),
    )
    coordinator = SimpleNamespace(
        gateway=Gateway("gateway-one"),
        resources={program.path: program, empty_program.path: empty_program},
    )

    _remove_retired_entities(hass, entry, (coordinator,))

    assert registry.async_get(valid.entity_id) is not None
    assert registry.async_get(empty.entity_id) is not None


@pytest.mark.parametrize("invalid", ["empty", "malformed", "missing"])
@pytest.mark.parametrize("disabled_by", [None, er.RegistryEntryDisabler.USER])
async def test_program_registry_settings_survive_invalid_startup_and_recovery(
    hass: HomeAssistant,
    invalid: str,
    disabled_by: er.RegistryEntryDisabler | None,
) -> None:
    sensor = _program_sensor(hass, _resource(HEATING))
    coordinator = sensor.coordinator
    entry = coordinator.config_entry
    assert entry is not None
    registry = er.async_get(hass)
    registered = registry.async_get_or_create(
        "sensor", DOMAIN, sensor.unique_id, config_entry=entry
    )
    registry.async_update_entity(
        registered.entity_id,
        name="My schedule",
        icon="mdi:clock",
        disabled_by=disabled_by,
    )
    previous = registry.async_get(registered.entity_id)
    broken = _changed(
        HEATING, switchPoints=[] if invalid == "empty" else [{"time": 360}]
    )
    coordinator.resources = {} if invalid == "missing" else {broken.path: broken}
    coordinator.data = {broken.path: ResourceSnapshot(broken, True, datetime.now(UTC))}
    _remove_retired_entities(hass, entry, (coordinator,))
    assert registry.async_get(registered.entity_id) == previous

    added: list[BoschBuderusSensor] = []
    _track_resource_sensors(entry, coordinator, added.extend)
    assert added == []
    recovered = _resource(HEATING)
    coordinator.resources[recovered.path] = recovered
    coordinator.async_set_updated_data(
        {recovered.path: ResourceSnapshot(recovered, True, datetime.now(UTC))}
    )
    assert len(added) == 1
    assert added[0].unique_id == sensor.unique_id
    assert registry.async_get(registered.entity_id) == previous


async def test_obsolete_unknown_program_preview_is_still_retired(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    obsolete = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "gateway-one:dhwCircuits:dhw1:switchPrograms:cp",
        config_entry=entry,
    )
    resource = Resource(
        path="/dhwCircuits/dhw1/switchPrograms/cp",
        metadata=ResourceMetadata(resource_type="switchProgram"),
    )
    coordinator = SimpleNamespace(
        gateway=Gateway("gateway-one"), resources={resource.path: resource}
    )
    _remove_retired_entities(hass, entry, (coordinator,))
    assert registry.async_get(obsolete.entity_id) is None


@pytest.mark.parametrize(
    ("ha_zone", "gateway_zone", "expected"),
    [
        ("UTC", "Europe/Berlin", "comfort2"),
        ("US/Pacific", "Europe/Berlin", "comfort2"),
        ("Europe/Berlin", "UTC", "eco"),
        ("UTC", "+05:45", "comfort2"),
        ("Europe/Berlin", "not/a/timezone", "comfort2"),
        ("UTC", None, "eco"),
    ],
)
async def test_program_uses_gateway_timezone_and_validated_fallback(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    ha_zone: str,
    gateway_zone: str | None,
    expected: str,
) -> None:
    await hass.config.async_set_time_zone(ha_zone)
    freezer.move_to("2026-11-02 05:30:00+00:00")
    sensor = _program_sensor(hass, _resource(HEATING))
    if gateway_zone is not None:
        resource = parse_resource(
            {"id": HOLIDAY_TIMEZONE_PATH, "type": "stringValue", "value": gateway_zone}
        )
        sensor.coordinator.resources[resource.path] = resource
        if gateway_zone in {"Europe/Berlin", "UTC"}:
            sensor.coordinator.holiday_timezones[gateway_zone] = ZoneInfo(gateway_zone)
    assert sensor.native_value == expected


@pytest.mark.parametrize(
    ("start", "points", "ticks", "expected"),
    [
        (
            "2026-10-25 00:29:30+00:00",
            [(90, "eco"), (150, "comfort2")],
            [
                "2026-10-25 00:30:00+00:00",
                "2026-10-25 00:31:00+00:00",
                "2026-10-25 01:00:00+00:00",
                "2026-10-25 01:15:00+00:00",
                "2026-10-25 01:30:00+00:00",
                "2026-10-25 01:31:00+00:00",
            ],
            ["comfort2", "eco", "comfort2"],
        ),
        (
            "2026-10-25 01:15:00+00:00",
            [(90, "eco"), (150, "comfort2")],
            [
                "2026-10-25 01:16:00+00:00",
                "2026-10-25 01:30:00+00:00",
            ],
            ["comfort2"],
        ),
        (
            "2027-03-28 00:59:30+00:00",
            [(150, "comfort2"), (195, "eco")],
            [
                "2027-03-28 01:00:00+00:00",
                "2027-03-28 01:15:00+00:00",
                "2027-03-28 01:16:00+00:00",
            ],
            ["comfort2", "eco"],
        ),
    ],
)
async def test_real_timer_handles_clock_changes_without_repeated_state_writes(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    start: str,
    points: list[tuple[int, str]],
    ticks: list[str],
    expected: list[str],
) -> None:
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to(start)
    payload = copy.deepcopy(HEATING)
    payload["switchPoints"] = [
        {"dayOfWeek": "Su", "time": minute, "setpoint": level}
        for minute, level in points
    ]
    sensor = _program_sensor(hass, _resource(payload))
    observed = []
    sensor.async_write_ha_state = Mock(  # type: ignore[method-assign]
        side_effect=lambda: observed.append(sensor.native_value)
    )
    await sensor.async_added_to_hass()
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert observed == []
    for tick in ticks:
        freezer.move_to(tick)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert observed == expected
    assert sensor.coordinator.client.mock_calls == []

    await sensor.async_remove()
    freezer.tick(60)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert observed == expected
    assert sensor._switch_timer is None


async def test_program_levels_refresh_after_polling_without_recreating_entity(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-11-02 06:00:00+00:00")
    sensor = _program_sensor(hass, _resource(DHW))
    assert sensor.options == ["low", "eco", "high", "off"]
    original_id = sensor.unique_id
    sensor.entity_description = replace(
        sensor.entity_description, entity_registry_enabled_default=True
    )
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    with patch.object(
        type(sensor), "device_info", new_callable=PropertyMock, return_value=None
    ):
        await component.async_add_entities([sensor])
    for level in ("boost1", "boost2"):
        payload = copy.deepcopy(DHW)
        payload["switchPoints"][0]["setpoint"] = level
        updated = _resource(payload)
        sensor.coordinator.resources[updated.path] = updated
        sensor.coordinator.async_set_updated_data(
            {updated.path: ResourceSnapshot(updated, True, datetime.now(UTC))}
        )
        assert sensor.unique_id == original_id
        state = hass.states.get(sensor.entity_id)
        assert state is not None
        assert state.state == level
        assert state.attributes["options"] == ["low", "eco", "high", "off", level]

    payload["switchPoints"][0]["setpoint"] = "invalid level"
    invalid = _resource(payload)
    sensor.coordinator.async_set_updated_data(
        {invalid.path: ResourceSnapshot(invalid, True, datetime.now(UTC))}
    )
    state = hass.states.get(sensor.entity_id)
    assert state is not None
    assert state.state == "unknown"
    assert "invalid level" not in sensor.options
    assert sensor._switch_timer is None

    for available in (True, False, True):
        sensor.coordinator.async_set_updated_data(
            {updated.path: ResourceSnapshot(updated, available, datetime.now(UTC))}
        )
        state = hass.states.get(sensor.entity_id)
        assert state is not None
        assert state.state == ("boost2" if available else "unavailable")
        assert (sensor._switch_timer is not None) is available
    assert sensor.coordinator.client.mock_calls == []
    await sensor.async_remove()


async def test_diagnostics_contain_no_switch_times(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    client = PointTClient(AsyncMock(), "synthetic-token")
    gateway = Gateway("synthetic-gateway")
    coordinator = BoschBuderusDataUpdateCoordinator(hass, client, gateway, entry)
    heating, dhw = _resource(HEATING), _resource(DHW)
    coordinator.resources = {heating.path: heating, dhw.path: dhw}
    coordinator.data = {
        resource.path: ResourceSnapshot(resource, True, datetime.now(UTC))
        for resource in (heating, dhw)
    }
    entry.runtime_data = BoschBuderusRuntimeData(
        client, AsyncMock(), (gateway,), (coordinator,)
    )

    report = json.dumps(await async_get_config_entry_diagnostics(hass, entry))

    for private in ("switchPoints", "dayOfWeek", "comfort2", '"time"', "1290"):
        assert private not in report
