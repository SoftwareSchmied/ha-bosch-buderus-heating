"""Cloud readings stay valid for three polling intervals of their group (P12)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.binary_sensor import (
    BoschBuderusBinarySensor,
    build_binary_sensor_descriptions,
)
from custom_components.bosch_buderus_heating.const import DOMAIN, PollingProfile
from custom_components.bosch_buderus_heating.coordinator import (
    EXPIRED_CATEGORY,
    SOURCE_STOPPED_CATEGORY,
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.observations import (
    pair_observations,
    pair_summary,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    InvalidPayload,
    RequestMetrics,
    Resource,
    ResourceError,
    ResourceMetadata,
    ResourceNotFound,
    ResourceOmitted,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import RequestTimeout
from custom_components.bosch_buderus_heating.resource_catalog import (
    PollGroup,
    poll_group,
)
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from tests.ha_test_helpers import (
    MockConfigEntry,
    async_fire_time_changed,
)
from tests.test_local_selection import TARGET, FakeRuntime, local

RETURN = "/heatSources/returnTemperature"
OUTDOOR = "/system/sensors/temperatures/outdoor_t1"
FIRMWARE = "/gateway/versionHardware"  # static; the firmware is read slowly (P22)
DEFROST = "/heatSources/hs2/defrostActive"


def _temperature(path: str, value: float = 30.0) -> Resource:
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue", unit="C"),
    )


FIRMWARE_RESOURCE = Resource(
    FIRMWARE,
    value="1.2.3",
    has_value=True,
    metadata=ResourceMetadata(resource_type="stringValue"),
)
DEFROST_RESOURCE = Resource(
    DEFROST,
    value=True,
    has_value=True,
    metadata=ResourceMetadata(resource_type="booleanValue"),
)


def _coordinator(
    hass: HomeAssistant,
    *resources: Resource,
    received: datetime | None = None,
    profile: PollingProfile = PollingProfile.STANDARD,
) -> BoschBuderusDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type="MX400"), entry, profile
    )
    coordinator.update_interval = None
    received = received or datetime.now(UTC)
    coordinator.resources = {item.path: item for item in resources}
    coordinator.data = {
        item.path: ResourceSnapshot(item, True, received) for item in resources
    }
    coordinator.last_update_success = True
    return coordinator


def _sensor(coordinator: BoschBuderusDataUpdateCoordinator) -> BoschBuderusSensor:
    (description,) = build_sensor_descriptions(coordinator.resources)
    return BoschBuderusSensor(coordinator, description)


def _sensor_for(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> BoschBuderusSensor:
    description = next(
        item
        for item in build_sensor_descriptions(coordinator.resources)
        if item.resource_path == path
    )
    return BoschBuderusSensor(coordinator, description)


def test_fixture_paths_use_the_expected_polling_groups() -> None:
    assert poll_group(_temperature(RETURN)) is PollGroup.FAST
    assert poll_group(_temperature(OUTDOOR)) is PollGroup.SLOW
    assert poll_group(FIRMWARE_RESOURCE) is PollGroup.STATIC


@pytest.mark.parametrize(
    ("profile", "fast", "slow"),
    [
        (PollingProfile.STANDARD, timedelta(minutes=3), timedelta(minutes=45)),
        (PollingProfile.CLOUD_FRIENDLY, timedelta(minutes=6), timedelta(minutes=90)),
    ],
)
async def test_limit_is_three_intervals_of_the_group_and_static_never_expires(
    hass: HomeAssistant, profile, fast, slow
) -> None:
    resources = (_temperature(RETURN), _temperature(OUTDOOR), FIRMWARE_RESOURCE)
    coordinator = _coordinator(hass, *resources, profile=profile)
    data = coordinator.data
    assert coordinator.validity_limit(data[RETURN]) == fast
    assert coordinator.validity_limit(data[OUTDOOR]) == slow
    assert coordinator.validity_limit(data[FIRMWARE]) is None
    runtime = coordinator.diagnostics_summary()
    assert runtime["validity_limits_seconds"]["fast"] == fast.total_seconds()
    assert "static" not in runtime["validity_limits_seconds"]


async def test_expired_reading_is_only_hidden_in_the_displayed_copy(
    hass: HomeAssistant,
) -> None:
    now = datetime.now(UTC)
    coordinator = _coordinator(
        hass,
        _temperature(RETURN),
        _temperature(OUTDOOR),
        FIRMWARE_RESOURCE,
        received=now - timedelta(minutes=4),
    )
    stored = coordinator.data[RETURN]
    shown = coordinator.display_snapshot(RETURN)
    assert shown is not None and not shown.available
    assert shown.freshness is Freshness.STALE
    assert shown.last_error_category == EXPIRED_CATEGORY
    assert shown.resource == stored.resource
    # Stored data, writes and the polling plan keep the original snapshot.
    assert coordinator.data[RETURN] is stored and stored.available
    # Slower groups and static resources are still valid.
    assert coordinator.display_snapshot(OUTDOOR) is coordinator.data[OUTDOOR]
    assert coordinator.display_snapshot(FIRMWARE) is coordinator.data[FIRMWARE]
    assert coordinator.diagnostics_summary()["resources_expired"] == 1


async def test_reading_from_a_clock_set_back_no_longer_counts(
    hass: HomeAssistant,
) -> None:
    ahead = datetime.now(UTC) + timedelta(minutes=2)
    coordinator = _coordinator(hass, _temperature(RETURN), received=ahead)
    assert not coordinator.display_snapshot(RETURN).available
    coordinator.data[RETURN] = replace(
        coordinator.data[RETURN], last_success=datetime.now(UTC) + timedelta(seconds=30)
    )
    assert coordinator.display_snapshot(RETURN).available


async def test_failed_cloud_cycle_keeps_valid_readings_until_they_expire(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, _temperature(RETURN))
    sensor = _sensor(coordinator)
    coordinator.last_update_success = False  # failed, paused or circuit open
    assert sensor.available and sensor.native_value == 30.0
    coordinator.data[RETURN] = replace(
        coordinator.data[RETURN],
        last_success=datetime.now(UTC) - timedelta(minutes=3, seconds=1),
    )
    assert not sensor.available
    # An explicit failure of the reading itself still ends it immediately.
    coordinator.data[RETURN] = replace(
        coordinator.data[RETURN], last_success=datetime.now(UTC), available=False
    )
    assert not sensor.available


async def test_current_local_reading_survives_a_whole_cloud_outage(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(
        hass,
        _temperature(RETURN),
        received=datetime.now(UTC) - timedelta(hours=2),
    )
    sensor = _sensor(coordinator)
    coordinator.last_update_success = False
    assert not sensor.available
    runtime = FakeRuntime([local(RETURN, 31.5)])
    coordinator.local_selection = LocalSelection(runtime, TARGET)
    assert sensor.available and sensor.native_value == 31.5
    runtime.observations = []
    assert not sensor.available


async def test_binary_sensor_follows_the_same_rule(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass, DEFROST_RESOURCE)
    description = next(
        item
        for item in build_binary_sensor_descriptions(coordinator.resources)
        if item.value_key is None
    )
    sensor = BoschBuderusBinarySensor(coordinator, description)
    coordinator.last_update_success = False
    assert sensor.available and sensor.is_on is True
    limit = coordinator.validity_limit(coordinator.data[DEFROST])
    assert limit is not None
    coordinator.data[DEFROST] = replace(
        coordinator.data[DEFROST],
        last_success=datetime.now(UTC) - limit - timedelta(seconds=1),
    )
    assert not sensor.available


async def test_expiry_becomes_visible_by_timer_without_a_request(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    coordinator = _coordinator(hass, _temperature(RETURN), FIRMWARE_RESOURCE)
    listener = Mock()
    remove = coordinator.async_add_listener(listener)
    coordinator.async_update_listeners()
    listener.reset_mock()
    freezer.tick(timedelta(minutes=2, seconds=59))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    listener.assert_not_called()
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    listener.assert_called_once()
    assert not coordinator.display_snapshot(RETURN).available
    # Only the static reading is left, which never expires: no further timer.
    assert coordinator._validity_unsub is None
    assert coordinator.client.mock_calls == []
    remove()
    await coordinator.async_shutdown()


async def test_no_timer_without_listeners_and_none_after_shutdown_or_stop(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, _temperature(RETURN))
    coordinator.async_update_listeners()
    assert coordinator._validity_unsub is None
    remove = coordinator.async_add_listener(Mock())
    coordinator.async_update_listeners()
    assert coordinator._validity_unsub is not None
    await coordinator.async_shutdown()
    assert coordinator._validity_unsub is None
    assert coordinator._validity_stop_unsub is None

    second = _coordinator(hass, _temperature(RETURN))
    second.async_add_listener(Mock())
    second.async_update_listeners()
    assert second._validity_unsub is not None
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert second._validity_unsub is None
    remove()


def test_pair_summary_counts_unit_combinations_without_values() -> None:
    from tests.test_local_api import scalar
    from tests.test_observations import cloud
    from tests.test_observations import local as local_observation

    summary = pair_summary(
        pair_observations(
            [cloud(RETURN, 30.0, unit="C")],
            [local_observation(scalar(RETURN, value=30.0, unitOfMeasure="K"))],
        )
    )
    assert summary["incomparable_reasons"] == {"unit_differs": 1}
    assert summary["incomparable_units"] == {"C|K": 1}
    assert "30" not in repr(summary["incomparable_units"])


# ------------------------------------------------- real polling cycles


def _polling(
    hass: HomeAssistant, *resources: Resource, group: PollGroup = PollGroup.FAST
) -> tuple[BoschBuderusDataUpdateCoordinator, AsyncMock]:
    client = AsyncMock()
    client.metrics = RequestMetrics()
    coordinator = _coordinator(hass, *resources)
    coordinator.client = client
    coordinator._paths_by_group = {group: tuple(item.path for item in resources)}
    return coordinator, client


async def test_whole_request_failure_keeps_the_last_good_value(
    hass: HomeAssistant,
) -> None:
    coordinator, client = _polling(hass, _temperature(RETURN))
    sensor = _sensor(coordinator)
    client.get_resources_bulk.side_effect = RequestTimeout()
    client.get_resource.side_effect = RequestTimeout()
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert not coordinator.data[RETURN].available  # stored attempt result
    assert sensor.available and sensor.native_value == 30.0
    # Without a later successful read, the value still ends at its limit.
    coordinator.data[RETURN] = replace(
        coordinator.data[RETURN],
        last_success=datetime.now(UTC) - timedelta(minutes=4),
    )
    assert not sensor.available


@pytest.mark.parametrize("status", [429, 502, 503])
async def test_rate_limit_and_server_errors_keep_the_last_good_value(
    hass: HomeAssistant, status: int
) -> None:
    coordinator, client = _polling(hass, _temperature(RETURN))
    sensor = _sensor(coordinator)
    client.get_resources_bulk.return_value = (
        BatchItemResult(
            "gateway-one", RETURN, status, error=ResourceError(RETURN, status)
        ),
    )
    client.get_resource.side_effect = ServiceUnavailable()
    await coordinator.async_refresh()
    assert sensor.available and sensor.native_value == 30.0


@pytest.mark.parametrize("status", [403, 404])
async def test_explicit_answer_for_the_reading_ends_it_at_once(
    hass: HomeAssistant, status: int
) -> None:
    coordinator, client = _polling(
        hass, _temperature(RETURN), _temperature("/heatSources/actualModulation")
    )
    sensor = _sensor_for(coordinator, RETURN)
    client.get_resources_bulk.return_value = (
        BatchItemResult(
            "gateway-one", RETURN, status, error=ResourceError(RETURN, status)
        ),
        BatchItemResult(
            "gateway-one",
            "/heatSources/actualModulation",
            200,
            resource=_temperature("/heatSources/actualModulation", 12.0),
        ),
    )
    await coordinator.async_refresh()
    assert not sensor.available


async def test_consecutive_failed_cycles_still_publish_explicit_failures(
    hass: HomeAssistant,
) -> None:
    coordinator, client = _polling(hass, _temperature(RETURN))
    listener = Mock()
    remove = coordinator.async_add_listener(listener)
    client.get_resources_bulk.side_effect = RequestTimeout()
    client.get_resource.side_effect = RequestTimeout()
    await coordinator.async_refresh()
    calls = listener.call_count
    # Still a failed cycle, but the fallback read answers 404 for the reading.
    client.get_resource.side_effect = ResourceNotFound(RETURN, 404)
    coordinator._next_update = {}
    coordinator._negative_until.clear()
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert listener.call_count > calls
    assert not coordinator.display_snapshot(RETURN).available
    remove()
    await coordinator.async_shutdown()


async def test_deferred_and_stopped_readings(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass, _temperature(RETURN))
    stored = coordinator.data[RETURN]
    coordinator.data[RETURN] = replace(
        stored,
        available=False,
        freshness=Freshness.STALE,
        last_error_category="poll_deferred",
    )
    assert coordinator.display_snapshot(RETURN).available
    coordinator.data[RETURN] = replace(
        stored,
        available=False,
        freshness=Freshness.STALE,
        last_error_category=SOURCE_STOPPED_CATEGORY,
    )
    assert not coordinator.display_snapshot(RETURN).available


async def test_limit_follows_the_scheduled_group_and_the_cycle_duration(
    hass: HomeAssistant,
) -> None:
    coordinator, _ = _polling(hass, _temperature(RETURN), group=PollGroup.CONTROL)
    snapshot = coordinator.data[RETURN]
    assert poll_group(snapshot.resource) is PollGroup.FAST
    assert coordinator.validity_limit(snapshot) == timedelta(minutes=15)
    coordinator._paths_by_group = {}
    coordinator._note_cycle_duration(20_000.0)
    assert coordinator.validity_limit(snapshot) == timedelta(seconds=240)


def test_unknown_unit_text_is_not_copied_into_diagnostics() -> None:
    from tests.test_local_api import scalar
    from tests.test_observations import cloud
    from tests.test_observations import local as local_observation

    summary = pair_summary(
        pair_observations(
            [cloud(RETURN, 30.0, unit="x|private")],
            [local_observation(scalar(RETURN, value=30.0, unitOfMeasure="C"))],
        )
    )
    assert summary["incomparable_units"] == {"other|C": 1}
    assert "private" not in repr(summary)


async def test_explicit_end_survives_a_later_whole_request_failure(
    hass: HomeAssistant,
) -> None:
    coordinator, client = _polling(hass, _temperature(RETURN))
    sensor = _sensor(coordinator)
    client.get_resources_bulk.return_value = (
        BatchItemResult(
            "gateway-one", RETURN, 404, error=ResourceNotFound(RETURN, 404)
        ),
    )
    await coordinator.async_refresh()
    assert not sensor.available
    # Next cycle: the whole request times out; the old value must stay ended.
    coordinator._next_update = {}
    coordinator._negative_until.clear()
    client.get_resources_bulk.side_effect = RequestTimeout()
    client.get_resource.side_effect = RequestTimeout()
    await coordinator.async_refresh()
    assert coordinator.data[RETURN].last_error_category == "no_answer_requesttimeout"
    assert not sensor.available
    # A deferral keeps the marker as well.
    coordinator.data[RETURN] = replace(
        coordinator.data[RETURN], last_error_category="poll_deferred"
    )
    assert not sensor.available
    # Only a successful read clears it.
    client.get_resources_bulk.side_effect = None
    client.get_resources_bulk.return_value = (
        BatchItemResult(
            "gateway-one", RETURN, 200, resource=_temperature(RETURN, 31.0)
        ),
    )
    coordinator._next_update = {}
    coordinator._negative_until.clear()
    await coordinator.async_refresh()
    assert sensor.available and sensor.native_value == 31.0


async def test_stopped_source_is_not_shown_through_local_values(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, _temperature(RETURN))
    sensor = _sensor(coordinator)
    coordinator.local_selection = LocalSelection(
        FakeRuntime([local(RETURN, 31.5)]), TARGET
    )
    assert sensor.available and sensor.native_value == 31.5
    coordinator.data[RETURN] = replace(
        coordinator.data[RETURN],
        available=False,
        freshness=Freshness.STALE,
        last_error_category=SOURCE_STOPPED_CATEGORY,
        ended=True,
    )
    assert not sensor.available


async def test_combined_sensors_keep_the_strict_rule(hass: HomeAssistant) -> None:
    from tests.test_sensor import _dew_point_sensor

    sensor = _dew_point_sensor(hass)
    assert sensor.available
    sensor.coordinator.last_update_success = False
    assert not sensor.available


async def test_cycle_extension_is_capped_at_one_interval(
    hass: HomeAssistant,
) -> None:
    coordinator, _ = _polling(hass, _temperature(RETURN))
    coordinator._note_cycle_duration(3_600_000.0)
    assert coordinator.validity_limit(coordinator.data[RETURN]) == timedelta(minutes=6)


# ------------------------------------------------- final review corrections


@pytest.mark.parametrize(
    "message",
    [
        "Bulk response must be an array",
        "Bulk gateway item must be an object",
        "PointT response was not valid JSON",
    ],
)
async def test_unusable_whole_response_keeps_the_last_good_value(
    hass: HomeAssistant, message: str
) -> None:
    coordinator, client = _polling(hass, _temperature(RETURN))
    sensor = _sensor(coordinator)
    client.get_resources_bulk.side_effect = InvalidPayload(message)
    # The bounded single read gets no answer either.
    client.get_resource.side_effect = RequestTimeout()
    await coordinator.async_refresh()
    stored = coordinator.data[RETURN]
    assert not stored.available and not stored.ended
    assert stored.last_error_category == "no_answer_invalidpayload"
    assert sensor.available and sensor.native_value == 30.0


async def test_reading_omitted_from_the_response_keeps_its_value(
    hass: HomeAssistant,
) -> None:
    coordinator, client = _polling(
        hass, _temperature(RETURN), _temperature("/heatSources/actualModulation")
    )
    sensor = _sensor_for(coordinator, RETURN)
    client.get_resources_bulk.return_value = (
        BatchItemResult(
            "gateway-one",
            RETURN,
            None,
            error=ResourceOmitted("Bulk response omitted a requested path"),
        ),
        BatchItemResult(
            "gateway-one",
            "/heatSources/actualModulation",
            200,
            resource=_temperature("/heatSources/actualModulation", 12.0),
        ),
    )
    # The bounded single read for the omitted path fails as well.
    client.get_resource.side_effect = ServiceUnavailable()
    await coordinator.async_refresh()
    assert sensor.available and sensor.native_value == 30.0


async def test_item_level_401_is_not_an_answer_for_the_reading(
    hass: HomeAssistant,
) -> None:
    coordinator, client = _polling(
        hass, _temperature(RETURN), _temperature("/heatSources/actualModulation")
    )
    sensor = _sensor_for(coordinator, RETURN)
    client.get_resources_bulk.return_value = (
        BatchItemResult("gateway-one", RETURN, 401, error=ResourceError(RETURN, 401)),
        BatchItemResult(
            "gateway-one",
            "/heatSources/actualModulation",
            200,
            resource=_temperature("/heatSources/actualModulation", 12.0),
        ),
    )
    await coordinator.async_refresh()
    assert sensor.available


@pytest.mark.parametrize("category", ["http_503", "poll_deferred", "no_answer_x"])
async def test_dew_point_does_not_keep_failed_inputs(
    hass: HomeAssistant, category: str
) -> None:
    from tests.test_sensor import _dew_point_sensor

    sensor = _dew_point_sensor(hass)
    assert sensor.available
    data = sensor.coordinator.data
    humidity = next(path for path in data if path.endswith("actualHumidity"))
    data[humidity] = replace(
        data[humidity],
        available=False,
        freshness=Freshness.STALE,
        last_error_category=category,
    )
    assert not sensor.available


async def test_low_salt_combines_stored_readings_with_the_strict_rule(
    hass: HomeAssistant,
) -> None:
    from tests.test_water_softener import reading

    salt = reading("sensor/saltLevel", 10, "%")
    alarm = reading("sensor/saltLevelAlarm", 20, "%")
    coordinator = _coordinator(hass, salt, alarm)
    description = next(
        item
        for item in build_binary_sensor_descriptions(coordinator.resources)
        if item.value_key == "low_salt"
    )
    sensor = BoschBuderusBinarySensor(coordinator, description)
    assert sensor.available
    coordinator.data[alarm.path] = replace(
        coordinator.data[alarm.path],
        available=False,
        freshness=Freshness.STALE,
        last_error_category="http_503",
    )
    assert not sensor.available
    coordinator.data[alarm.path] = replace(
        coordinator.data[alarm.path],
        available=True,
        freshness=Freshness.FRESH,
        last_error_category=None,
    )
    assert sensor.available
    coordinator.last_update_success = False
    assert not sensor.available


# ------------------------------------------------- last review corrections


def _parsed(*items: dict, paths: tuple[str, ...]) -> tuple[BatchItemResult, ...]:
    from custom_components.bosch_buderus_heating.pointt.parsers import (
        parse_batch_response,
    )

    return parse_batch_response(
        [{"gatewayId": "gateway-one", "resourcePaths": list(items)}],
        gateway_id="gateway-one",
        requested_paths=paths,
    )


def _ok_item(path: str, value: float) -> dict:
    return {
        "resourcePath": path,
        "serverStatus": 200,
        "gatewayResponse": {
            "status": 200,
            "payload": {
                "id": path,
                "type": "floatValue",
                "value": value,
                "unitOfMeasure": "C",
            },
        },
    }


MODULATION = "/heatSources/actualModulation"


@pytest.mark.parametrize(
    "item",
    [
        {"resourcePath": RETURN, "serverStatus": "garbage"},
        {"resourcePath": RETURN, "serverStatus": 200},
        {"resourcePath": RETURN, "serverStatus": 200, "gatewayResponse": {}},
    ],
)
async def test_unreadable_item_is_invalid_data_for_the_reading(
    hass: HomeAssistant, item: dict
) -> None:
    coordinator, client = _polling(hass, _temperature(RETURN), _temperature(MODULATION))
    sensor = _sensor_for(coordinator, RETURN)
    client.get_resources_bulk.return_value = _parsed(
        item, _ok_item(MODULATION, 12.0), paths=(RETURN, MODULATION)
    )
    client.get_resource.side_effect = InvalidPayload("still invalid")
    await coordinator.async_refresh()
    assert coordinator.data[RETURN].ended
    assert not sensor.available


async def test_omission_found_by_the_real_parser_keeps_the_value(
    hass: HomeAssistant,
) -> None:
    coordinator, client = _polling(hass, _temperature(RETURN), _temperature(MODULATION))
    sensor = _sensor_for(coordinator, RETURN)
    client.get_resources_bulk.return_value = _parsed(
        _ok_item(MODULATION, 12.0), paths=(RETURN, MODULATION)
    )
    client.get_resource.side_effect = RequestTimeout()
    await coordinator.async_refresh()
    assert not coordinator.data[RETURN].ended
    assert sensor.available and sensor.native_value == 30.0


async def test_invalid_single_read_after_a_failed_request_ends_the_reading(
    hass: HomeAssistant,
) -> None:
    coordinator, client = _polling(hass, _temperature(RETURN))
    sensor = _sensor(coordinator)
    client.get_resources_bulk.side_effect = RequestTimeout()
    client.get_resource.side_effect = InvalidPayload("invalid reading")
    await coordinator.async_refresh()
    assert coordinator.data[RETURN].ended
    assert not sensor.available


async def test_stopped_source_during_a_request_ends_the_reading(
    hass: HomeAssistant,
) -> None:
    from custom_components.bosch_buderus_heating.pointt.exceptions import (
        SourceStopped,
    )

    coordinator, client = _polling(hass, _temperature(RETURN))
    sensor = _sensor(coordinator)
    client.get_resources_bulk.side_effect = SourceStopped()
    client.get_resource.side_effect = SourceStopped()
    await coordinator.async_refresh()
    assert coordinator.data[RETURN].ended
    assert not sensor.available


async def test_pressure_limits_attributes_use_stored_data(
    hass: HomeAssistant,
) -> None:
    from tests.test_sensor import _pressure_sensors

    pressure, _status = _pressure_sensors(hass)
    assert pressure.extra_state_attributes
    data = pressure.coordinator.data
    data["/heatSources/systemPressureRange"] = replace(
        data["/heatSources/systemPressureRange"],
        available=False,
        freshness=Freshness.STALE,
        last_error_category="http_503",
    )
    assert pressure.extra_state_attributes is None


async def test_low_salt_level_itself_uses_stored_data(hass: HomeAssistant) -> None:
    from tests.test_water_softener import reading

    salt = reading("sensor/saltLevel", 10, "%")
    alarm = reading("sensor/saltLevelAlarm", 20, "%")
    coordinator = _coordinator(hass, salt, alarm)
    description = next(
        item
        for item in build_binary_sensor_descriptions(coordinator.resources)
        if item.value_key == "low_salt"
    )
    sensor = BoschBuderusBinarySensor(coordinator, description)
    coordinator.data[salt.path] = replace(
        coordinator.data[salt.path],
        available=False,
        freshness=Freshness.STALE,
        last_error_category="http_503",
    )
    assert not sensor.available


@pytest.mark.parametrize("status", [400, 409, 410])
async def test_other_client_error_from_the_single_read_ends_the_reading(
    hass: HomeAssistant, status: int
) -> None:
    from custom_components.bosch_buderus_heating.pointt.exceptions import (
        UnexpectedHttpStatus,
    )

    coordinator, client = _polling(hass, _temperature(RETURN))
    sensor = _sensor(coordinator)
    client.get_resources_bulk.side_effect = RequestTimeout()
    client.get_resource.side_effect = UnexpectedHttpStatus(status)
    await coordinator.async_refresh()
    assert coordinator.data[RETURN].ended
    assert coordinator.data[RETURN].last_error_category == f"http_{status}"
    assert not sensor.available


async def test_single_read_failure_is_counted_once(hass: HomeAssistant) -> None:
    coordinator, client = _polling(hass, _temperature(RETURN))
    client.get_resources_bulk.side_effect = RequestTimeout()
    client.get_resource.side_effect = InvalidPayload("invalid reading")
    await coordinator.async_refresh()
    # One failed bulk attempt and one single read, not two for the read.
    assert coordinator._capability_metrics[RETURN].attempts == 2
