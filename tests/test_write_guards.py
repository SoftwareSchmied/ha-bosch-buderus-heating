"""Write safety of the 0.9 review: step grid, read-back, preflight, lock wait.

Every test counts the PUT requests: a fix must never add an unintended or
repeated write to the heating system.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.heating_climate import (
    track_heating_climates,
)
from custom_components.bosch_buderus_heating.pointt import (
    AccessTokenRejected,
    AuthenticationError,
    InvalidPayload,
    RateLimited,
    Resource,
    ResourceMetadata,
    ResourceNotFound,
    ServiceUnavailable,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.models import BatchItemResult
from custom_components.bosch_buderus_heating.writes import (
    WriteBusy,
    WriteService,
    assess_control,
    number_policy_for_resource,
)
from tests.test_heating_controls import MODE, ROOT, TARGET, scalar, setup

CONTEXT = (
    scalar(ROOT + "/heatCoolMode", "heat", options=("heat", "heatCool")),
    scalar(ROOT + "/currentSuWiMode", "forced", writable=False),
    scalar("/system/seasonOptimizer/mode", "automatic", options=("off", "automatic")),
    scalar("/system/globalSeasonOptimizer/currentMode", "heating", writable=False),
)
CAPTURE = (
    Path(__file__).parent / "fixtures/contracts/captures/k40-cloud-2026-11-05.json"
)


def _number(path, value, unit="C", minimum=5.0, maximum=30.0):
    return Resource(
        path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue",
            unit=unit,
            writable=True,
            minimum=minimum,
            maximum=maximum,
        ),
    )


class Cloud:
    """Answer reads from the coordinator's view and count every request."""

    def __init__(self, coordinator, *, with_context=True):
        self.coordinator = coordinator
        self.state = {
            path: snapshot.resource for path, snapshot in coordinator.data.items()
        }
        if with_context:
            for resource in CONTEXT:
                coordinator.data[resource.path] = ResourceSnapshot(
                    resource, True, datetime.now(UTC)
                )
                coordinator.resources[resource.path] = resource
                self.state[resource.path] = resource
        self.puts = []
        self.bulk_calls = []
        self.gets = []
        self.readback_error = None
        client = coordinator.client
        client.get_resources_bulk.side_effect = self.bulk
        client.get_resource.side_effect = self.get
        client.put_resource_value.side_effect = self.put
        coordinator._write_service = WriteService(client, sleep=AsyncMock())

    async def bulk(self, gateway, paths, **_kwargs):
        self.bulk_calls.append(tuple(paths))
        return tuple(
            BatchItemResult(gateway, path, 200, resource=self.state[path])
            for path in paths
        )

    async def get(self, gateway, path, **_kwargs):
        self.gets.append(path)
        if self.puts and self.readback_error is not None:
            raise self.readback_error
        return self.state[path]

    async def put(self, gateway, path, value):
        self.puts.append((path, value))
        self.state[path] = replace(self.state[path], value=value)


# C31: the step grid of hot-water and heating-circuit setpoints -------------


@pytest.mark.parametrize(
    ("path", "minimum", "maximum", "allowed", "rejected"),
    [
        ("/dhwCircuits/dhw1/temperatureLevels/high", 40, 55, 53.0, 52.5),
        ("/dhwCircuits/dhw1/singleChargeSetpoint", 50, 70, 60.0, 60.4),
        ("/heatingCircuits/hc1/manualRoomSetpoint", 5, 30, 20.5, 20.3),
        ("/heatingCircuits/hc1/temperatureLevels/eco", 5, 20.5, 17.5, 17.25),
    ],
)
async def test_values_off_the_gateway_grid_are_rejected_before_any_put(
    path, minimum, maximum, allowed, rejected
):
    current = _number(path, minimum, minimum=minimum, maximum=maximum)
    policy = number_policy_for_resource(current)
    assert policy is not None and policy.write_grid
    client = AsyncMock()
    client.put_resource_value.return_value = None
    client.get_resource.return_value = replace(current, value=allowed)
    service = WriteService(client, sleep=AsyncMock())
    with pytest.raises(WriteValidationError, match="step size"):
        await service.async_write_number("gw", current, rejected, policy)
    client.put_resource_value.assert_not_awaited()
    client.get_resource.assert_not_awaited()
    result = await service.async_write_number("gw", current, allowed, policy)
    assert result.resource.value == allowed
    client.put_resource_value.assert_awaited_once_with("gw", path, allowed)


async def test_grid_stays_limited_to_documented_setpoints():
    # The maximum supply temperature and non-Celsius gateways keep sending
    # each value within the limits, exactly once, as before.
    client = AsyncMock()
    client.put_resource_value.return_value = None
    service = WriteService(client, sleep=AsyncMock())
    for current, value in (
        (_number("/heatingCircuits/hc1/maxFlowTemp", 40, minimum=30, maximum=60), 40.5),
        (
            _number(
                "/dhwCircuits/dhw1/temperatureLevels/high",
                120,
                unit="F",
                minimum=100,
                maximum=140,
            ),
            125.5,
        ),
    ):
        client.reset_mock()
        client.get_resource.return_value = replace(current, value=value)
        policy = number_policy_for_resource(current)
        assert policy is not None
        await service.async_write_number("gw", current, value, policy)
        client.put_resource_value.assert_awaited_once_with("gw", current.path, value)


async def test_fahrenheit_climate_target_uses_the_nearest_grid_value(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    hass.config.units = US_CUSTOMARY_SYSTEM
    # Home Assistant converted 70 °F into the entity's Celsius: 21.11 °C.
    await entity.async_set_temperature(temperature=21.111111)
    assert cloud.puts == [(TARGET, 21.0)]


# C32: a read-back failure after a sent PUT ---------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        RateLimited(120),
        AuthenticationError("expired"),
        InvalidPayload("broken"),
        ResourceNotFound(TARGET, 404),
    ],
)
async def test_read_back_failure_after_put_is_unconfirmed(hass, failure):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    cloud.readback_error = failure
    with (
        patch.object(coordinator._config_entry, "async_start_reauth") as reauth,
        pytest.raises(HomeAssistantError) as error,
    ):
        await entity.async_set_temperature(temperature=20.5)
    assert error.value.translation_key == "write_not_confirmed"
    assert isinstance(error.value.__cause__, WriteNotConfirmed)
    assert error.value.__cause__.__cause__ is failure
    assert cloud.puts == [(TARGET, 20.5)]
    snapshot = coordinator.data[TARGET]
    assert not snapshot.available and snapshot.freshness is Freshness.STALE
    rate_limited = isinstance(failure, RateLimited)
    assert (coordinator._cloud_backoff_until > 0) is rate_limited
    assert reauth.call_count == int(isinstance(failure, AuthenticationError))


async def test_read_back_failure_message_no_longer_names_an_operating_mode():
    root = Path(__file__).parents[1] / "custom_components/bosch_buderus_heating"
    for name in ("strings.json", "translations/de.json"):
        exceptions = json.loads((root / name).read_text(encoding="utf-8"))["exceptions"]
        for key in ("write_not_confirmed", "write_failed"):
            message = exceptions[key]["message"]
            assert "Betriebsart" not in message and "operating mode" not in message
            assert "retained" not in message and "bleibt erhalten" not in message


# C33: nothing marked unconfirmed without a sent PUT -------------------------


async def test_failed_preflight_keeps_the_readings(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    failure = ServiceUnavailable(503)
    coordinator.client.get_resources_bulk.side_effect = failure
    coordinator.client.get_resource.side_effect = failure
    with pytest.raises(HomeAssistantError):
        await entity.async_set_temperature(temperature=20.5)
    assert cloud.puts == []
    for path in (MODE, TARGET, "/system/seasonOptimizer/mode"):
        snapshot = coordinator.data[path]
        assert snapshot.available and snapshot.freshness is Freshness.FRESH
        assert not snapshot.ended
    assert entity.available


@pytest.mark.parametrize("before_sending", [True, False])
async def test_rate_limited_put_keeps_the_reading(hass, before_sending):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)

    async def put(gateway, path, value):
        if not before_sending:
            cloud.puts.append((path, value))
        raise RateLimited(60)

    coordinator.client.put_resource_value.side_effect = put
    with pytest.raises(HomeAssistantError) as error:
        await entity.async_set_temperature(temperature=20.5)
    assert error.value.translation_key == "write_rate_limited"
    assert len(cloud.puts) == int(not before_sending)
    snapshot = coordinator.data[TARGET]
    assert snapshot.available and snapshot.freshness is Freshness.FRESH
    assert coordinator._cloud_backoff_until > 0
    assert entity.available


async def test_cancelled_write_is_still_marked_unconfirmed(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)

    async def put(gateway, path, value):
        cloud.puts.append((path, value))
        raise asyncio.CancelledError

    coordinator.client.put_resource_value.side_effect = put
    with pytest.raises(asyncio.CancelledError):
        await entity.async_set_temperature(temperature=20.5)
    assert cloud.puts == [(TARGET, 20.5)]
    assert coordinator.data[TARGET].freshness is Freshness.STALE


async def test_rrc_refused_preflight_keeps_the_reading(hass):
    _, coordinator, _entity = setup(hass)
    path = "/zones/zn1/clockProgram"
    resource = scalar(path, 1, unit=None, minimum=0, maximum=9999)
    coordinator.data[path] = ResourceSnapshot(resource, True, datetime.now(UTC))

    async def refused():
        raise WriteValidationError("RRC program is no longer offered")

    with pytest.raises(WriteValidationError):
        await coordinator._async_rrc_operation(path, refused)
    snapshot = coordinator.data[path]
    assert snapshot.available and snapshot.freshness is Freshness.FRESH
    coordinator.client.put_resource_value.assert_not_awaited()


# C34: plausibility of the advertised limits ---------------------------------


@pytest.mark.parametrize(
    ("resource", "eligible"),
    [
        (
            _number("/dhwCircuits/dhw1/temperatureLevels/high", 50, "C", 0, 6553.5),
            False,
        ),
        (_number("/heatingCircuits/hc1/maxFlowTemp", 40, "C", -3276.8, 6553.5), False),
        (
            _number("/dhwCircuits/dhw1/chargeDuration", 60, "mins", 0, 1e9),
            False,
        ),
        (_number("/dhwCircuits/dhw1/chargeDuration", 60, "mins", 60, 2880), True),
        (_number("/dhwCircuits/dhw1/chargeDuration", 1, "h", 1, 48), True),
        (_number("/dhwCircuits/dhw1/chargeDuration", 1, "h", 1, 49), False),
        (_number("/heatingCircuits/hc1/maxFlowTemp", 40, "C", -30, 100), True),
        (_number("/heatingCircuits/hc1/maxFlowTemp", 40, "C", -31, 100), False),
        (_number("/dhwCircuits/dhw1/temperatureLevels/high", 120, "F", 86, 212), True),
        (_number("/dhwCircuits/dhw1/temperatureLevels/high", 120, "F", 86, 213), False),
        (_number("/dhwCircuits/dhw1/temperatureLevels/high", 320, "K", 300, 373), True),
        (
            _number("/dhwCircuits/dhw1/temperatureLevels/high", 320, "K", 300, 380),
            False,
        ),
    ],
)
async def test_implausible_limits_create_no_control_and_send_nothing(
    resource, eligible
):
    assessment = assess_control(resource)
    assert assessment.eligible is eligible
    if not eligible:
        assert assessment.rejection_reason == "implausible_bounds"
        client = AsyncMock()
        with pytest.raises(WriteValidationError):
            await WriteService(client).async_write_number(
                "gw", resource, resource.value, assessment.policy
            )
        client.put_resource_value.assert_not_awaited()


def test_limits_of_the_reference_capture_stay_plausible():
    capture = json.loads(CAPTURE.read_text(encoding="utf-8"))
    checked = 0

    def visit(node):
        nonlocal checked
        if isinstance(node, dict):
            if isinstance(node.get("path"), str) and "metadata" in node:
                resource = Resource(
                    node["path"],
                    value=node.get("value"),
                    has_value=bool(node.get("has_value")),
                    metadata=ResourceMetadata(
                        resource_type=node["metadata"].get("resource_type"),
                        unit=node["metadata"].get("unit"),
                        writable=bool(node["metadata"].get("writable")),
                        minimum=node["metadata"].get("minimum"),
                        maximum=node["metadata"].get("maximum"),
                    ),
                )
                if (
                    resource.metadata.resource_type == "floatValue"
                    and resource.metadata.writable
                    and assess_control(resource).policy is not None
                ):
                    checked += 1
                    assert assess_control(resource).rejection_reason != (
                        "implausible_bounds"
                    ), resource.path
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(capture)
    assert checked >= 5


# C35: one bulk preflight, no second read of the target ----------------------


async def test_unchanged_climate_target_costs_one_bulk_read_and_no_put(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    await entity.async_set_temperature(temperature=19)
    assert len(cloud.bulk_calls) == 1
    assert set(cloud.bulk_calls[0]) == {MODE, TARGET} | {r.path for r in CONTEXT}
    assert cloud.gets == [] and cloud.puts == []


async def test_changed_climate_target_writes_once_after_one_bulk_read(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    await entity.async_set_temperature(temperature=20.5)
    assert len(cloud.bulk_calls) == 1
    assert cloud.puts == [(TARGET, 20.5)]
    assert cloud.gets == [TARGET]  # the read-back only
    assert coordinator.data[TARGET].resource.value == 20.5


async def test_mode_command_uses_the_bulk_reading_of_its_own_resource(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    await entity.async_set_hvac_mode(HVACMode.HEAT)  # already manual
    await entity.async_set_hvac_mode(HVACMode.AUTO)
    assert len(cloud.bulk_calls) == 2
    assert cloud.puts == [(MODE, "auto")]
    assert cloud.gets == [MODE]


async def test_bulk_failure_falls_back_to_single_reads(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    coordinator.client.get_resources_bulk.side_effect = ServiceUnavailable(502)
    await entity.async_set_temperature(temperature=20.5)
    assert cloud.puts == [(TARGET, 20.5)]
    # Six preflight reads, then the read-back.
    assert len(cloud.gets) == 7 and cloud.gets[-1] == TARGET


async def test_unanswered_bulk_item_is_read_individually(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)

    async def bulk(gateway, paths, **_kwargs):
        cloud.bulk_calls.append(tuple(paths))
        return tuple(
            BatchItemResult(gateway, path, 200, resource=cloud.state[path])
            if path != MODE
            else BatchItemResult(gateway, path, 500, error=ServiceUnavailable(500))
            for path in paths
        )

    coordinator.client.get_resources_bulk.side_effect = bulk
    await entity.async_set_temperature(temperature=20.5)
    assert cloud.gets == [MODE, TARGET]
    assert cloud.puts == [(TARGET, 20.5)]


@pytest.mark.parametrize("whole", [True, False])
async def test_rate_limited_preflight_sends_nothing(hass, whole):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)

    async def bulk(gateway, paths, **_kwargs):
        if whole:
            raise RateLimited(90)
        return tuple(BatchItemResult(gateway, path, 429) for path in paths)

    coordinator.client.get_resources_bulk.side_effect = bulk
    with pytest.raises(HomeAssistantError) as error:
        await entity.async_set_temperature(temperature=20.5)
    assert error.value.translation_key == "write_rate_limited"
    assert cloud.puts == [] and cloud.gets == []
    assert coordinator._cloud_backoff_until > 0
    assert coordinator.data[TARGET].freshness is Freshness.FRESH


async def test_preflight_sign_in_failure_requests_reauthentication(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    coordinator.client.get_resources_bulk.side_effect = AccessTokenRejected("x")
    with (
        patch.object(coordinator._config_entry, "async_start_reauth") as reauth,
        pytest.raises(HomeAssistantError),
    ):
        await entity.async_set_temperature(temperature=20.5)
    assert reauth.call_count == 1
    assert cloud.puts == []


async def test_gateway_change_seen_in_bulk_blocks_and_publishes(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    cloud.state[MODE] = replace(cloud.state[MODE], value="auto")
    with pytest.raises(ServiceValidationError):
        await entity.async_set_temperature(temperature=20.5)
    assert cloud.puts == []
    assert coordinator.data[MODE].resource.value == "auto"


# C58: a write waits only so long for a running poll -------------------------


async def test_write_behind_a_long_poll_is_reported_and_never_sent_later(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    with patch.object(
        coordinator_module, "WRITE_LOCK_TIMEOUT", coordinator_module.timedelta(0)
    ):
        async with coordinator._update_lock:
            with pytest.raises(HomeAssistantError) as error:
                await entity.async_set_temperature(temperature=20.5)
            assert error.value.translation_key == "write_busy"
            assert isinstance(error.value.__cause__, WriteBusy)
            with pytest.raises(WriteBusy):
                await coordinator.async_delete_holiday(1)
            with pytest.raises(WriteBusy):
                await coordinator._async_rrc_operation("/zones/zn1/clockProgram", None)
        await asyncio.sleep(0)
    assert not coordinator._update_lock.locked()
    assert cloud.puts == [] and cloud.bulk_calls == []
    coordinator.client.delete_holiday_period.assert_not_awaited()
    # Once the poll is over, the next command runs normally.
    await entity.async_set_temperature(temperature=20.5)
    assert cloud.puts == [(TARGET, 20.5)]


async def test_lock_wait_limit_never_interrupts_a_running_write(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    original = cloud.put

    async def slow_put(gateway, path, value):
        await asyncio.sleep(0.05)
        await original(gateway, path, value)

    coordinator.client.put_resource_value.side_effect = slow_put
    with patch.object(
        coordinator_module,
        "WRITE_LOCK_TIMEOUT",
        coordinator_module.timedelta(seconds=0.01),
    ):
        await entity.async_set_temperature(temperature=20.5)
    assert cloud.puts == [(TARGET, 20.5)]
    assert coordinator.data[TARGET].freshness is Freshness.FRESH
    assert not coordinator._update_lock.locked()


async def test_waiting_write_starts_when_the_poll_ends_in_time(hass):
    _, coordinator, entity = setup(hass)
    cloud = Cloud(coordinator)
    async with coordinator._update_lock:
        task = asyncio.create_task(entity.async_set_temperature(temperature=20.5))
        await asyncio.sleep(0)
        assert cloud.puts == []
    await task
    assert cloud.puts == [(TARGET, 20.5)]


# C70: a registered climate stays provided in a cooling context ---------------


@pytest.mark.parametrize("registered", [True, False])
async def test_registered_climate_is_added_unavailable_in_cooling(hass, registered):
    entry, coordinator, _entity = setup(hass)
    cooling = scalar(ROOT + "/currentSuWiMode", "cooling", writable=False)
    coordinator.data[cooling.path] = ResourceSnapshot(cooling, True, datetime.now(UTC))
    if registered:
        er.async_get(hass).async_get_or_create(
            "climate",
            DOMAIN,
            "gateway:heatingCircuits:hc2:climate",
            config_entry=entry,
        )
    added = []
    lifecycle = type("Lifecycle", (), {"async_on_unload": lambda self, _cb: None})()
    track_heating_climates(lifecycle, coordinator, added.extend)
    assert len(added) == int(registered)
    if registered:
        assert not added[0].available
        with pytest.raises(ServiceValidationError):
            await added[0].async_set_hvac_mode(HVACMode.HEAT)
    coordinator.client.put_resource_value.assert_not_awaited()


async def test_climate_registered_for_another_entry_is_not_adopted(hass):
    _, coordinator, _entity = setup(hass)
    cooling = scalar(ROOT + "/currentSuWiMode", "cooling", writable=False)
    coordinator.data[cooling.path] = ResourceSnapshot(cooling, True, datetime.now(UTC))
    er.async_get(hass).async_get_or_create(
        "climate", DOMAIN, "gateway:heatingCircuits:hc2:climate"
    )
    added = []
    lifecycle = type("Lifecycle", (), {"async_on_unload": lambda self, _cb: None})()
    track_heating_climates(lifecycle, coordinator, added.extend)
    assert added == []
