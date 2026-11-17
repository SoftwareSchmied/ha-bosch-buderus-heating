"""L16: a control write reads the written setting live before its one PUT.

The gateway here is a small stateful fake that answers the bulk and single
reads, accepts one PUT and shows the new value. The coordinator and the write
service are the real ones, so every case runs the real transaction.
"""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
    SnapshotSource,
)
from custom_components.bosch_buderus_heating.live_target import with_live_target
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    BatchItemResult,
    Gateway,
    Resource,
    ResourceMetadata,
    WriteNotConfirmed,
    WriteRejected,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    RequestTimeout,
    ResourceNotFound,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.write_window import _WINDOW
from custom_components.bosch_buderus_heating.writes import (
    WriteBusy,
    WriteService,
    assess_control,
)
from tests.ha_test_helpers import MockConfigEntry

DHW_TARGET = "/dhwCircuits/dhw1/temperatureLevels/high"
DHW_MODE = "/dhwCircuits/dhw1/operationMode"


def number(value=50.0, **meta):
    fields = {
        "resource_type": "floatValue",
        "unit": "C",
        "minimum": 40,
        "maximum": 60,
        "writable": True,
    }
    fields.update(meta)
    return Resource(
        path=DHW_TARGET,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(**fields),
    )


def enum(path=DHW_MODE, value="Off", options=("Off", "On", "Eco"), **meta):
    fields = {
        "resource_type": "stringValue",
        "writable": True,
        "allowed_values": options,
    }
    fields.update(meta)
    return Resource(
        path=path, value=value, has_value=True, metadata=ResourceMetadata(**fields)
    )


class FakeGateway:
    """What the gateway holds; ``fail`` makes every read fail the same way."""

    def __init__(self, *resources: Resource) -> None:
        self.state = {r.path: r for r in resources}
        self.fail: Exception | None = None
        self.puts: list[tuple[str, object]] = []
        self.reads: list[str] = []
        self.apply_puts = True
        self.client = AsyncMock()
        self.client.get_resources_bulk.side_effect = self._bulk
        self.client.get_resource.side_effect = self._single
        self.client.put_resource_value.side_effect = self._put

    async def _bulk(self, gateway, paths):
        self.reads.extend(paths)
        if self.fail is not None:
            raise self.fail
        return tuple(
            BatchItemResult(gateway, p, 200, resource=self.state[p]) for p in paths
        )

    async def _single(self, gateway, path):
        self.reads.append(path)
        if self.fail is not None:
            raise self.fail
        if path not in self.state:
            raise ResourceNotFound(path, 404)
        return self.state[path]

    async def _put(self, gateway, path, value):
        self.puts.append((path, value))
        if self.apply_puts:
            self.state[path] = replace(self.state[path], value=value)


def coordinator_for(hass, gateway: FakeGateway, *polled: Resource):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, gateway.client, Gateway("synthetic-gateway"), entry
    )
    coordinator._write_service = WriteService(
        gateway.client, sleep=AsyncMock(), read_back_delay=0, read_back_attempts=2
    )
    coordinator.resources = {r.path: r for r in polled}
    coordinator.data = {
        r.path: ResourceSnapshot(r, True, datetime.now(UTC)) for r in polled
    }
    coordinator.last_update_success = True
    coordinator.update_interval = None
    return coordinator


def policy_of(resource: Resource):
    assessment = assess_control(resource)
    assert assessment.policy is not None
    return assessment.policy


def with_meta(resource: Resource, **meta) -> Resource:
    return replace(resource, metadata=replace(resource.metadata, **meta))


# --- the mechanism ----------------------------------------------------------


def test_the_target_joins_the_reads_once():
    target, guard = number(), enum()
    assert with_live_target((), target) == (target,)
    assert with_live_target((guard,), target) == (guard, target)
    assert with_live_target((target, guard), target) == (target, guard)


async def test_a_write_reads_the_target_live_once_then_sends_once(hass):
    polled = number(50.0)
    gateway = FakeGateway(polled)
    coordinator = coordinator_for(hass, gateway, polled)

    confirmed = await coordinator.async_write_control(
        DHW_TARGET, 55.0, policy_of(polled)
    )

    assert confirmed.value == 55.0
    assert gateway.puts == [(DHW_TARGET, 55.0)]
    # One bulk read before the PUT; the rest are read-backs.
    assert gateway.client.get_resources_bulk.await_count == 1
    assert coordinator.data[DHW_TARGET].source is SnapshotSource.WRITE
    assert coordinator.data[DHW_TARGET].resource.value == 55.0


async def test_an_enum_write_reads_live_and_sends_once(hass):
    polled = enum(value="Off")
    gateway = FakeGateway(polled)
    coordinator = coordinator_for(hass, gateway, polled)

    await coordinator.async_write_control(DHW_MODE, "Eco", policy_of(polled))

    assert gateway.puts == [(DHW_MODE, "Eco")]
    assert gateway.client.get_resources_bulk.await_count == 1


async def test_a_further_reading_is_read_in_the_same_request(hass):
    polled = number(50.0)
    other = enum()
    gateway = FakeGateway(polled, other)
    coordinator = coordinator_for(hass, gateway, polled, other)

    await coordinator.async_write_control(
        DHW_TARGET, 52.0, policy_of(polled), fresh=(other,)
    )

    assert gateway.client.get_resources_bulk.await_count == 1
    assert sorted(gateway.reads[:2]) == sorted([DHW_TARGET, DHW_MODE])


# --- the live reading decides, not the poll ---------------------------------


@pytest.mark.parametrize(
    "live_meta",
    [
        {"used": False},
        {"available": False},
        {"writable": False},
        {"maximum": 52},
        {"minimum": 54},
    ],
)
async def test_the_live_flags_and_limits_refuse_what_the_poll_allowed(hass, live_meta):
    polled = number(50.0)
    gateway = FakeGateway(with_meta(polled, **live_meta))
    coordinator = coordinator_for(hass, gateway, polled)

    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(DHW_TARGET, 53.0, policy_of(polled))

    assert gateway.puts == []


async def test_live_options_decide_an_enum(hass):
    polled = enum(options=("Off", "On", "Eco"))
    gateway = FakeGateway(with_meta(polled, allowed_values=("Off", "On")))
    coordinator = coordinator_for(hass, gateway, polled)

    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(DHW_MODE, "Eco", policy_of(polled))

    assert gateway.puts == []


async def test_a_limit_widened_on_the_gateway_allows_the_write(hass):
    polled = number(50.0, maximum=55)
    gateway = FakeGateway(with_meta(polled, maximum=60))
    coordinator = coordinator_for(hass, gateway, polled)

    await coordinator.async_write_control(DHW_TARGET, 58.0, policy_of(polled))

    assert gateway.puts == [(DHW_TARGET, 58.0)]


async def test_a_value_already_on_the_gateway_sends_nothing(hass):
    polled = number(50.0)
    gateway = FakeGateway(replace(polled, value=55.0))
    coordinator = coordinator_for(hass, gateway, polled)

    result = await coordinator.async_write_control(DHW_TARGET, 55.0, policy_of(polled))

    assert result.value == 55.0
    assert gateway.puts == []
    assert coordinator.data[DHW_TARGET].resource.value == 55.0


async def test_a_stale_poll_value_equal_to_the_request_still_writes(hass):
    polled = number(55.0)
    gateway = FakeGateway(replace(polled, value=50.0))
    coordinator = coordinator_for(hass, gateway, polled)

    await coordinator.async_write_control(DHW_TARGET, 55.0, policy_of(polled))

    assert gateway.puts == [(DHW_TARGET, 55.0)]


# --- failure paths with the real transaction --------------------------------


@pytest.mark.parametrize(
    "failure", [RequestTimeout(), ServiceUnavailable(), AuthenticationError()]
)
async def test_a_failed_live_read_sends_nothing_and_keeps_the_reading(hass, failure):
    polled = number(50.0)
    gateway = FakeGateway(polled)
    gateway.fail = failure
    coordinator = coordinator_for(hass, gateway, polled)
    coordinator._request_reauthentication = Mock()
    before = coordinator.data[DHW_TARGET]

    with pytest.raises(type(failure)):
        await coordinator.async_write_control(DHW_TARGET, 55.0, policy_of(polled))

    assert gateway.puts == []
    assert coordinator.data[DHW_TARGET] == before
    assert coordinator.data[DHW_TARGET].freshness is Freshness.FRESH
    assert coordinator._request_reauthentication.called is isinstance(
        failure, AuthenticationError
    )


async def test_the_next_write_works_once_the_gateway_answers_again(hass):
    polled = number(50.0)
    gateway = FakeGateway(polled)
    coordinator = coordinator_for(hass, gateway, polled)
    gateway.fail = RequestTimeout()
    with pytest.raises(RequestTimeout):
        await coordinator.async_write_control(DHW_TARGET, 55.0, policy_of(polled))
    gateway.fail = None

    await coordinator.async_write_control(DHW_TARGET, 55.0, policy_of(polled))

    assert gateway.puts == [(DHW_TARGET, 55.0)]


async def test_a_resource_the_gateway_no_longer_has_sends_nothing(hass):
    polled = number(50.0)
    gateway = FakeGateway(polled)
    coordinator = coordinator_for(hass, gateway, polled)
    del gateway.state[DHW_TARGET]
    # The bulk answer is missing as well: the single read ends the write.
    gateway.client.get_resources_bulk.side_effect = None
    gateway.client.get_resources_bulk.return_value = ()

    with pytest.raises(ResourceNotFound):
        await coordinator.async_write_control(DHW_TARGET, 55.0, policy_of(polled))

    assert gateway.puts == []


@pytest.mark.parametrize("state", ["stale", "unavailable", "missing"])
async def test_a_poll_state_without_a_current_reading_refuses_before_any_read(
    hass, state
):
    polled = number(50.0)
    gateway = FakeGateway(polled)
    coordinator = coordinator_for(hass, gateway, polled)
    if state == "stale":
        coordinator.data[DHW_TARGET] = replace(
            coordinator.data[DHW_TARGET], freshness=Freshness.STALE
        )
    elif state == "unavailable":
        coordinator.data[DHW_TARGET] = replace(
            coordinator.data[DHW_TARGET], available=False
        )
    else:
        coordinator.data = {}

    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(DHW_TARGET, 55.0, policy_of(polled))

    assert gateway.puts == []
    assert gateway.reads == []


async def test_an_unconfirmed_write_sends_once_and_marks_the_reading_stale(hass):
    polled = number(50.0)
    gateway = FakeGateway(polled)
    gateway.apply_puts = False
    coordinator = coordinator_for(hass, gateway, polled)

    with pytest.raises((WriteNotConfirmed, WriteRejected)):
        await coordinator.async_write_control(DHW_TARGET, 55.0, policy_of(polled))

    assert gateway.puts == [(DHW_TARGET, 55.0)]
    assert coordinator.data[DHW_TARGET].freshness is Freshness.STALE


async def test_a_refused_write_does_not_block_the_next_one(hass):
    polled = number(50.0)
    gateway = FakeGateway(with_meta(polled, used=False))
    coordinator = coordinator_for(hass, gateway, polled)
    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(DHW_TARGET, 53.0, policy_of(polled))
    assert gateway.puts == []

    gateway.state[DHW_TARGET] = polled
    await coordinator.async_write_control(DHW_TARGET, 53.0, policy_of(polled))

    assert gateway.puts == [(DHW_TARGET, 53.0)]


async def test_a_slow_live_read_that_uses_up_the_send_window_sends_nothing(hass):
    polled = number(50.0)
    gateway = FakeGateway(polled)
    coordinator = coordinator_for(hass, gateway, polled)
    read = gateway.client.get_resources_bulk.side_effect

    async def slow_read(gateway_id, paths):
        answer = await read(gateway_id, paths)
        # The time of the transaction is gone when the answer arrives.
        window = _WINDOW.get()
        assert window is not None
        window.deadline = 0.0
        return answer

    gateway.client.get_resources_bulk.side_effect = slow_read

    with pytest.raises(WriteBusy):
        await coordinator.async_write_control(DHW_TARGET, 55.0, policy_of(polled))

    assert gateway.puts == []
    assert coordinator.data[DHW_TARGET].freshness is Freshness.FRESH
