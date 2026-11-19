"""A short cloud blip keeps the last values of cloud-dependent entities (H-B3-2).

The cycles run through the coordinator's own refresh on the anonymized cloud
inventory of a heat pump, with a simulated clock (the harness of
``test_cloud_outages_plant``). The sign-in service is the real ``TokenManager``
with an access token that runs out on the same clock, so a slow or failing
renewal meets real polling cycles.
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.bosch_buderus_heating import text as text_platform
from custom_components.bosch_buderus_heating.cloud_hold import (
    GRACE_CYCLES,
    GRACE_MAX_SECONDS,
    CloudHold,
    LastFailedRefresh,
)
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.held_readings import (
    cloud_usable,
    held_snapshot,
)
from custom_components.bosch_buderus_heating.number import (
    BoschBuderusNumber,
    build_number_descriptions,
)
from custom_components.bosch_buderus_heating.performance_history import Period
from custom_components.bosch_buderus_heating.performance_sensors import (
    CurrentCopSensor,
    PerformanceFactorSensor,
    PrimarySpreadSensor,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    AuthTokens,
    RequestTimeout,
    SignInUnavailable,
    TokenManager,
)
from custom_components.bosch_buderus_heating.pointt_water_heater import (
    PointTWaterHeater,
)
from custom_components.bosch_buderus_heating.select import (
    BoschBuderusSelect,
    build_select_descriptions,
)
from custom_components.bosch_buderus_heating.switch import (
    BoschBuderusSwitch,
    build_switch_descriptions,
)
from tests.program_kind_helpers import load, resource
from tests.test_cloud_outages_plant import (
    INTERVAL,
    Cloud,
    _plant,
    _refresh,
)
from tests.test_cloud_polling_plan import Clock

ROOT = "/dhwCircuits/dhw1"
NUMBER = ROOT + "/temperatureLevels/high"
SELECT = "/heatingCircuits/hc1/operationMode"
SWITCH = "/system/awayMode/enabled"


class Entities:
    """One of each kind of entity that shows a cloud value or a calculation."""

    def __init__(self, hass: HomeAssistant, coordinator, cloud: Cloud) -> None:
        self.coordinator = coordinator
        resources = coordinator.resources
        number = next(
            item
            for item in build_number_descriptions(resources)
            if item.resource_path == NUMBER
        )
        select = next(
            item
            for item in build_select_descriptions(resources)
            if item.resource_path == SELECT
        )
        switch = next(
            item
            for item in build_switch_descriptions(resources)
            if item.resource_path == SWITCH
        )
        self.number = BoschBuderusNumber(coordinator, number)
        self.number.hass = hass
        self.select = BoschBuderusSelect(coordinator, select)
        self.switch = BoschBuderusSwitch(coordinator, switch)
        self.heater = PointTWaterHeater(coordinator, ROOT)
        self.heater.hass = hass
        self.cop = CurrentCopSensor(coordinator)
        self.spread = PrimarySpreadSensor(coordinator)
        self.factor = PerformanceFactorSensor(
            coordinator, Mock(), Period.TODAY, "total"
        )
        # A program name (the text control), read once and kept.
        name = resource(
            load("connectkey/switch-program-names.json")["cases"]["ascii_plain"]
        )
        cloud.resources[name.path] = name
        coordinator.resources[name.path] = name
        coordinator.data[name.path] = ResourceSnapshot(name, True, datetime.now(UTC))
        (description,) = text_platform.descriptions({name.path: name})
        self.text = text_platform.ProgramNameText(coordinator, description)

    def available(self) -> dict[str, bool]:
        return {
            "number": self.number.available,
            "select": self.select.available,
            "switch": self.switch.available,
            "water_heater": self.heater.available,
            "cop": self.cop.available,
            "spread": self.spread.available,
            "factor": self.factor.available,
            "text": self.text.available,
        }

    def all_available(self) -> bool:
        return all(self.available().values())

    def none_available(self) -> bool:
        return not any(self.available().values())

    def values(self) -> tuple[object, ...]:
        return (
            self.number.native_value,
            self.select.current_option,
            self.switch.is_on,
            self.heater.current_operation,
            self.heater.native_current_temperature,
            self.text.native_value,
        )


def _all_due(coordinator: BoschBuderusDataUpdateCoordinator, clock: Clock) -> None:
    for group in coordinator._poll_intervals:
        coordinator._next_update[group] = clock.now


async def _settle(manager: TokenManager) -> None:
    """Let a renewal in the background finish; its failure is the manager's."""
    with suppress(SignInUnavailable):
        await manager.async_wait_for_refresh()


async def _cycle(coordinator: BoschBuderusDataUpdateCoordinator, clock: Clock) -> None:
    """The next cycle one interval later; every group is due."""
    clock.now += INTERVAL
    _all_due(coordinator, clock)
    await _refresh(coordinator)


def _fail(cloud: Cloud, status: int = 504) -> None:
    cloud.all_status = cloud.get_status = status


def _recover(cloud: Cloud) -> None:
    cloud.all_status = cloud.get_status = None


# ------------------------------------------------- a failed refresh is held


async def test_entities_keep_their_values_for_three_failed_cycles(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        await _cycle(coordinator, clock)
        assert coordinator.last_update_success
        assert entities.all_available(), f"{entities.available()}"
        values = entities.values()
        assert None not in values
        notified: list[bool] = []
        coordinator.async_add_listener(
            lambda: notified.append(entities.all_available())
        )

        _fail(cloud)
        for failed in range(1, GRACE_CYCLES + 1):
            await _cycle(coordinator, clock)
            assert not coordinator.last_update_success
            assert coordinator.cloud_holding(), failed
            assert entities.all_available(), failed
            assert entities.values() == values
        assert coordinator.cloud_hold.failures == GRACE_CYCLES

        # The fourth failed cycle ends the hold, and the entities learn it.
        before = len(notified)
        await _cycle(coordinator, clock)
        assert not coordinator.cloud_holding()
        assert entities.none_available(), entities.available()
        assert len(notified) > before
        assert notified[-1] is False

        # The cloud answers again (after the protection pause of the failures).
        _recover(cloud)
        clock.now += 300
        await _cycle(coordinator, clock)
        assert coordinator.last_update_success and entities.all_available()
        assert entities.values() == values
        assert coordinator.cloud_hold.failures == 0
        assert coordinator.cloud_hold.started == 1
        assert coordinator.cloud_hold.expired == 1
        await coordinator.async_shutdown()


async def test_a_blip_of_one_cycle_never_shows_unavailable(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        await _cycle(coordinator, clock)
        seen: list[dict[str, bool]] = []
        coordinator.async_add_listener(lambda: seen.append(entities.available()))
        _fail(cloud)
        cloud.seconds = 10.56
        await _cycle(coordinator, clock)
        assert not coordinator.last_update_success
        _recover(cloud)
        cloud.seconds = 0.5
        await _cycle(coordinator, clock)
        assert coordinator.last_update_success
        assert seen and all(all(item.values()) for item in seen), seen
        assert coordinator.cloud_hold.expired == 0
        await coordinator.async_shutdown()


async def test_the_hold_ends_by_time_even_with_few_counted_cycles(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        await _cycle(coordinator, clock)
        _fail(cloud)
        await _cycle(coordinator, clock)
        assert coordinator.cloud_holding() and entities.all_available()
        # No further cycle ran, but the outage is older than the limit.
        clock.now += GRACE_MAX_SECONDS - 10
        assert coordinator.cloud_holding() and entities.all_available()
        clock.now += 11
        assert not coordinator.cloud_holding()
        assert entities.none_available()
        assert coordinator.cloud_hold.failures == 1
        await coordinator.async_shutdown()


async def test_the_listeners_hear_when_an_idle_cycle_ends_the_hold(
    hass: HomeAssistant,
) -> None:
    """Idle cycles (nothing due after a failure) are not counted: the time
    limit ends the hold, and the cycle that finds it over tells the entities."""
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        await _cycle(coordinator, clock)
        _fail(cloud)
        await _cycle(coordinator, clock)
        assert coordinator.cloud_holding()
        notified: list[bool] = []
        coordinator.async_add_listener(
            lambda: notified.append(entities.all_available())
        )
        # The next wake-up has nothing due; the limit passes during it.
        clock.now += GRACE_MAX_SECONDS - 20
        for group in coordinator._poll_intervals:
            coordinator._next_update[group] = clock.now + 3600
        locked = coordinator._async_update_data_locked

        async def slow() -> object:
            clock.now += 30
            return await locked()

        coordinator._async_update_data_locked = slow  # type: ignore[method-assign]
        await _refresh(coordinator)
        assert not coordinator.cloud_holding()
        assert False in notified
        assert entities.none_available()
        await coordinator.async_shutdown()


# ------------------------------------------ what is never held or never allowed


async def test_a_write_is_refused_while_values_are_held(hass: HomeAssistant) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        await _cycle(coordinator, clock)
        _fail(cloud)
        await _cycle(coordinator, clock)
        assert coordinator.cloud_holding() and entities.number.available
        client = coordinator.client
        client.put_resource_value.reset_mock()
        client.get_resources_bulk.reset_mock()
        with pytest.raises(HomeAssistantError):
            await entities.number.async_set_native_value(52.0)
        with pytest.raises(HomeAssistantError):
            await entities.select.async_select_option(entities.select.options[0])
        with pytest.raises(HomeAssistantError):
            await entities.switch.async_turn_off()
        client.put_resource_value.assert_not_awaited()
        reason, _until = coordinator.control_status()
        assert reason == "cloud_unreachable"
        await coordinator.async_shutdown()


async def test_a_held_reading_is_no_current_reading_for_a_write(
    hass: HomeAssistant,
) -> None:
    """The write itself needs a current stored reading, even if the cycle is
    fine and only this reading failed (it is shown held)."""
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        await _cycle(coordinator, clock)
        cloud.failing = {NUMBER: 500}
        cloud.get_status = 500
        await _cycle(coordinator, clock)
        assert coordinator.last_update_success
        stored = coordinator.data[NUMBER]
        assert not stored.available and stored.freshness is Freshness.STALE
        assert entities.number.available
        assert entities.number.native_value is not None
        client = coordinator.client
        client.put_resource_value.reset_mock()
        with pytest.raises(HomeAssistantError):
            await entities.number.async_set_native_value(52.0)
        client.put_resource_value.assert_not_awaited()
        await coordinator.async_shutdown()


async def test_a_failed_reading_is_held_up_to_three_failures(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        await _cycle(coordinator, clock)
        value = entities.number.native_value
        cloud.failing = {NUMBER: 500}
        cloud.get_status = 500
        await _cycle(coordinator, clock)
        assert coordinator.last_update_success
        assert coordinator.data[NUMBER].consecutive_failures == 1
        assert entities.number.available
        assert entities.number.native_value == value
        # The last good value outlives the answered cycles, as long as the
        # reading failed at most three times in a row.
        snapshot = coordinator.data[NUMBER]
        coordinator.data[NUMBER] = replace(snapshot, consecutive_failures=3)
        assert entities.number.available
        coordinator.data[NUMBER] = replace(snapshot, consecutive_failures=4)
        assert not entities.number.available
        await coordinator.async_shutdown()


async def test_a_reading_that_is_answered_with_an_error_is_not_held(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        await _cycle(coordinator, clock)
        assert entities.number.available
        del cloud.resources[NUMBER]  # the gateway answers 404 for it
        await _cycle(coordinator, clock)
        assert coordinator.last_update_success
        assert coordinator.data[NUMBER].ended
        assert not entities.number.available
        await coordinator.async_shutdown()


async def test_a_rejected_sign_in_is_not_held(hass: HomeAssistant) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        await _cycle(coordinator, clock)
        coordinator.client.get_resources_bulk.side_effect = AuthenticationError()
        await _cycle(coordinator, clock)
        assert not coordinator.last_update_success
        assert not coordinator.cloud_holding()
        assert entities.none_available()
        await coordinator.async_shutdown()


async def test_a_pending_sign_in_or_stopped_source_is_not_held(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        await _cycle(coordinator, clock)
        _fail(cloud)
        await _cycle(coordinator, clock)
        assert coordinator.cloud_holding()
        monkeypatch.setattr(coordinator, "_cloud_auth_required", lambda: True)
        assert not coordinator.cloud_holding()
        assert entities.none_available()
        monkeypatch.undo()
        assert coordinator.cloud_holding()
        monkeypatch.setattr(coordinator, "_cloud_source_stopped", lambda: True)
        assert not coordinator.cloud_holding()
        assert entities.none_available()
        await coordinator.async_shutdown()


async def test_an_unexpected_error_is_not_held(hass: HomeAssistant) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        await _cycle(coordinator, clock)
        coordinator.client.get_resources_bulk.side_effect = RuntimeError("bug")
        await _cycle(coordinator, clock)
        assert not coordinator.last_update_success
        assert not coordinator.cloud_holding()
        assert entities.none_available()
        await coordinator.async_shutdown()


async def test_the_hold_of_an_outage_is_cancelled_by_an_unexpected_error(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        await _cycle(coordinator, clock)
        _fail(cloud)
        await _cycle(coordinator, clock)
        assert coordinator.cloud_holding()
        coordinator.client.get_resources_bulk.side_effect = RuntimeError("bug")
        await _cycle(coordinator, clock)
        assert not coordinator.cloud_holding()
        assert entities.none_available()
        await coordinator.async_shutdown()


# ----------------------------------------- the token blip of the evidence


class SlowSignIn:
    """The sign-in service on the simulated clock."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.mode = "ok"
        self.requests: list[float] = []
        self.issued = 0

    async def refresh(self, refresh_token: str) -> AuthTokens:
        self.requests.append(self.clock.now)
        if self.mode == "timeout":
            # A request that did not connect in 10 s, as in the evidence.
            self.clock.now += 10.9
            raise RequestTimeout("OAuth token request timed out")
        self.issued += 1
        now = self.clock.now
        return AuthTokens(
            f"access-{self.issued}",
            f"refresh-{self.issued}",
            expires_at=now + 3600.0,
            issued_at=now,
        )


def _with_token(
    coordinator: BoschBuderusDataUpdateCoordinator,
    cloud: Cloud,
    clock: Clock,
) -> tuple[TokenManager, SlowSignIn]:
    """Every request of the cloud needs the access token, as with the client."""
    sign_in = SlowSignIn(clock)
    manager = TokenManager(
        sign_in,
        AuthTokens(
            "access-0",
            "refresh-0",
            expires_at=clock.now + 3600.0,
            issued_at=clock.now,
        ),
        lambda _tokens: None,
        clock=clock,
        retry_clock=clock,
    )
    client = coordinator.client
    bulk, get = cloud.bulk, cloud.get

    async def bulk_with_token(gateway, paths):
        await manager.get_access_token()
        return await bulk(gateway, paths)

    async def get_with_token(gateway, path, **kwargs):
        await manager.get_access_token()
        return await get(gateway, path, **kwargs)

    client.get_resources_bulk = AsyncMock(side_effect=bulk_with_token)
    client.get_resource = AsyncMock(side_effect=get_with_token)
    return manager, sign_in


async def test_a_failing_renewal_with_a_valid_token_fails_no_cycle(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        manager, sign_in = _with_token(coordinator, cloud, clock)
        sign_in.mode = "timeout"
        start = clock.now
        failed = 0
        # From a quiet start to 55 minutes: the renewal is due at 50 minutes
        # (ten minutes before the end); every attempt times out.
        while clock.now < start + 3300:
            await _cycle(coordinator, clock)
            await _settle(manager)
            failed += not coordinator.last_update_success
            assert entities.all_available()
        assert failed == 0
        assert coordinator.cloud_hold.started == 0
        assert manager.tokens.access_token == "access-0"
        # Attempts began ten minutes before the end and are paced by the wait
        # after a failure (30 s, doubling), not once per cycle.
        assert sign_in.requests
        assert min(sign_in.requests) >= start + 3000 - INTERVAL - 11
        assert 3 <= len(sign_in.requests) <= 6
        gaps = [
            b - a for a, b in zip(sign_in.requests, sign_in.requests[1:], strict=False)
        ]
        assert min(gaps) >= 30.0
        # The service answers again: the next attempt renews the token, still
        # without a failed cycle.
        sign_in.mode = "ok"
        while manager.tokens.access_token == "access-0":
            await _cycle(coordinator, clock)
            await _settle(manager)
            assert coordinator.last_update_success and entities.all_available()
            assert clock.now < start + 3570
        assert manager.tokens.access_token == "access-1"
        await coordinator.async_shutdown()


async def test_a_renewal_that_never_succeeds_fails_the_cycles_after_expiry(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        entities = Entities(hass, coordinator, cloud)
        manager, sign_in = _with_token(coordinator, cloud, clock)
        sign_in.mode = "timeout"
        start = clock.now
        expiry = start + 3600
        while clock.now + INTERVAL < expiry - 30:
            await _cycle(coordinator, clock)
            await _settle(manager)
            assert coordinator.last_update_success and entities.all_available()
        # The token runs out: the first request without a usable token fails
        # the cycle with the sign-in error; three failures are held.
        failed_held = 0
        while True:
            await _cycle(coordinator, clock)
            await _settle(manager)
            if not coordinator.last_update_success:
                break
        for _ in range(GRACE_CYCLES):
            assert coordinator.cloud_holding()
            assert entities.all_available()
            failed_held += 1
            await _cycle(coordinator, clock)
            await _settle(manager)
        assert failed_held == GRACE_CYCLES
        assert not coordinator.cloud_holding()
        assert entities.none_available()
        assert clock.now >= expiry - 30
        # The service recovers: the next renewal ends the outage.
        sign_in.mode = "ok"
        for _ in range(12):
            await _cycle(coordinator, clock)
            await _settle(manager)
            if coordinator.last_update_success:
                break
        assert coordinator.last_update_success and entities.all_available()
        await coordinator.async_shutdown()


# -------------------------------------------------------------- diagnostics


async def test_the_diagnostics_name_the_failed_readings(hass: HomeAssistant) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _cycle(coordinator, clock)
        assert coordinator.diagnostics_summary()["last_failed_refresh"] is None
        _fail(cloud, 504)
        await _cycle(coordinator, clock)
        summary = coordinator.diagnostics_summary()
        report = summary["last_failed_refresh"]
        assert isinstance(report, dict)
        assert report["cycle_failed"] is True
        assert report["category"] == "service_unavailable"
        assert report["failed_readings"] >= 1
        paths = report["paths"]
        assert 0 < len(paths) <= 10
        assert all(item["status"] == 504 for item in paths)
        assert all(item["category"] == "service_unavailable" for item in paths)
        assert any(item["path"].startswith("/") for item in paths)
        assert summary["cloud_hold"] == {
            "grace_cycles": 3,
            "grace_max_seconds": 300.0,
            "holding": True,
            "failed_cycles_in_a_row": 1,
            "holds_started": 1,
            "holds_expired": 0,
        }
        # No identifiers: the report carries templates and counts only.
        text = json.dumps(summary)
        assert "gateway-1" not in text
        # The report stays after the recovery (it is the last failed refresh).
        _recover(cloud)
        await _cycle(coordinator, clock)
        assert coordinator.diagnostics_summary()["last_failed_refresh"] == report
        await coordinator.async_shutdown()


async def test_a_partly_failed_refresh_is_reported_as_not_failed(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _cycle(coordinator, clock)
        cloud.failing = {NUMBER: 500}
        cloud.get_status = 500
        await _cycle(coordinator, clock)
        report = coordinator.diagnostics_summary()["last_failed_refresh"]
        assert isinstance(report, dict)
        assert report["cycle_failed"] is False
        assert report["category"] is None
        assert [item["path"] for item in report["paths"]] == [
            "/dhwCircuits/{dhw}/temperatureLevels/high"
        ]
        assert report["paths"][0]["status"] == 500
        await coordinator.async_shutdown()


# ------------------------------------------------------------ the pieces


def test_the_hold_counts_failures_and_time() -> None:
    hold = CloudHold()
    assert not hold.holding(0.0)
    for count in range(1, GRACE_CYCLES + 1):
        hold.failed(10.0 * count)
        assert hold.holding(10.0 * count)
    hold.failed(40.0)
    assert not hold.holding(40.0)
    assert hold.expired == 1
    hold.recovered()
    assert hold.failures == 0 and not hold.holding(41.0)
    hold.failed(100.0)
    assert hold.holding(100.0 + GRACE_MAX_SECONDS)
    assert not hold.holding(100.0 + GRACE_MAX_SECONDS + 0.1)
    assert hold.started == 2 and hold.expired == 2
    hold.recovered()
    hold.failed(0.0)
    hold.cancel()
    assert not hold.holding(1.0)
    hold.recovered()
    hold.failed(0.0)
    assert hold.holding(1.0)


def test_the_report_keeps_templates_counts_and_a_bound() -> None:
    report = LastFailedRefresh()
    report.observe([], cycle_failed=False, category=None)
    assert report.diagnostics() is None
    failures = [
        (f"/heatingCircuits/hc{index}/operationMode", 500, "request_failed")
        for index in range(1, 4)
    ] + [(f"/system/path{index}", None, "timeout") for index in range(15)]
    report.observe(failures, cycle_failed=True, category="timeout")
    result = report.diagnostics()
    assert result is not None
    assert result["failed_readings"] == 18
    assert len(result["paths"]) == 10
    assert result["paths_omitted"] == 6
    assert result["paths"][0] == {
        "path": "/heatingCircuits/{hc}/operationMode",
        "status": 500,
        "category": "request_failed",
        "count": 3,
    }
    # A refresh without failures keeps the last report.
    report.observe([], cycle_failed=False, category=None)
    assert report.diagnostics() == result


async def test_held_snapshot_and_cloud_usable_follow_the_rules(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    with _plant(hass, clock) as (coordinator, cloud):
        await _cycle(coordinator, clock)
        assert cloud_usable(coordinator)
        fresh = coordinator.data[NUMBER]
        assert held_snapshot(coordinator, NUMBER) is fresh
        assert held_snapshot(coordinator, "/no/such/path") is None
        cloud.failing = {NUMBER: 500}
        cloud.get_status = 500
        await _cycle(coordinator, clock)
        stored = coordinator.data[NUMBER]
        assert not stored.available
        shown = held_snapshot(coordinator, NUMBER)
        assert shown is not None and shown.available
        assert shown.freshness is Freshness.FRESH
        assert shown.resource == fresh.resource
        # Without a hold (no such coordinator feature) the cycle decides.
        plain = Mock(spec=["last_update_success"])
        plain.last_update_success = False
        assert not cloud_usable(plain)
        plain.last_update_success = True
        assert cloud_usable(plain)
        await coordinator.async_shutdown()
