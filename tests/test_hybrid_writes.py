"""Hybrid plants: the write transactions of the strategy and the prices.

The coordinator and the write service are the real ones; the gateway is the
stateful fake of ``test_live_write``. Every case runs the whole transaction:
live read, one PUT, read-back, no second PUT.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.bosch_buderus_heating import coordinator as coordinator_module
from custom_components.bosch_buderus_heating import hybrid_controls as hybrid
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.number import (
    async_setup_entry as setup_numbers,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    Gateway,
    InvalidPayload,
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    RateLimited,
    RequestTimeout,
    ResourceForbidden,
    ServiceUnavailable,
    UnexpectedHttpStatus,
)
from custom_components.bosch_buderus_heating.select import (
    async_setup_entry as setup_selects,
)
from custom_components.bosch_buderus_heating.writes import (
    RRC_READ_BACK_DELAYS,
    RrcWriteNotConfirmed,
    WriteBusy,
    WriteService,
    assess_control,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_hybrid_controls import (
    ELECTRICITY,
    GAS,
    STRATEGY,
    plant,
    price,
    strategy,
)
from tests.test_live_write import FakeGateway, coordinator_for, with_meta

POLLED = (strategy(), price(ELECTRICITY, 0.55), price(GAS, 0.09))


def policy_of(resource, context=None):
    assessment = assess_control(resource, context)
    assert assessment.eligible, assessment.rejection_reason
    return assessment.policy


def setup(hass, *resources, gateway_cls=FakeGateway):
    resources = resources or POLLED
    gateway = gateway_cls(*resources)
    return gateway, coordinator_for(hass, gateway, *resources)


async def entities(hass, coordinator):
    unload = []
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(bacon=None, coordinators=(coordinator,)),
        async_on_unload=unload.append,
    )
    added = []
    await setup_selects(hass, entry, added.extend)
    await setup_numbers(hass, entry, added.extend)
    return {item.entity_description.resource_path: item for item in added}, unload


# --- 10 to 12: the writes ---------------------------------------------------


async def test_the_strategy_is_read_live_sent_once_and_confirmed(hass):
    gateway, coordinator = setup(hass)
    chosen, unload = await entities(hass, coordinator)
    select = chosen[STRATEGY]
    assert select.current_option == "cost"

    await select.async_select_option("max_renewable_share")

    assert gateway.puts == [(STRATEGY, "maxRenewableShare")]
    # One live read before the PUT; the rest are read-backs.
    assert gateway.client.get_resources_bulk.await_count == 1
    assert select.current_option == "max_renewable_share"
    assert coordinator.data[STRATEGY].resource.value == "maxRenewableShare"
    for callback in unload:
        callback()


async def test_a_price_is_sent_once_as_a_float_and_confirmed(hass):
    gateway, coordinator = setup(hass)
    chosen, _ = await entities(hass, coordinator)
    number = chosen[ELECTRICITY]

    await number.async_set_native_value(0.31)

    assert gateway.puts == [(ELECTRICITY, 0.31)]
    assert isinstance(gateway.puts[0][1], float)
    assert number.native_value == 0.31
    assert coordinator.data[ELECTRICITY].resource.value == 0.31


async def test_an_unchanged_value_sends_nothing_and_a_new_one_sends_once(hass):
    gateway, coordinator = setup(hass)
    chosen, _ = await entities(hass, coordinator)
    number = chosen[ELECTRICITY]

    # The value the plant holds already: no PUT.
    await number.async_set_native_value(0.55)
    assert gateway.puts == []

    # An hourly automation: the same value again and again, then a new one.
    await number.async_set_native_value(0.31)
    await number.async_set_native_value(0.31)
    await number.async_set_native_value(0.31)
    assert gateway.puts == [(ELECTRICITY, 0.31)]
    await number.async_set_native_value(0.32)
    assert gateway.puts == [(ELECTRICITY, 0.31), (ELECTRICITY, 0.32)]

    select = chosen[STRATEGY]
    await select.async_select_option("cost")
    assert len(gateway.puts) == 2


# --- 13: refusals before anything is sent -----------------------------------


@pytest.mark.parametrize("value", [-0.01, 100.01, 0.555, 0.001, 1e9])
async def test_values_outside_the_limits_or_the_grid_are_refused_unsent(hass, value):
    gateway, coordinator = setup(hass)

    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(ELECTRICITY, value, policy_of(POLLED[1]))

    assert gateway.puts == []
    assert coordinator.data[ELECTRICITY].resource.value == 0.55
    assert coordinator.data[ELECTRICITY].freshness is Freshness.FRESH


@pytest.mark.parametrize("value", [True, "0.31", None, float("nan"), float("inf")])
async def test_values_of_the_wrong_kind_are_refused_unsent(hass, value):
    gateway, coordinator = setup(hass)

    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(ELECTRICITY, value, policy_of(POLLED[1]))

    assert gateway.puts == []


async def test_a_strategy_the_plant_does_not_offer_is_refused_unsent(hass):
    gateway, coordinator = setup(hass)

    for value in ("boilerOnly", "vendorSpecial", ""):
        with pytest.raises(WriteValidationError):
            await coordinator.async_write_control(STRATEGY, value, policy_of(POLLED[0]))

    assert gateway.puts == []


# --- 14: the live reading decides -------------------------------------------


@pytest.mark.parametrize(
    "live",
    [
        {"writable": False},
        {"used": False},
        {"available": False},
        {"maximum": 0.30},
        {"minimum": 0.35},
        {"step_size": 0.05},
    ],
)
async def test_the_live_reading_refuses_what_the_poll_allowed(hass, live):
    gateway, coordinator = setup(hass)
    gateway.state[ELECTRICITY] = with_meta(gateway.state[ELECTRICITY], **live)

    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(ELECTRICITY, 0.31, policy_of(POLLED[1]))

    assert gateway.puts == []


@pytest.mark.parametrize(
    "live",
    [
        {"allowed_values": ("cost",)},
        {"allowed_values": ("cost", "vendorSpecial")},
        {"writable": False},
        {"used": False},
        {"available": False},
    ],
)
async def test_the_live_reading_refuses_a_strategy_the_poll_allowed(hass, live):
    gateway, coordinator = setup(hass)
    gateway.state[STRATEGY] = with_meta(gateway.state[STRATEGY], **live)

    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(
            STRATEGY, "maxRenewableShare", policy_of(POLLED[0])
        )

    assert gateway.puts == []


async def test_a_limit_widened_on_the_plant_allows_a_price(hass):
    gateway, coordinator = setup(hass)
    narrow = price(ELECTRICITY, 0.55, maximum=0.60)
    coordinator.data[ELECTRICITY] = ResourceSnapshot(narrow, True, datetime.now(UTC))
    coordinator.resources[ELECTRICITY] = narrow

    await coordinator.async_write_control(ELECTRICITY, 0.90, policy_of(narrow))

    assert gateway.puts == [(ELECTRICITY, 0.90)]


# --- 15: no current reading -------------------------------------------------


@pytest.mark.parametrize("state", ["stale", "unavailable", "missing"])
async def test_a_poll_state_without_a_current_reading_refuses_before_any_read(
    hass, state
):
    gateway, coordinator = setup(hass)
    if state == "stale":
        coordinator.data[ELECTRICITY] = replace(
            coordinator.data[ELECTRICITY], freshness=Freshness.STALE
        )
    elif state == "unavailable":
        coordinator.data[ELECTRICITY] = replace(
            coordinator.data[ELECTRICITY], available=False
        )
    else:
        del coordinator.data[ELECTRICITY]

    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(ELECTRICITY, 0.31, policy_of(POLLED[1]))

    assert gateway.puts == [] and gateway.reads == []


async def test_an_entity_without_a_current_reading_is_unavailable(hass):
    _, coordinator = setup(hass)
    chosen, _ = await entities(hass, coordinator)
    assert chosen[ELECTRICITY].available and chosen[STRATEGY].available
    coordinator.data[ELECTRICITY] = replace(
        coordinator.data[ELECTRICITY], freshness=Freshness.STALE
    )
    assert not chosen[ELECTRICITY].available


# --- 16 to 20: failure paths with the real transaction ----------------------


class TimeoutAfterApply(FakeGateway):
    """The PUT reaches the plant, the answer is lost."""

    async def _put(self, gateway, path, value):
        await super()._put(gateway, path, value)
        raise RequestTimeout()


class TimeoutBeforeApply(FakeGateway):
    """The PUT is lost on the way."""

    async def _put(self, gateway, path, value):
        self.puts.append((path, value))
        raise RequestTimeout()


async def test_a_put_timeout_with_the_new_value_read_back_is_confirmed_unrepeated(
    hass,
):
    gateway, coordinator = setup(hass, gateway_cls=TimeoutAfterApply)

    confirmed = await coordinator.async_write_control(
        ELECTRICITY, 0.31, policy_of(POLLED[1])
    )

    assert confirmed.value == 0.31
    assert gateway.puts == [(ELECTRICITY, 0.31)]
    assert coordinator.data[ELECTRICITY].freshness is Freshness.FRESH


async def test_the_write_service_reports_the_timed_out_put(hass):
    gateway = TimeoutAfterApply(*POLLED)
    service = WriteService(
        gateway.client, sleep=AsyncMock(), read_back_delay=0, read_back_attempts=2
    )

    result = await service.async_write_number(
        "synthetic-gateway", POLLED[1], 0.31, policy_of(POLLED[1])
    )

    assert result.put_timed_out is True
    assert gateway.puts == [(ELECTRICITY, 0.31)]


async def test_a_put_timeout_with_the_old_value_read_back_is_not_confirmed(hass):
    gateway, coordinator = setup(hass, gateway_cls=TimeoutBeforeApply)

    with pytest.raises(WriteNotConfirmed):
        await coordinator.async_write_control(ELECTRICITY, 0.31, policy_of(POLLED[1]))

    assert gateway.puts == [(ELECTRICITY, 0.31)]
    # The reading is not trusted until the next poll, nothing is sent again.
    assert coordinator.data[ELECTRICITY].freshness is Freshness.STALE


async def test_the_entity_reports_an_unconfirmed_write_as_an_error(hass):
    gateway, coordinator = setup(hass, gateway_cls=TimeoutBeforeApply)
    chosen, _ = await entities(hass, coordinator)

    with pytest.raises(HomeAssistantError):
        await chosen[ELECTRICITY].async_set_native_value(0.31)

    assert len(gateway.puts) == 1
    assert not chosen[ELECTRICITY].available


@pytest.mark.parametrize(
    "failure", [ResourceForbidden(ELECTRICITY, 403), UnexpectedHttpStatus(400)]
)
async def test_a_refused_put_is_sent_once_and_changes_nothing(hass, failure):
    gateway, coordinator = setup(hass)

    async def refuse(gateway_id, path, value):
        gateway.puts.append((path, value))
        raise failure

    gateway.client.put_resource_value.side_effect = refuse

    with pytest.raises((WriteNotConfirmed, ResourceForbidden)):
        await coordinator.async_write_control(ELECTRICITY, 0.31, policy_of(POLLED[1]))

    assert gateway.puts == [(ELECTRICITY, 0.31)]
    assert gateway.state[ELECTRICITY].value == 0.55


async def test_a_rate_limit_on_the_put_starts_the_backoff_and_ends_the_requests(hass):
    gateway, coordinator = setup(hass)

    async def limited(gateway_id, path, value):
        gateway.puts.append((path, value))
        raise RateLimited(retry_after=30)

    gateway.client.put_resource_value.side_effect = limited

    with pytest.raises(RateLimited):
        await coordinator.async_write_control(ELECTRICITY, 0.31, policy_of(POLLED[1]))
    requests = len(gateway.reads) + len(gateway.puts)
    assert gateway.puts == [(ELECTRICITY, 0.31)]

    # The next change is stopped by the backoff: no request of any kind.
    with pytest.raises(RateLimited):
        await coordinator.async_write_control(ELECTRICITY, 0.32, policy_of(POLLED[1]))
    assert len(gateway.reads) + len(gateway.puts) == requests


@pytest.mark.parametrize(
    ("failure", "cause"),
    [
        (ServiceUnavailable(), ServiceUnavailable),
        (RequestTimeout(), RequestTimeout),
        (AuthenticationError(), AuthenticationError),
        (InvalidPayload("garbled"), InvalidPayload),
    ],
    ids=["503", "timeout", "401", "payload"],
)
async def test_a_read_back_that_cannot_be_completed_is_not_confirmed_with_its_cause(
    hass, failure, cause
):
    gateway, coordinator = setup(hass)
    coordinator._request_reauthentication = Mock()

    async def broken(gateway_id, path):
        raise failure

    gateway.client.get_resource.side_effect = broken

    with pytest.raises(WriteNotConfirmed) as raised:
        await coordinator.async_write_control(ELECTRICITY, 0.31, policy_of(POLLED[1]))

    assert isinstance(raised.value.__cause__, cause)
    # Sent once; every attempt of the read-back failed, none sent again.
    assert gateway.puts == [(ELECTRICITY, 0.31)]
    assert gateway.client.get_resource.await_count >= 1
    assert coordinator._request_reauthentication.called is issubclass(
        cause, AuthenticationError
    )
    assert coordinator.data[ELECTRICITY].freshness is Freshness.STALE


class RoundingGateway(FakeGateway):
    """A plant that stores a price to a tenth, whatever it was sent."""

    async def _put(self, gateway, path, value):
        self.puts.append((path, value))
        self.state[path] = replace(self.state[path], value=round(value, 1))


async def test_a_plant_that_rounds_the_price_never_confirms_it(hass):
    gateway, coordinator = setup(hass, gateway_cls=RoundingGateway)

    with pytest.raises(WriteNotConfirmed):
        await coordinator.async_write_control(ELECTRICITY, 0.31, policy_of(POLLED[1]))

    assert gateway.puts == [(ELECTRICITY, 0.31)]
    assert gateway.state[ELECTRICITY].value == 0.3


async def test_a_plant_with_a_coarser_grid_refuses_the_price_unsent(hass):
    # The same plant says so with its step: the value is refused before the
    # PUT instead of being stored rounded.
    coarse = price(ELECTRICITY, 0.5, step_size=0.1)
    gateway, coordinator = setup(hass, coarse, gateway_cls=RoundingGateway)

    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(ELECTRICITY, 0.31, policy_of(coarse))

    assert gateway.puts == []
    await coordinator.async_write_control(ELECTRICITY, 0.3, policy_of(coarse))
    assert gateway.puts == [(ELECTRICITY, 0.3)]


# --- 21: a room controller ---------------------------------------------------


async def test_on_a_room_controller_the_read_back_follows_the_slow_plan(hass):
    gateway = FakeGateway(*POLLED)
    gateway.apply_puts = False
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    sleep = AsyncMock()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, gateway.client, Gateway("synthetic-gateway", device_type="rrc2"), entry
    )
    coordinator._write_service = WriteService(gateway.client, sleep=sleep)
    coordinator.resources = {r.path: r for r in POLLED}
    coordinator.data = {
        r.path: ResourceSnapshot(r, True, datetime.now(UTC)) for r in POLLED
    }
    coordinator.last_update_success = True
    coordinator.update_interval = None
    assert coordinator.control_context().rrc_plant

    with pytest.raises(RrcWriteNotConfirmed) as raised:
        await coordinator.async_write_control(
            ELECTRICITY,
            0.31,
            policy_of(POLLED[1], coordinator.control_context()),
        )

    assert raised.value.translation_key == "rrc_write_pending_confirmation"
    assert [call.args[0] for call in sleep.await_args_list] == list(
        RRC_READ_BACK_DELAYS
    )
    assert gateway.puts == [(ELECTRICITY, 0.31)]


# --- 22: the brake and the lock ---------------------------------------------


async def test_an_open_circuit_breaker_sends_nothing(hass):
    gateway, coordinator = setup(hass)
    coordinator._circuit_open_until = monotonic() + 60

    with pytest.raises(WriteValidationError):
        await coordinator.async_write_control(ELECTRICITY, 0.31, policy_of(POLLED[1]))

    assert gateway.puts == [] and gateway.reads == []


async def test_a_running_poll_makes_the_write_wait_or_refuse(hass, monkeypatch):
    gateway, coordinator = setup(hass)
    monkeypatch.setattr(
        coordinator_module, "WRITE_LOCK_TIMEOUT", timedelta(milliseconds=30)
    )
    await coordinator._update_lock.acquire()
    try:
        with pytest.raises(WriteBusy):
            await coordinator.async_write_control(
                ELECTRICITY, 0.31, policy_of(POLLED[1])
            )
    finally:
        coordinator._update_lock.release()
    assert gateway.puts == [] and gateway.reads == []

    # Free again: the same change goes through.
    await coordinator.async_write_control(ELECTRICITY, 0.31, policy_of(POLLED[1]))
    assert gateway.puts == [(ELECTRICITY, 0.31)]


# --- 23: a restart after a write ---------------------------------------------


async def test_after_a_restart_the_entities_return_with_the_values_of_the_next_poll(
    hass,
):
    gateway, coordinator = setup(hass)
    chosen, unload = await entities(hass, coordinator)
    await chosen[ELECTRICITY].async_set_native_value(0.31)
    for callback in unload:
        callback()

    # A new coordinator on the same plant: the same entities (same keys), the
    # value comes with the poll.
    restarted_gateway = FakeGateway(*gateway.state.values())
    restarted = coordinator_for(hass, restarted_gateway, *gateway.state.values())
    again, _ = await entities(hass, restarted)
    assert set(again) == set(chosen)
    for path, entity in again.items():
        assert entity.unique_id == chosen[path].unique_id
    assert again[ELECTRICITY].native_value == 0.31
    assert restarted_gateway.puts == []


# --- the paths of a plant without them ---------------------------------------


async def test_a_plant_without_the_paths_gets_no_entity_and_no_request(hass):
    other = price("/system/energyTariff/night/electricity/value", 0.2)
    gateway, coordinator = setup(hass, other)
    chosen, _ = await entities(hass, coordinator)
    assert chosen == {}
    assert gateway.puts == [] and gateway.reads == []
    assert set(plant()) == set(hybrid.CONTROL_PATHS)
