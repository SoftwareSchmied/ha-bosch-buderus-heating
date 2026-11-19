"""One hold for every cloud family (X12, see ``cloud_hold``).

The generic controls got a grace period over a short cloud blip in 0.9.2. The
room controllers (also zones and radio devices), the air conditioners and
wallboxes of a PointT gateway and the separately connected cloud devices follow
the same rules now: at most three failed cycles in a row and five minutes, never
after a rejected sign-in or an explicit refusal (403, 404 ...), and no change is
sent during the window (a change reads the setting again and is refused without
a current reading).

Every test runs real cycles: the coordinators' own refresh on the plants of the
room-controller tests with a simulated clock, the air-conditioner and wallbox
coordinators on their synthetic units, the MQTT account of the second cloud with
its real connection loop in the integration tests of that family.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.bosch_buderus_heating import aircon, family_hold
from custom_components.bosch_buderus_heating.aircon_climate import AirconClimate
from custom_components.bosch_buderus_heating.aircon_entities import _switches
from custom_components.bosch_buderus_heating.aircon_policies import POLL_PATHS
from custom_components.bosch_buderus_heating.bacon.models import BaconError
from custom_components.bosch_buderus_heating.cloud_hold import (
    GRACE_CYCLES,
    GRACE_MAX_SECONDS,
)
from custom_components.bosch_buderus_heating.pointt import Gateway
from custom_components.bosch_buderus_heating.pointt.exceptions import (
    AuthenticationError,
    RateLimited,
    RequestTimeout,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.wallbox_entities import (
    WallboxSwitch,
)
from custom_components.bosch_buderus_heating.wallbox_sensor import (
    DESCRIPTIONS as WALLBOX_SENSORS,
)
from custom_components.bosch_buderus_heating.wallbox_sensor import WallboxSensor
from tests.aircon_helpers import (  # noqa: F401
    FakeUnit,
    aircon_cleanup,
    make_coordinator,
)
from tests.helpers.rrc_absence import absence_tree
from tests.helpers.rrc_absence import setup as absence_setup
from tests.helpers.rrc_absence import teardown as absence_teardown
from tests.helpers.rrc_cloud import RrcCloud, cloud_patches
from tests.test_bacon_session import MODULE as BACON_SESSION
from tests.test_bacon_session import environment as bacon_environment
from tests.test_discovery_recovery import Clock
from tests.test_entry import _entry
from tests.test_rrc_circuit import (
    _setup as circuit_setup,
)
from tests.test_rrc_circuit import (
    _teardown as circuit_teardown,
)
from tests.test_rrc_circuit import (
    circuit_tree,
    legacy_setup_path,  # noqa: F401
)
from tests.test_rrc_ct200_setup import ct200_tree
from tests.test_rrc_device_settings import (
    OFFSET_ID,
    SENSITIVITY_ID,
    _enable,
)
from tests.test_rrc_device_settings import _setup as device_setup
from tests.test_rrc_device_settings import _teardown as device_teardown
from tests.test_rrc_device_settings import tree as device_tree
from tests.test_rrc_dhw import DISABLED as DHW_DISABLED
from tests.test_rrc_dhw import _setup as dhw_setup
from tests.test_rrc_dhw import _teardown as dhw_teardown
from tests.test_rrc_dhw import tree as dhw_tree
from tests.test_wallbox_controls import _spec, settle, wallbox

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

BLIP = ServiceUnavailable(503)


# -------------------------------------------------- the room controllers


def _kinds(hass: HomeAssistant) -> dict[str, str]:
    return {
        entity.entity_id: type(entity).__name__
        for component in hass.data["entity_components"].values()
        for entity in component.entities
    }


def _available(hass: HomeAssistant) -> set[str]:
    return {
        state.entity_id
        for state in hass.states.async_all()
        if state.state != "unavailable"
    }


def _entity(hass: HomeAssistant, kind: str):
    found = [
        entity
        for component in hass.data["entity_components"].values()
        for entity in component.entities
        if type(entity).__name__ == kind
    ]
    assert found, kind
    return found[0]


async def _cycle(hass, coordinator, clock, step=60.0) -> None:
    clock.now += step
    await coordinator.async_refresh()
    await hass.async_block_till_done()


async def _due_cycle(hass, coordinator, clock) -> None:
    """A cycle one minute later in which every group of readings is due."""
    clock.now += 60
    for group in coordinator._poll_intervals:
        coordinator._next_update[group] = clock.now
    await coordinator.async_refresh()
    await hass.async_block_till_done()


async def _hold_window(
    hass: HomeAssistant,
    coordinator,
    cloud: RrcCloud,
    clock,
    expected: set[str],
    refused: Callable[[], Awaitable[None]] | None = None,
    kept: frozenset[str] = frozenset(),
) -> None:
    """Three failed cycles keep every entity, the fourth ends it, the
    return restores it. ``expected`` are the entity classes of the family that
    end with the hold; ``kept`` are entities of the family that stay shown by their
    reading validity as before; ``refused`` is a change that must send nothing."""
    for _ in range(2):
        await _cycle(hass, coordinator, clock)
    before = _available(hass)
    kinds = _kinds(hass)
    present = {kinds[entity] for entity in before}
    assert (expected | kept) <= present, (expected | kept) - present
    written = len(cloud.written())
    cloud.bulk_error = BLIP
    for failed in range(1, GRACE_CYCLES + 1):
        await _cycle(hass, coordinator, clock)
        assert not coordinator.last_update_success and coordinator.cloud_holding()
        lost = sorted(before - _available(hass))
        assert not lost, (failed, [kinds[item] for item in lost])
        if refused is not None and failed == 2:
            with pytest.raises(HomeAssistantError):
                await refused()
            assert len(cloud.written()) == written
    await _cycle(hass, coordinator, clock)
    assert not coordinator.cloud_holding()
    ended = Counter(kinds[item] for item in before - _available(hass))
    assert expected <= set(ended), expected - set(ended)
    cloud.bulk_error = None
    clock.now += 300
    await _cycle(hass, coordinator, clock)
    assert coordinator.last_update_success
    assert before <= _available(hass), before - _available(hass)
    assert coordinator.cloud_hold.expired == 1


async def test_ct200_zones_devices_and_program_selects_hold_three_cycles(
    hass: HomeAssistant,
) -> None:
    """Zones, radio devices, valve problems and the program selects of a large
    CT200 plant; a program change is refused during the window."""
    entry, cloud, clock = _entry(hass), RrcCloud(ct200_tree()), Clock()
    with cloud_patches(cloud, clock, Gateway("gateway-one", device_type="rrc2")):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinators[0]

        async def change_program() -> None:
            select = _entity(hass, "RrcProgramSelect")
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": select.entity_id, "option": select.options[-1]},
                blocking=True,
            )

        await _hold_window(
            hass,
            coordinator,
            cloud,
            clock,
            {"RrcProgramSelect", "ValveProblemBinarySensor"},
            change_program,
            kept=frozenset(
                {"RrcZoneClimate", "RrcZoneSensor", "RadioBatteryLowBinarySensor"}
            ),
        )
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_boost_hold_and_no_change_during_the_window(
    hass: HomeAssistant,
) -> None:
    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await absence_setup(hass, cloud)
    try:

        async def start_boost() -> None:
            switch = _entity(hass, "RrcBoostSwitch")
            await hass.services.async_call(
                "switch", "turn_on", {"entity_id": switch.entity_id}, blocking=True
            )

        await _hold_window(
            hass,
            coordinator,
            cloud,
            clock,
            {"RrcBoostSwitch", "RrcBoostNumber"},
            start_boost,
        )
    finally:
        await absence_teardown(hass, entry, stack)


async def test_hot_water_sensors_and_disinfection_time_hold(
    hass: HomeAssistant,
) -> None:
    cloud, clock = RrcCloud(dhw_tree()), Clock()
    entry, coordinator, _sleeps, stack = await dhw_setup(
        hass, cloud, clock, DHW_DISABLED
    )
    try:

        async def change_time() -> None:
            time = _entity(hass, "RrcDhwTime")
            await hass.services.async_call(
                "time",
                "set_value",
                {"entity_id": time.entity_id, "time": "04:00:00"},
                blocking=True,
            )

        await _hold_window(
            hass,
            coordinator,
            cloud,
            clock,
            {"RrcDhwSensor", "RrcDhwTime"},
            change_time,
        )
    finally:
        await dhw_teardown(hass, entry, stack)


async def test_operating_season_sensor_holds(hass: HomeAssistant) -> None:
    cloud = RrcCloud(circuit_tree())
    entry, coordinator, _sleeps, stack, clock = await circuit_setup(hass, cloud)
    try:
        await _hold_window(
            hass, coordinator, cloud, clock, {"RrcOperatingSeasonSensor"}
        )
    finally:
        await circuit_teardown(hass, entry, stack)


async def test_device_settings_hold_and_the_sensitivity_is_not_changed(
    hass: HomeAssistant,
) -> None:
    cloud, clock = RrcCloud(device_tree()), Clock()
    entry, stack = await device_setup(hass, cloud, clock)
    try:
        await _enable(hass, entry, OFFSET_ID, SENSITIVITY_ID)
        coordinator = entry.runtime_data.coordinators[0]

        async def change_sensitivity() -> None:
            select = _entity(hass, "WindowSensitivitySelect")
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": select.entity_id, "option": select.options[0]},
                blocking=True,
            )

        await _hold_window(
            hass,
            coordinator,
            cloud,
            clock,
            {"WindowSensitivitySelect", "ValveProblemBinarySensor"},
            change_sensitivity,
        )
    finally:
        await device_teardown(hass, entry, stack)


async def test_one_failed_reading_of_the_boost_is_held_but_a_refusal_is_not(
    hass: HomeAssistant,
) -> None:
    """A single reading that gets no answer (503) keeps the last value; an
    explicit answer (404) never does."""
    from custom_components.bosch_buderus_heating.rrc_boost import BOOST_PATH

    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await absence_setup(hass, cloud)
    try:
        await _due_cycle(hass, coordinator, clock)
        switch = _entity(hass, "RrcBoostSwitch")
        assert switch.available
        cloud.down[BOOST_PATH] = 503
        await _due_cycle(hass, coordinator, clock)
        assert coordinator.last_update_success
        assert 1 <= coordinator.data[BOOST_PATH].consecutive_failures <= GRACE_CYCLES
        assert switch.available
        cloud.down[BOOST_PATH] = 404
        await _due_cycle(hass, coordinator, clock)
        assert not coordinator.data[BOOST_PATH].available
        assert not switch.available
    finally:
        await absence_teardown(hass, entry, stack)


async def test_rejected_sign_in_is_never_held_for_room_controllers(
    hass: HomeAssistant,
) -> None:
    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await absence_setup(hass, cloud)
    try:
        await _cycle(hass, coordinator, clock)
        before = _available(hass)
        cloud.bulk_error = AuthenticationError("rejected")
        with patch.object(entry, "async_start_reauth"):
            await _cycle(hass, coordinator, clock)
        assert not coordinator.cloud_holding()
        switch = _entity(hass, "RrcBoostSwitch")
        assert not switch.available
        assert before - _available(hass)
    finally:
        await absence_teardown(hass, entry, stack)


# -------------------------------------------------- the air conditioners


class Time:
    """A controllable clock for the hold and the readings of one family."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


def _climate(hass: HomeAssistant, coordinator) -> AirconClimate:
    entity = AirconClimate(coordinator)
    entity.hass = hass
    return entity


async def test_air_conditioner_entities_hold_three_failed_cycles(
    hass: HomeAssistant,
) -> None:
    coordinator, unit, _ = await make_coordinator(hass)
    climate = _climate(hass, coordinator)
    switch = dict(_switches(coordinator))["airConditioning:sleepMode"]()
    switch.hass = hass
    values = (climate.hvac_mode, climate.current_temperature, switch.is_on)
    assert None not in values and climate.available and switch.available
    heard: list[bool] = []
    coordinator.async_add_listener(lambda: heard.append(climate.available))

    unit.bulk_error = BLIP
    for failed in range(1, GRACE_CYCLES + 1):
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        assert coordinator.cloud_holding(), failed
        assert climate.available and switch.available
        assert (climate.hvac_mode, climate.current_temperature, switch.is_on) == values
    assert coordinator.cloud_hold.failures == GRACE_CYCLES

    # The fourth failed cycle ends the hold, and the entities hear it.
    await coordinator.async_refresh()
    assert not coordinator.cloud_holding()
    assert not climate.available and not switch.available
    assert heard[-1] is False

    unit.bulk_error = None
    await coordinator.async_refresh()
    assert coordinator.last_update_success and climate.available and switch.available
    assert (climate.hvac_mode, climate.current_temperature, switch.is_on) == values
    hold = coordinator.diagnostics()["cloud_hold"]
    assert hold == {
        "grace_cycles": 3,
        "grace_max_seconds": 300.0,
        "holding": False,
        "failed_cycles_in_a_row": 0,
        "holds_started": 1,
        "holds_expired": 1,
    }


@pytest.mark.parametrize(
    "error",
    [BLIP, RequestTimeout(), RateLimited(30)],
    ids=["server_error", "timeout", "rate_limited"],
)
async def test_air_conditioner_holds_every_kind_of_blip(
    hass: HomeAssistant, error: Exception
) -> None:
    coordinator, unit, _ = await make_coordinator(hass)
    climate = _climate(hass, coordinator)
    unit.bulk_error = error
    await coordinator.async_refresh()
    assert not coordinator.last_update_success and climate.available


async def test_air_conditioner_with_only_server_errors_in_the_answer_is_held(
    hass: HomeAssistant,
) -> None:
    coordinator, unit, _ = await make_coordinator(hass)
    climate = _climate(hass, coordinator)
    unit.status.update(dict.fromkeys((*POLL_PATHS, *coordinator._program_paths()), 503))
    await coordinator.async_refresh()
    assert coordinator.error == "no_reading"
    assert not coordinator.last_update_success and climate.available


async def test_air_conditioner_sends_no_change_during_the_window(
    hass: HomeAssistant,
) -> None:
    coordinator, unit, _ = await make_coordinator(hass)
    switch = dict(_switches(coordinator))["airConditioning:sleepMode"]()
    switch.hass = hass
    unit.bulk_error = BLIP
    await coordinator.async_refresh()
    assert switch.available
    calls = (len(unit.bulk_calls), len(unit.gets), len(unit.puts))
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()
    # Not even a reading went out; the change reads again when the cloud is back.
    assert (len(unit.bulk_calls), len(unit.gets), len(unit.puts)) == calls
    assert unit.puts == []
    unit.bulk_error = None
    await coordinator.async_refresh()
    await switch.async_turn_on()
    assert unit.puts == [("/airConditioning/sleepMode", "on")]


async def test_air_conditioner_hold_ends_by_time(hass: HomeAssistant) -> None:
    coordinator, unit, _ = await make_coordinator(hass)
    climate = _climate(hass, coordinator)
    clock = Time()
    with (
        patch(family_hold.__name__ + ".monotonic", clock),
        patch(aircon.__name__ + ".monotonic", clock),
    ):
        coordinator.data = aircon.AirconSnapshot(
            coordinator.data.resources, coordinator.data.missing, clock.now
        )
        unit.bulk_error = BLIP
        await coordinator.async_refresh()
        assert coordinator.cloud_holding() and climate.available
        clock.now += GRACE_MAX_SECONDS - 10
        assert coordinator.cloud_holding() and climate.available
        clock.now += 11
        assert not coordinator.cloud_holding() and not climate.available
        assert coordinator.cloud_hold.failures == 1


async def test_air_conditioner_refusals_and_errors_are_never_held(
    hass: HomeAssistant,
) -> None:
    # An explicit answer (404 for every path of the unit).
    coordinator, unit, _ = await make_coordinator(hass)
    climate = _climate(hass, coordinator)
    unit.resources.clear()
    await coordinator.async_refresh()
    assert coordinator.error == "no_reading" and not coordinator.last_update_success
    assert not coordinator.cloud_holding() and not climate.available
    unit.resources.update(FakeUnit().resources)
    await coordinator.async_refresh()
    assert climate.available
    # A rejected sign-in.
    unit.bulk_error = AuthenticationError("rejected")
    with patch.object(coordinator.config_entry, "async_start_reauth"):
        await coordinator.async_refresh()
    assert not coordinator.cloud_holding() and not climate.available
    # An unexpected error.
    unit.bulk_error = None
    await coordinator.async_refresh()
    assert climate.available
    unit.bulk_error = RuntimeError("unexpected")
    await coordinator.async_refresh()
    assert not coordinator.cloud_holding() and not climate.available
    assert coordinator.cloud_hold.started == 0 or coordinator.cloud_hold.failures == 0


async def test_air_conditioner_of_a_stopped_source_is_not_held(
    hass: HomeAssistant,
) -> None:
    coordinator, unit, _ = await make_coordinator(hass)
    climate = _climate(hass, coordinator)
    unit.bulk_error = BLIP
    await coordinator.async_refresh()
    assert climate.available
    with patch.object(coordinator, "_source_stopped", return_value=True):
        assert not coordinator.cloud_holding() and not climate.available


# ------------------------------------------------------------- the wallbox


def _sensor(coordinator) -> WallboxSensor:
    description = next(item for item in WALLBOX_SENSORS if item.section == "telemetry")
    return WallboxSensor(coordinator, description)


async def _failing(coordinator, fake, error=BLIP) -> None:
    fake.errors["telemetry"] = error
    coordinator._next.clear()
    await coordinator.async_refresh()


async def test_wallbox_entities_hold_three_failed_cycles(
    hass: HomeAssistant,
) -> None:
    coordinator, fake = await wallbox(hass)
    sensor, switch = _sensor(coordinator), WallboxSwitch(coordinator, _spec("locked"))
    leaf = next(iter(coordinator.leaves))
    values = (sensor.native_value, switch.is_on)
    assert None not in values and sensor.available and switch.available
    assert coordinator.leaf_fresh(leaf)
    heard: list[bool] = []
    coordinator.async_add_listener(lambda: heard.append(sensor.available))

    for failed in range(1, GRACE_CYCLES + 1):
        await _failing(coordinator, fake)
        assert not coordinator.last_update_success and coordinator.cloud_holding()
        assert sensor.available and switch.available, failed
        assert (sensor.native_value, switch.is_on) == values
        assert coordinator.leaf_fresh(leaf)
        # The stored objects are cleared as before; only the display is held.
        assert all(item.error for item in coordinator.data.values())

    await _failing(coordinator, fake)
    assert not coordinator.cloud_holding()
    assert not sensor.available and not switch.available
    assert not coordinator.leaf_fresh(leaf)
    assert heard[-1] is False

    fake.errors.clear()
    coordinator._next.clear()
    await coordinator.async_refresh()
    assert coordinator.last_update_success and sensor.available and switch.available
    assert (sensor.native_value, switch.is_on) == values
    hold = coordinator.diagnostics()["cloud_hold"]
    assert (hold["holds_started"], hold["holds_expired"]) == (1, 1)
    assert hold["failed_cycles_in_a_row"] == 0 and not hold["holding"]


async def test_wallbox_sends_no_change_during_the_window(hass: HomeAssistant) -> None:
    coordinator, fake = await wallbox(hass)
    settle(fake)
    switch = WallboxSwitch(coordinator, _spec("locked"))
    await _failing(coordinator, fake)
    assert switch.available
    fake.calls.clear()
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()
    assert fake.writes() == [] and fake.calls == []
    fake.errors.clear()
    coordinator._next.clear()
    await coordinator.async_refresh()
    fake.calls.clear()
    await switch.async_turn_on()
    assert [call[0] for call in fake.writes()] == ["PUT"]


async def test_wallbox_hold_ends_by_time(hass: HomeAssistant) -> None:
    coordinator, fake = await wallbox(hass)
    sensor = _sensor(coordinator)
    clock = Time()
    with (
        patch(family_hold.__name__ + ".monotonic", clock),
        patch("custom_components.bosch_buderus_heating.wallbox.monotonic", clock),
    ):
        coordinator.data = {
            section: type(snapshot)(
                snapshot.values, clock.now, snapshot.error, snapshot.unknown_names
            )
            if hasattr(snapshot, "values")
            else snapshot
            for section, snapshot in coordinator.data.items()
        }
        await _failing(coordinator, fake)
        assert coordinator.cloud_holding() and sensor.available
        clock.now += GRACE_MAX_SECONDS - 10
        assert coordinator.cloud_holding() and sensor.available
        clock.now += 11
        assert not coordinator.cloud_holding() and not sensor.available


@pytest.mark.parametrize(
    "error", [RequestTimeout(), RateLimited(30)], ids=["timeout", "rate_limited"]
)
async def test_wallbox_holds_every_kind_of_blip(
    hass: HomeAssistant, error: Exception
) -> None:
    coordinator, fake = await wallbox(hass)
    sensor = _sensor(coordinator)
    await _failing(coordinator, fake, error)
    assert not coordinator.last_update_success and sensor.available


async def test_wallbox_refusals_and_errors_are_never_held(
    hass: HomeAssistant,
) -> None:
    from custom_components.bosch_buderus_heating.pointt.exceptions import (
        ResourceNotFound,
    )

    coordinator, fake = await wallbox(hass)
    sensor, switch = _sensor(coordinator), WallboxSwitch(coordinator, _spec("locked"))
    # An explicit answer for an object: the cycle succeeds, the object is gone.
    fake.errors["conf"] = ResourceNotFound("conf", 404)
    coordinator._next.clear()
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert sensor.available and not coordinator.section_fresh("conf")
    fake.errors.clear()
    coordinator._next.clear()
    await coordinator.async_refresh()
    assert switch.available
    # A rejected sign-in.
    with patch.object(coordinator.config_entry, "async_start_reauth"):
        await _failing(coordinator, fake, AuthenticationError("rejected"))
    assert not coordinator.cloud_holding() and not sensor.available
    fake.errors.clear()
    coordinator._next.clear()
    await coordinator.async_refresh()
    assert sensor.available
    # An unexpected error.
    await _failing(coordinator, fake, RuntimeError("unexpected"))
    assert not coordinator.cloud_holding() and not sensor.available


async def test_wallbox_of_a_stopped_source_is_not_held(hass: HomeAssistant) -> None:
    coordinator, fake = await wallbox(hass)
    sensor = _sensor(coordinator)
    await _failing(coordinator, fake)
    assert sensor.available
    with patch.object(coordinator, "_hold_blocked", return_value=True):
        assert not coordinator.cloud_holding() and not sensor.available


# ------------------------------------------- the separately connected devices


async def _connections(account, failures, *, stop_after, between=None):
    """The real connection loop with scripted outcomes; what the devices show
    at each wait before the next attempt."""
    account.run_connection = AsyncMock(side_effect=failures)
    seen: list[bool] = []

    async def sleep(delay: float) -> None:
        seen.append(account.available("serial-A"))
        if between is not None:
            between(len(seen))
        if len(seen) == stop_after:
            raise asyncio.CancelledError

    with (
        patch(BACON_SESSION + ".asyncio.sleep", side_effect=sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await account.run()
    return seen


async def test_a_lost_connection_is_held_for_three_failed_attempts() -> None:
    account = bacon_environment()
    heard: list[bool] = []
    account.add_listener(lambda: heard.append(account.available("serial-A")))
    heard_at: list[int] = []
    seen = await _connections(
        account,
        [BaconError("connection")] * 5,
        stop_after=GRACE_CYCLES + 1,
        between=lambda count: heard_at.append(len(heard)),
    )
    # Three failed connections are held; the fourth ends the hold, and the
    # entities hear it once.
    assert seen == [True, True, True, False]
    assert heard_at == [0, 0, 0, 1] and heard[0] is False
    assert account.cloud_hold.expired == 1


async def test_a_planned_renewal_and_failed_reconnects_count_together() -> None:
    account = bacon_environment()
    seen = await _connections(
        account,
        [None, *[BaconError("service_unavailable")] * 5],
        stop_after=GRACE_CYCLES + 1,
    )
    # The renewal itself is no failure: the three failed reconnects are held.
    assert seen == [True, True, True, False]


async def test_a_lost_connection_is_held_for_five_minutes_at_most() -> None:
    account = bacon_environment()
    clock = Time()
    with patch(BACON_SESSION + ".monotonic", clock):

        def later(count: int) -> None:
            clock.now += GRACE_MAX_SECONDS / 2 + 1

        seen = await _connections(
            account, [BaconError("connection")] * 4, stop_after=3, between=later
        )
    assert seen == [True, True, False]


@pytest.mark.parametrize(
    "category",
    [
        "authentication",
        "account_changed",
        "source_stopped",
        "devices_unavailable",
        "subscription_rejected",
        "invalid_device_list",
    ],
)
async def test_no_hold_after_a_rejection_or_a_change_of_the_account(
    category: str,
) -> None:
    account = bacon_environment()
    seen: list[bool] = []
    account.run_connection = AsyncMock(side_effect=[BaconError(category)] * 3)

    async def sleep(delay: float) -> None:
        seen.append(account.available("serial-A"))
        raise asyncio.CancelledError

    with patch(BACON_SESSION + ".asyncio.sleep", side_effect=sleep):
        if category in {"authentication", "account_changed"}:
            await account.run()
        else:
            with pytest.raises(asyncio.CancelledError):
                await account.run()
    assert not account.available("serial-A")
    assert seen in ([], [False])
    assert account.cloud_hold.started == 0


async def test_an_unexpected_error_is_no_blip() -> None:
    account = bacon_environment()
    seen = await _connections(account, [RuntimeError("private")] * 2, stop_after=1)
    assert seen == [False]


async def test_a_message_of_the_new_connection_ends_the_outage() -> None:
    account = bacon_environment()
    account._disconnected(failure="connection")
    assert account.available("serial-A") and account.carried("serial-A")
    diagnostics = account.diagnostics()["cloud_hold"]
    assert diagnostics["holding"] and diagnostics["failed_cycles_in_a_row"] == 1
    account.connected = True
    account.generation += 1
    from tests.test_bacon_session import emit

    emit(account, "topics/info", {"online": True, "timestamp": 2}, retained=True)
    emit(
        account,
        "topics/info",
        {"online": True, "timestamp": 2},
        "serial-B",
        retained=True,
    )
    after = account.diagnostics()["cloud_hold"]
    assert not after["holding"] and after["failed_cycles_in_a_row"] == 0
    assert not account.carried("serial-A")
    # The next loss is a new outage with its own count.
    account._disconnected(failure="connection")
    assert account.diagnostics()["cloud_hold"]["holds_started"] == 2
    assert account.diagnostics()["cloud_hold"]["failed_cycles_in_a_row"] == 1
    account._end_grace()


async def test_a_held_device_is_not_written() -> None:
    account = bacon_environment()
    account._disconnected(failure="connection")
    assert account.available("serial-A")
    account._mqtt = AsyncMock()
    with pytest.raises(BaconError, match="connection"):
        await account.write_state(
            "serial-A", {"powerEnabled": False}, lambda d, x: True
        )
    account._mqtt.publish.assert_not_called()
    account._end_grace()
