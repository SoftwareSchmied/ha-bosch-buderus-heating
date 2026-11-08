"""P17: readings stay visible, and it is clear when and why operation is not.

Failures run through real coordinator cycles (``async_refresh``) with the PointT
client replaced; nothing sets the coordinator's end state by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.binary_sensor import (
    BoschBuderusCloudControlBinarySensor,
)
from custom_components.bosch_buderus_heating.connection_runtime import ConnectionPhase
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.managed_pointt import (
    ManagedPointTCoordinator,
)
from custom_components.bosch_buderus_heating.pointt import (
    RateLimited,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from tests.test_local_api import read, scalar
from tests.test_local_selection import TARGET, FakeRuntime
from tests.test_pointt_water_heater import ACTUAL, setup


async def cycle(coordinator) -> None:
    coordinator._next_update = {PollGroup.FAST: 0}
    await coordinator.async_refresh()


def local_actual(value: float):
    return read(
        scalar(ACTUAL, value=value),
        observed_at=datetime.now(UTC),
        observed_monotonic=10.0,
    )


def control(coordinator) -> BoschBuderusCloudControlBinarySensor:
    entity = BoschBuderusCloudControlBinarySensor(coordinator)
    entity.hass = coordinator.hass
    return entity


# ------------------------------------------------ 1A: water heater reading


def test_water_heater_shows_the_local_actual_temperature_only(
    hass: HomeAssistant,
) -> None:
    _, coordinator, heater, _ = setup(hass)
    assert heater.native_current_temperature == 48.5  # cloud without local values
    coordinator.local_selection = LocalSelection(
        FakeRuntime([local_actual(49.5)]), TARGET
    )
    assert heater.native_current_temperature == 49.5
    # Mode and target stay the cloud values.
    assert heater.native_target_temperature == 52
    assert heater.current_operation == "ownprogram"
    coordinator.local_selection = LocalSelection(
        FakeRuntime([local_actual(49.5)], enabled=False), TARGET
    )
    assert heater.native_current_temperature == 48.5


# --------------------------------------- 2A + 3B: status entity and dialog


async def test_normal_operation_reports_control_available(hass: HomeAssistant) -> None:
    _, coordinator, heater, _ = setup(hass)
    await cycle(coordinator)
    entity = control(coordinator)
    assert coordinator.control_status() == (None, None)
    assert entity.available and entity.is_on
    assert entity.extra_state_attributes == {"reason": None, "until": None}
    assert entity.entity_registry_enabled_default is False
    assert entity.unique_id == "gateway:cloud_control"
    assert heater.available


async def test_cloud_outage_disables_controls_and_says_why(hass: HomeAssistant) -> None:
    _, coordinator, heater, _ = setup(hass)
    coordinator.local_selection = LocalSelection(
        FakeRuntime([local_actual(49.5)]), TARGET
    )
    coordinator.client.get_resources_bulk.side_effect = ServiceUnavailable(503)
    await cycle(coordinator)
    entity = control(coordinator)
    assert coordinator.control_status() == ("cloud_unreachable", None)
    assert entity.available and not entity.is_on
    assert entity.extra_state_attributes["reason"] == "cloud_unreachable"
    # 2A: the water heater is not shown as operable, the reading stays on sensors.
    assert not heater.available
    assert coordinator.display_snapshot(ACTUAL).resource.value == 49.5


async def test_rate_limit_pause_names_its_end(hass: HomeAssistant) -> None:
    _, coordinator, _, _ = setup(hass)
    coordinator.client.get_resources_bulk.side_effect = RateLimited(120)
    await cycle(coordinator)
    reason, until = coordinator.control_status()
    assert reason == "rate_limited"
    assert until is not None
    # Rounded up to the next full minute for a stable attribute.
    assert 100 < (until - datetime.now(UTC)).total_seconds() <= 181
    assert control(coordinator).extra_state_attributes["until"] == until.isoformat(
        timespec="seconds"
    )


async def test_repeated_gateway_failures_open_the_protection_pause(
    hass: HomeAssistant,
) -> None:
    _, coordinator, _, _ = setup(hass)
    coordinator.client.get_resources_bulk.side_effect = ServiceUnavailable(503)
    for _ in range(3):
        await cycle(coordinator)
    reason, until = coordinator.control_status()
    assert reason == "protection_pause"
    assert until is not None


@pytest.mark.parametrize(
    ("phase", "current", "expected"),
    [
        (ConnectionPhase.AUTH_REQUIRED, False, "auth_required"),
        (ConnectionPhase.READY, False, "cloud_stopped"),
        (ConnectionPhase.READY, True, None),
    ],
)
def test_managed_source_reports_sign_in_and_stop(phase, current, expected) -> None:
    runtime = SimpleNamespace(connection_phase=lambda _connection: phase)
    fake = SimpleNamespace(
        source=SimpleNamespace(
            connection=SimpleNamespace(runtime=runtime, connection_id="cloud"),
            current=lambda: current,
        )
    )
    auth = ManagedPointTCoordinator._cloud_auth_required(fake)
    stopped = ManagedPointTCoordinator._cloud_source_stopped(fake)
    reason = "auth_required" if auth else "cloud_stopped" if stopped else None
    assert reason == expected


async def test_dialog_explains_missing_controls(hass: HomeAssistant) -> None:
    from tests.test_heating_controls import flow

    options, coordinator, _, result = await flow(hass)
    assert result["type"] == "form"  # controls available
    coordinator.client.get_resources_bulk.side_effect = RateLimited(300)
    coordinator._paths_by_group = {PollGroup.FAST: tuple(coordinator.data)}
    await cycle(coordinator)
    result = await options.async_step_operate()
    assert result["type"] == "abort"
    assert result["reason"] == "controls_rate_limited"
    assert result["description_placeholders"]["until"].count(":") == 1


async def test_dialog_explains_a_pause_while_values_are_current(
    hass: HomeAssistant,
) -> None:
    """A write that hits a rate limit pauses the cloud; other values stay current."""
    from homeassistant.exceptions import HomeAssistantError

    from tests.test_heating_controls import flow

    options, coordinator, climate, _ = await flow(hass)
    coordinator._write_service.async_write_number.side_effect = RateLimited(300)
    with pytest.raises((RateLimited, HomeAssistantError)):
        await climate.async_set_temperature(temperature=20.5)
    from custom_components.bosch_buderus_heating.controls_flow import controls

    assert controls(coordinator)  # other settings are still current
    result = await options.async_step_operate()
    assert result["reason"] == "controls_rate_limited"


async def test_dialog_without_any_setting_says_so(hass: HomeAssistant) -> None:
    from tests.test_heating_controls import flow

    options, coordinator, _, _ = await flow(hass)
    coordinator.data = {}
    result = await options.async_step_operate()
    assert result["reason"] == "controls_not_current"


# ------------------------------------------- 4A: manual vs current setpoint


async def test_local_current_setpoint_never_feeds_climate_or_manual_target(
    hass: HomeAssistant,
) -> None:
    from tests.test_heating_controls import ROOT
    from tests.test_heating_controls import TARGET as MANUAL
    from tests.test_heating_controls import setup as climate_setup

    _entry, coordinator, climate = climate_setup(hass, mode="manual")
    before = climate.native_target_temperature
    assert before == 19  # the stored manual setpoint
    coordinator.local_selection = LocalSelection(
        FakeRuntime(
            [
                read(
                    scalar(ROOT + "/currentRoomSetpoint", value=24.0),
                    observed_at=datetime.now(UTC),
                    observed_monotonic=10.0,
                )
            ]
        ),
        TARGET,
    )
    # The sensor of the effective setpoint may show the local value ...
    assert (
        coordinator.display_snapshot(ROOT + "/currentRoomSetpoint").resource.value
        == 24.0
    )
    # ... but neither the climate target nor the stored manual setting change.
    assert climate.native_target_temperature == before
    assert coordinator.display_snapshot(MANUAL).resource.value == 19


# ------------------------------------------------------- review follow-ups


async def test_account_wide_pause_is_reported_with_a_stable_end(
    hass: HomeAssistant,
) -> None:
    from custom_components.bosch_buderus_heating.pointt.transport import (
        RateLimitBackoff,
    )

    _, coordinator, _, _ = setup(hass)
    await cycle(coordinator)
    backoff = RateLimitBackoff()
    coordinator.client.backoff = backoff
    assert coordinator.control_status() == (None, None)
    backoff.activate(600)  # e.g. set by another gateway of the same account
    first = coordinator.control_status()
    assert first[0] == "rate_limited"
    assert first[1].second == 0 and first[1].microsecond == 0
    assert coordinator.control_status() == first


async def test_stale_settings_with_fresh_readings_are_not_current(
    hass: HomeAssistant,
) -> None:
    from dataclasses import replace

    from custom_components.bosch_buderus_heating.coordinator import Freshness

    _, coordinator, _, _ = setup(hass)
    await cycle(coordinator)
    for path, snapshot in tuple(coordinator.data.items()):
        if snapshot.resource.metadata.writable:
            coordinator.data[path] = replace(snapshot, freshness=Freshness.STALE)
    assert coordinator.control_status() == ("not_current", None)


async def test_status_entity_belongs_to_the_gateway_device(hass: HomeAssistant) -> None:
    from custom_components.bosch_buderus_heating.const import DOMAIN

    _, coordinator, _, _ = setup(hass)
    assert control(coordinator).device_info["identifiers"] == {(DOMAIN, "gateway")}


async def test_status_entity_re_evaluates_when_a_pause_ends(
    hass: HomeAssistant,
) -> None:
    _, coordinator, _, _ = setup(hass)
    poll = coordinator.client.get_resources_bulk.side_effect
    coordinator.client.get_resources_bulk.side_effect = RateLimited(120)
    await cycle(coordinator)
    entity = control(coordinator)
    try:
        entity._refresh_status()
        assert entity._cancel_end is not None and not entity.is_on
        coordinator._cloud_backoff_until = 0.0  # the pause runs out
        coordinator.client.get_resources_bulk.side_effect = poll
        await cycle(coordinator)
        written = []
        entity.async_write_ha_state = lambda: written.append(entity.is_on)
        # Let the scheduled re-evaluation fire as it would in Home Assistant.
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        from tests.ha_test_helpers import (
            async_fire_time_changed,
        )

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=300))
        await hass.async_block_till_done()
        assert written == [True]
        assert entity._cancel_end is None and entity.is_on
    finally:
        entity._cancel_pause_end()


async def test_dialog_without_dialog_settings_says_so(hass: HomeAssistant) -> None:
    from tests.test_heating_controls import flow

    options, coordinator, _, _ = await flow(hass)
    other = "/gateway/someWritableSetting"
    snapshot = next(iter(coordinator.data.values()))
    from dataclasses import replace

    coordinator.data = {
        other: replace(
            snapshot,
            resource=replace(
                snapshot.resource,
                path=other,
                metadata=replace(snapshot.resource.metadata, writable=True),
            ),
        )
    }
    result = await options.async_step_operate()
    assert result["reason"] == "controls_none"


@pytest.mark.parametrize(
    ("phases", "expected"),
    [
        ((ConnectionPhase.AUTH_REQUIRED,), "controls_auth_required"),
        ((ConnectionPhase.UNAVAILABLE,), "controls_cloud_unreachable"),
        ((ConnectionPhase.CONNECTING,), "controls_not_current"),
        ((ConnectionPhase.STOPPED,), "controls_not_loaded"),
        ((), "controls_not_loaded"),  # local access only
    ],
)
def test_dialog_names_why_no_cloud_appliance_is_loaded(phases, expected) -> None:
    from custom_components.bosch_buderus_heating.controls_flow import (
        _missing_cloud_reason,
    )
    from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry

    keys = [f"cloud{index}" for index in range(len(phases))]
    connections = {key: SimpleNamespace(kind="singlekey_cloud") for key in keys}
    connections["local"] = SimpleNamespace(kind="k40_local")
    lookup = dict(zip(keys, phases, strict=True))
    bindings = {"lan": SimpleNamespace(connection_id="local")}
    runtime = ManagedEntry.__new__(ManagedEntry)
    runtime.connections = SimpleNamespace(
        snapshot=SimpleNamespace(connections=connections, bindings=bindings),
        connection_phase=lambda key: lookup.get(key, ConnectionPhase.READY),
        source_phase=lambda key: ConnectionPhase.READY,
    )
    assert _missing_cloud_reason(SimpleNamespace(runtime_data=runtime)) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (ConnectionPhase.UNAVAILABLE, "controls_cloud_unreachable"),
        (ConnectionPhase.READY, "controls_not_current"),  # still discovering
        (ConnectionPhase.DISABLED, "controls_not_loaded"),  # switched off
    ],
)
def test_dialog_uses_the_appliance_state_of_a_ready_access(source, expected) -> None:
    from custom_components.bosch_buderus_heating.controls_flow import (
        _missing_cloud_reason,
    )
    from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry

    runtime = ManagedEntry.__new__(ManagedEntry)
    runtime.connections = SimpleNamespace(
        snapshot=SimpleNamespace(
            connections={"cloud": SimpleNamespace(kind="singlekey_cloud")},
            bindings={"gw": SimpleNamespace(connection_id="cloud")},
        ),
        connection_phase=lambda _key: ConnectionPhase.READY,
        source_phase=lambda _key: source,
    )
    assert _missing_cloud_reason(SimpleNamespace(runtime_data=runtime)) == expected


def test_pause_end_text_has_a_date_when_not_today() -> None:
    from datetime import timedelta

    from custom_components.bosch_buderus_heating.controls_flow import _until_text

    assert _until_text(None) == ""
    assert _until_text(datetime.now(UTC) + timedelta(minutes=5)).count(":") == 1
    assert "-" in _until_text(datetime.now(UTC) + timedelta(days=1, hours=1))


async def test_options_dialog_reports_a_required_sign_in(hass: HomeAssistant) -> None:
    """A cloud access that needs a sign-in has no running appliance to list."""
    from custom_components.bosch_buderus_heating.config_flow import (
        BoschBuderusOptionsFlow,
    )
    from custom_components.bosch_buderus_heating.const import DOMAIN
    from custom_components.bosch_buderus_heating.managed_entry import ManagedEntry
    from tests.ha_test_helpers import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    runtime = ManagedEntry.__new__(ManagedEntry)
    runtime.connections = SimpleNamespace(
        snapshot=SimpleNamespace(
            bindings={"binding": SimpleNamespace(connection_id="cloud")},
            connections={"cloud": SimpleNamespace(kind="singlekey_cloud")},
        ),
        source_data=lambda _key: None,  # the source stopped for the sign-in
        connection_phase=lambda _key: ConnectionPhase.AUTH_REQUIRED,
        source_phase=lambda _key: ConnectionPhase.AUTH_REQUIRED,
    )
    entry.runtime_data = runtime
    options = BoschBuderusOptionsFlow(entry)
    options.hass = hass
    result = await options.async_step_controls()
    assert result["reason"] == "controls_auth_required"


async def test_reason_precedence_on_a_real_coordinator(hass: HomeAssistant) -> None:
    """Sign-in before stop before pauses before reachability."""
    _, coordinator, _, _ = setup(hass)
    coordinator.client.get_resources_bulk.side_effect = RateLimited(120)
    await cycle(coordinator)
    assert coordinator.control_status()[0] == "rate_limited"
    coordinator._cloud_source_stopped = lambda: True
    assert coordinator.control_status() == ("cloud_stopped", None)
    coordinator._cloud_auth_required = lambda: True
    assert coordinator.control_status() == ("auth_required", None)


async def test_water_heater_keeps_a_valid_value_like_the_sensor(
    hass: HomeAssistant,
) -> None:
    """A transient miss of one reading keeps it within its validity (P12)."""
    from dataclasses import replace

    _, coordinator, heater, _ = setup(hass)
    coordinator.data[ACTUAL] = replace(
        coordinator.data[ACTUAL], available=False, last_error_category="http_503"
    )
    assert coordinator.last_update_success
    assert heater.native_current_temperature == 48.5
    coordinator.data[ACTUAL] = replace(coordinator.data[ACTUAL], ended=True)
    assert heater.native_current_temperature is None  # an explicit failure ends it


def test_water_heater_rejects_a_local_error_value(hass: HomeAssistant) -> None:
    from dataclasses import replace

    _, coordinator, heater, _ = setup(hass)
    snapshot = coordinator.data[ACTUAL]
    coordinator.data[ACTUAL] = replace(
        snapshot,
        resource=replace(
            snapshot.resource,
            metadata=replace(
                snapshot.resource.metadata, numeric_error_values=(-3276.8,)
            ),
        ),
    )
    coordinator.local_selection = LocalSelection(
        FakeRuntime([local_actual(-3276.8)]), TARGET
    )
    assert heater.native_current_temperature != -3276.8
