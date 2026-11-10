"""W1 (CT-C04, CT-C07): the temporary setpoint of an RRC zone.

Set through the zone climate in the mode ``clock``, cleared with 0.0 by its
button, confirmed only by a reading (1, 2, 4 and 8 seconds), never sent
twice. Real setups and cycles against the fake cloud; every behaviour here
is expected, untested on hardware.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.bosch_buderus_heating import rrc_contract_zone
from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
    RequestTimeout,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    ContractRejected,
    assess_control,
)
from tests.test_rrc_zone_climate import (
    MANUAL,
    MODE,
    OVERRIDE,
    climate,
    entity_id,
    legacy_setup_path,  # noqa: F401  (autouse fixture)
    path,
    refresh,
    set_temperature,
    setup,
    teardown,
    zone_cloud,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
POLICY = rrc_contract_zone.OVERRIDE_POLICY


def clear_button(hass: HomeAssistant, zone: int) -> str:
    return entity_id(
        hass,
        "button",
        f"gateway-one:zones:zn{zone}:clockOverride:temperatureHeating:clear",
    )


async def press(hass: HomeAssistant, button: str) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )


def stats(coordinator) -> dict:
    return coordinator.rrc_write_stats.snapshot("zone")


async def test_set_and_clear_a_temporary_setpoint(hass: HomeAssistant) -> None:
    cloud = zone_cloud()
    entry, coordinator, sleeps, stack, _clock = await setup(hass, cloud)
    try:
        # Zone 3 follows its program without a temporary setpoint.
        await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert cloud.written("PUT") == [("PUT", path(3, OVERRIDE), 22.0)]
        assert sleeps == [1.0]
        state = hass.states.get(climate(hass, 3))
        assert state.attributes["temperature"] == 22.0
        assert state.attributes["temporary_override"] is True
        assert state.attributes["override_until_next_switch_point"] is True
        assert hass.states.get(clear_button(hass, 3)).state != "unavailable"

        # The button writes 0.0; the read-back must show 0.
        cloud.writes.clear()
        sleeps.clear()
        await press(hass, clear_button(hass, 3))
        assert cloud.written("PUT") == [("PUT", path(3, OVERRIDE), 0.0)]
        state = hass.states.get(climate(hass, 3))
        assert state.attributes["temperature"] == 19.0  # the program's again
        assert state.attributes["temporary_override"] is False
        assert hass.states.get(clear_button(hass, 3)).state == "unavailable"
        assert stats(coordinator)["writes_confirmed"] == 2
    finally:
        await teardown(hass, entry, stack)


async def test_clear_is_refused_without_an_active_setpoint(hass: HomeAssistant) -> None:
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        # Zone 1 runs manually, zone 3 has none: nothing to clear. The
        # button is unavailable, and its own check refuses a direct press.
        component = hass.data["entity_components"]["button"]
        for zone in (1, 3):
            await press(hass, clear_button(hass, zone))
            entity = component.get_entity(clear_button(hass, zone))
            with pytest.raises(ServiceValidationError) as refused:
                await entity.async_press()
            assert refused.value.translation_key == "rrc_override_not_active"
        assert not cloud.writes
    finally:
        await teardown(hass, entry, stack)


async def test_controller_ending_a_replaced_setpoint_at_once(
    hass: HomeAssistant,
) -> None:
    """P24/P33: zone 2 had 23.5; the controller answers 0 (switch point):
    not confirmed with its own message, one PUT, no waiting for nothing."""
    cloud = zone_cloud()
    entry, coordinator, sleeps, stack, _clock = await setup(hass, cloud)
    try:
        cloud.end_on_write.add(path(2, OVERRIDE))
        with pytest.raises(HomeAssistantError) as ended:
            await set_temperature(hass, climate(hass, 2), temperature=22.0)
        assert ended.value.translation_key == "rrc_override_ended"
        assert cloud.calls["PUT", path(2, OVERRIDE)] == 1
        assert sleeps == [1.0]
        assert coordinator.data[path(2, OVERRIDE)].freshness is Freshness.STALE
        assert stats(coordinator)["writes_unconfirmed"] == 1
    finally:
        await teardown(hass, entry, stack)


async def test_new_setpoint_applied_late_is_waited_for(hass: HomeAssistant) -> None:
    """Zone 3 had none, so a first 0 means "not yet": confirmed at the third
    reading after the PUT, never sent twice."""
    cloud = zone_cloud()
    entry, coordinator, sleeps, stack, _clock = await setup(hass, cloud)
    try:
        cloud.adopt_after[path(3, OVERRIDE)] = 3
        await set_temperature(hass, climate(hass, 3), temperature=22.5)
        assert sleeps == [1.0, 2.0, 4.0]
        assert cloud.calls["PUT", path(3, OVERRIDE)] == 1
        assert hass.states.get(climate(hass, 3)).attributes["temperature"] == 22.5
        assert stats(coordinator)["writes_confirmed"] == 1
    finally:
        await teardown(hass, entry, stack)


async def test_unconfirmed_setpoint_is_confirmed_late_by_a_regular_poll(
    hass: HomeAssistant,
) -> None:
    cloud = zone_cloud()
    entry, coordinator, sleeps, stack, clock = await setup(hass, cloud)
    try:
        cloud.adopt_after[path(3, OVERRIDE)] = 5
        with pytest.raises(HomeAssistantError) as pending:
            await set_temperature(hass, climate(hass, 3), temperature=22.5)
        assert pending.value.translation_key == "rrc_write_pending_confirmation"
        assert sleeps == [1.0, 2.0, 4.0, 8.0]
        assert cloud.calls["PUT", path(3, OVERRIDE)] == 1
        await refresh(hass, coordinator, clock, 1200)
        assert hass.states.get(climate(hass, 3)).attributes["temperature"] == 22.5
        assert cloud.calls["PUT", path(3, OVERRIDE)] == 1  # never sent again
        zone = stats(coordinator)
        assert zone["writes_unconfirmed"] == 1
        assert zone["late_confirmed"] == 1
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        report = diagnostics["gateways"][0]["rrc_features"]["contracts"]["zone"]
        assert report["writes"]["late_confirmed"] == 1
        assert "22.5" not in str(report)
    finally:
        await teardown(hass, entry, stack)


@pytest.mark.parametrize("applied", [True, False])
async def test_timeout_after_the_put_is_resolved_by_reading(
    hass: HomeAssistant, applied: bool
) -> None:
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        cloud.timeout_after_write[path(3, OVERRIDE)] = applied
        if applied:
            await set_temperature(hass, climate(hass, 3), temperature=22.0)
            assert hass.states.get(climate(hass, 3)).attributes["temperature"] == 22.0
        else:
            with pytest.raises(HomeAssistantError) as pending:
                await set_temperature(hass, climate(hass, 3), temperature=22.0)
            assert pending.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["PUT", path(3, OVERRIDE)] == 1
    finally:
        await teardown(hass, entry, stack)


async def test_put_without_effect_and_failing_puts(hass: HomeAssistant) -> None:
    """204 without effect, 403, 5xx and 429 on the PUT: never a second PUT."""
    cloud = zone_cloud()
    entry, coordinator, sleeps, stack, clock = await setup(hass, cloud)
    target = path(3, OVERRIDE)
    try:
        cloud.ignore_writes.add(target)
        with pytest.raises(HomeAssistantError) as ignored:
            await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert ignored.value.translation_key == "rrc_write_pending_confirmation"
        assert sleeps == [1.0, 2.0, 4.0, 8.0]
        assert cloud.calls["PUT", target] == 1
        cloud.ignore_writes.clear()
        await refresh(hass, coordinator, clock, 1200)

        cloud.forbidden.add(target)
        with pytest.raises(HomeAssistantError) as forbidden:
            await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert forbidden.value.translation_key == "write_failed"
        assert cloud.calls["PUT", target] == 2
        cloud.forbidden.discard(target)
        await refresh(hass, coordinator, clock, 1200)

        # A server error may follow an applied PUT: resolved by reading only.
        cloud.put_errors[target] = ServiceUnavailable(503)
        with pytest.raises(HomeAssistantError) as failed:
            await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert failed.value.translation_key == "rrc_write_pending_confirmation"
        assert cloud.calls["PUT", target] == 3
        del cloud.put_errors[target]
        await refresh(hass, coordinator, clock, 1200)

        cloud.rate_limit_writes[target] = 120
        with pytest.raises(HomeAssistantError) as limited:
            await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert limited.value.translation_key == "write_rate_limited"
        assert cloud.calls["PUT", target] == 4
        del cloud.rate_limit_writes[target]
        # The account brake stops the next change before anything is sent.
        with pytest.raises(HomeAssistantError) as braked:
            await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert braked.value.translation_key == "write_rate_limited"
        assert cloud.calls["PUT", target] == 4
        zone = stats(coordinator)
        assert zone["writes_sent"] == 4
        assert zone["writes_confirmed"] == 0
        assert set(zone["rejection_reasons"]) <= {"rate_limited", "request_failed"}
    finally:
        await teardown(hass, entry, stack)


async def test_429_on_the_fresh_read_sends_nothing(hass: HomeAssistant) -> None:
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        cloud.rate_limit[path(3, MODE)] = 60
        with pytest.raises(HomeAssistantError) as limited:
            await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert limited.value.translation_key == "write_rate_limited"
        assert not cloud.writes
    finally:
        await teardown(hass, entry, stack)


async def test_contract_rules_refuse_before_sending(hass: HomeAssistant) -> None:
    """Only in the mode clock; 0 or at least the manual minimum (S3, P23)."""
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        with pytest.raises(ContractRejected) as low:
            await coordinator.async_write_control(path(3, OVERRIDE), 3.0, POLICY)
        assert low.value.reason == "rrc_override_below_minimum"
        # Zone 1 runs manually: its temporary setpoint is never written.
        with pytest.raises(ContractRejected) as manual:
            await coordinator.async_write_control(path(1, OVERRIDE), 22.0, POLICY)
        assert manual.value.reason == "rrc_override_needs_clock_mode"
        with pytest.raises(ContractRejected):
            await coordinator.async_write_control(path(1, OVERRIDE), 0.0, POLICY)
        assert not cloud.writes
        rejected = stats(coordinator)["rejection_reasons"]
        assert rejected == {
            "rrc_override_below_minimum": 1,
            "rrc_override_needs_clock_mode": 2,
        }
    finally:
        await teardown(hass, entry, stack)


async def test_setpoint_shown_in_manual_mode_is_not_written(
    hass: HomeAssistant,
) -> None:
    tree = zone_cloud().tree
    tree[path(1, OVERRIDE)] = replace(tree[path(1, OVERRIDE)], value=24.0)
    cloud = zone_cloud(tree)
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        state = hass.states.get(climate(hass, 1))
        assert state.attributes["temporary_override"] is True
        assert state.attributes["temperature"] == 21.0  # the manual one
        assert hass.states.get(clear_button(hass, 1)).state == "unavailable"
        await set_temperature(hass, climate(hass, 1), temperature=22.0)
        assert cloud.written("PUT") == [("PUT", path(1, MANUAL), 22.0)]
    finally:
        await teardown(hass, entry, stack)


@pytest.mark.parametrize(
    "change",
    [
        {"writeable": 0},
        {"minValue": None},
        {"stepSize": 100.0},
        {"unitOfMeasure": "%"},
        {"type": "stringValue"},
    ],
)
async def test_malformed_metadata_offers_no_temporary_setpoint(
    hass: HomeAssistant, change: dict
) -> None:
    """S1, S3, S4, S2: without trustworthy metadata the target in the mode
    clock is shown only, and nothing is written."""
    from custom_components.bosch_buderus_heating.pointt.parsers import (
        parse_resource,
    )

    tree = zone_cloud().tree
    target = path(2, OVERRIDE)
    wire = {
        "id": target,
        "type": "floatValue",
        "writeable": 1,
        "recordable": 0,
        "unitOfMeasure": "C",
        "minValue": 0.0,
        "maxValue": 30.0,
        "stepSize": 0.5,
        "value": 23.5,
    }
    wire.update(change)
    tree[target] = parse_resource({key: v for key, v in wire.items() if v is not None})
    cloud = zone_cloud(tree)
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        state = hass.states.get(climate(hass, 2))
        assert state.state == "auto"
        assert state.attributes["supported_features"] == 0
        with pytest.raises(ServiceValidationError):
            await set_temperature(hass, climate(hass, 2), temperature=22.0)
        assert not cloud.writes
    finally:
        await teardown(hass, entry, stack)


async def test_outage_of_the_setpoint_in_real_cycles(hass: HomeAssistant) -> None:
    """A failing temporary setpoint keeps the climate and drops the target
    command; it returns with the reading."""
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, clock = await setup(hass, cloud)
    target = path(2, OVERRIDE)
    try:
        cloud.down[target] = 404
        await refresh(hass, coordinator, clock, 7200)
        state = hass.states.get(climate(hass, 2))
        assert state.state == "auto"
        assert state.attributes["supported_features"] == 0
        assert state.attributes["temperature"] == 23.5  # the active setpoint
        with pytest.raises(ServiceValidationError):
            await set_temperature(hass, climate(hass, 2), temperature=22.0)
        assert not cloud.writes
        del cloud.down[target]
        await refresh(hass, coordinator, clock, 7200)
        state = hass.states.get(climate(hass, 2))
        assert state.attributes["supported_features"] == 1
        await set_temperature(hass, climate(hass, 2), temperature=22.0)
        assert cloud.written("PUT") == [("PUT", target, 22.0)]
    finally:
        await teardown(hass, entry, stack)


async def test_down_mode_reading_errors_send_nothing(hass: HomeAssistant) -> None:
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        # The mode guard cannot be read: nothing is written (C33).
        cloud.down[path(3, MODE)] = RequestTimeout("timed out")
        with pytest.raises(HomeAssistantError):
            await set_temperature(hass, climate(hass, 3), temperature=22.0)
        cloud.down[path(3, MODE)] = 403
        with pytest.raises(HomeAssistantError):
            await set_temperature(hass, climate(hass, 3), temperature=22.0)
        assert not cloud.writes
    finally:
        await teardown(hass, entry, stack)


def test_the_override_policy_is_only_an_rrc_variant() -> None:
    """S11: the same path at another gateway has no write policy."""
    from custom_components.bosch_buderus_heating.pointt.parsers import (
        parse_resource,
    )

    resource = parse_resource(
        {
            "id": path(1, OVERRIDE),
            "type": "floatValue",
            "writeable": 1,
            "recordable": 0,
            "unitOfMeasure": "C",
            "minValue": 0.0,
            "maxValue": 30.0,
            "stepSize": 0.5,
            "value": 0.0,
        }
    )
    assert assess_control(resource, GENERIC_CONTEXT).policy is None
    assert assess_control(resource, None).policy is None
    assessment = assess_control(resource, RRC_CONTEXT)
    assert assessment.policy is POLICY and assessment.eligible
    assert not POLICY.create_entity


async def test_a_second_target_while_one_is_sent_is_busy(hass: HomeAssistant) -> None:
    """CT-C08: a slider sends fast; a target that cannot start while the
    first one waits for its read-back is reported busy and never sent."""
    import asyncio
    from datetime import timedelta
    from unittest.mock import patch

    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    release = asyncio.Event()

    async def slow(_delay: float) -> None:
        await release.wait()

    coordinator._write_service._sleep = slow
    try:
        with patch(
            "custom_components.bosch_buderus_heating.coordinator.WRITE_LOCK_TIMEOUT",
            timedelta(seconds=0.05),
        ):
            first = hass.async_create_task(
                set_temperature(hass, climate(hass, 3), temperature=22.0)
            )
            await asyncio.sleep(0.01)
            with pytest.raises(HomeAssistantError) as busy:
                await set_temperature(hass, climate(hass, 3), temperature=22.5)
            assert busy.value.translation_key == "write_busy"
            release.set()
            await first
        assert cloud.written("PUT") == [("PUT", path(3, OVERRIDE), 22.0)]
    finally:
        await teardown(hass, entry, stack)
