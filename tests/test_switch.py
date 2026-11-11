"""Tests for safe string-backed switches."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    Freshness,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.switch import (
    BoschBuderusSwitch,
    async_setup_entry,
    build_switch_descriptions,
)
from tests.ha_test_helpers import MockConfigEntry

PATH = "/dhwCircuits/dhw1/charge"


def _resource(
    path: str = PATH,
    value: str = "stop",
    allowed_values: tuple[str, ...] = ("start", "stop"),
    *,
    writable: bool = True,
) -> Resource:
    return Resource(
        path=path,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue",
            allowed_values=allowed_values,
            writable=writable,
        ),
    )


def _switch(hass: HomeAssistant) -> BoschBuderusSwitch:
    resource = _resource()
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type="K40"), entry
    )
    coordinator.resources = {PATH: resource}
    coordinator.data = {PATH: ResourceSnapshot(resource, True, datetime.now(UTC))}
    coordinator.last_update_success = True
    return BoschBuderusSwitch(
        coordinator, build_switch_descriptions(coordinator.resources)[0]
    )


def test_all_released_switches_are_discovered() -> None:
    resources = {
        item.path: item
        for item in (
            _resource(),
            _resource(
                "/dhwCircuits/dhw1/reduceTempOnAlarm",
                "off",
                ("off", "on"),
            ),
            _resource("/system/awayMode/enabled", "off", ("off", "on")),
        )
    }

    descriptions = build_switch_descriptions(resources)

    assert len(descriptions) == 3
    assert {item.translation_key for item in descriptions} == {
        "extra_hot_water",
        "reduce_temperature_on_alarm",
        "away_mode",
    }


def test_switch_state_identity_and_device(hass: HomeAssistant) -> None:
    entity = _switch(hass)

    assert entity.available
    assert not entity.is_on
    assert entity.entity_description.entity_registry_enabled_default
    assert entity.name == "DHW 1: Extra hot water"
    assert entity.unique_id == "gateway-one:dhwCircuits:dhw1:charge:control"
    assert entity.device_info["identifiers"] == {(DOMAIN, "gateway-one")}


def test_unreleased_or_incomplete_switch_is_not_exposed() -> None:
    invalid = (
        _resource(path="/gateway/tosAccepted"),
        _resource(writable=False),
        _resource(allowed_values=("start",)),
    )

    assert all(build_switch_descriptions({item.path: item}) == () for item in invalid)


def test_switch_becomes_unavailable_when_stale(hass: HomeAssistant) -> None:
    entity = _switch(hass)
    current = entity.coordinator.data[PATH]
    entity.coordinator.data[PATH] = ResourceSnapshot(
        current.resource,
        True,
        current.last_success,
        freshness=Freshness.STALE,
    )

    assert not entity.available


async def test_switch_calls_confirmed_coordinator_write(hass: HomeAssistant) -> None:
    entity = _switch(hass)
    writer = AsyncMock()
    entity.coordinator.async_write_control = writer

    await entity.async_turn_on()
    await entity.async_turn_off()

    assert [call.args[:2] for call in writer.await_args_list] == [
        (PATH, "start"),
        (PATH, "stop"),
    ]
    assert all(
        call.args[2] is entity.entity_description.write_policy
        for call in writer.await_args_list
    )


async def test_platform_adds_switches(hass: HomeAssistant) -> None:
    entity = _switch(hass)
    entry = SimpleNamespace(
        async_on_unload=Mock(),
        runtime_data=SimpleNamespace(bacon=None, coordinators=(entity.coordinator,)),
    )
    added: list[BoschBuderusSwitch] = []

    await async_setup_entry(hass, entry, added.extend)
    # Every unload callback, as a config entry runs them: removing the
    # listeners also stops the coordinator's refresh timer.
    for call in entry.async_on_unload.call_args_list:
        call.args[0]()

    assert len(added) == 1
    assert entity.coordinator._unsub_refresh is None
