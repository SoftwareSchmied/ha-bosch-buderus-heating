"""Zone climate: the lower limit of a temporary setpoint, entries refused
by the entity itself, and what an unavailable zone shows."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.bosch_buderus_heating.coordinator import Freshness
from custom_components.bosch_buderus_heating.pointt import Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.rrc_climate import zone_reading
from tests.test_rrc_zone_climate import (  # noqa: F401 - fixtures and helpers
    MANUAL,
    MODE,
    OVERRIDE,
    climate,
    legacy_setup_path,
    path,
    refresh,
    setup,
    teardown,
    zone_cloud,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def entity(hass: HomeAssistant, zone: int):
    return hass.data["entity_components"]["climate"].get_entity(climate(hass, zone))


def wire(leaf_path: str, **fields) -> Resource:
    data = {
        "id": leaf_path,
        "type": "floatValue",
        "writeable": 1,
        "recordable": 0,
        "unitOfMeasure": "C",
        "stepSize": 0.5,
        "value": 21.0,
        **fields,
    }
    return parse_resource(
        {key: value for key, value in data.items() if value is not None}
    )


# ------------------------------------------------------- reading a leaf
def snapshot(resource: Resource, *, available=True, freshness=Freshness.FRESH):
    return SimpleNamespace(resource=resource, available=available, freshness=freshness)


def reading_coordinator(**attributes):
    defaults = {
        "last_update_success": True,
        "data": {},
        "display_snapshot": lambda path: None,
    }
    return SimpleNamespace(**{**defaults, **attributes})


def test_a_stored_assumption_that_is_no_reading_counts_as_none():
    coordinator = reading_coordinator(assumed_fresh=lambda path: "pending")
    assert zone_reading(coordinator, "/zones/zn1/userMode") is None  # type: ignore[arg-type]
    stored = wire("/zones/zn1/userMode", type="stringValue", value="clock")
    coordinator = reading_coordinator(assumed_fresh=lambda path: stored)
    assert zone_reading(coordinator, stored.path) is stored  # type: ignore[arg-type]


def test_a_change_needs_a_reading_of_a_successful_cycle():
    leaf = wire("/zones/zn1/temperatureActual")
    coordinator = reading_coordinator(
        last_update_success=False, data={leaf.path: snapshot(leaf)}
    )
    assert zone_reading(coordinator, leaf.path, strict=True) is None  # type: ignore[arg-type]
    coordinator.last_update_success = True
    assert zone_reading(coordinator, leaf.path, strict=True) is leaf  # type: ignore[arg-type]
    # A stale or foreign reading is never taken for a change.
    coordinator.data = {leaf.path: snapshot(leaf, freshness=Freshness.STALE)}
    assert zone_reading(coordinator, leaf.path, strict=True) is None  # type: ignore[arg-type]
    coordinator.data = {leaf.path: snapshot(wire("/zones/zn2/temperatureActual"))}
    assert zone_reading(coordinator, leaf.path, strict=True) is None  # type: ignore[arg-type]


# ---------------------------------------------- the temporary setpoint
async def test_without_a_manual_minimum_a_zero_limit_offers_no_override(
    hass: HomeAssistant,
) -> None:
    """The override's lowest value is the manual setpoint's minimum (0 clears
    it); with no minimum to count from, no target is offered."""
    cloud = zone_cloud()
    manual = path(2, MANUAL)
    cloud.tree[manual] = wire(manual, minValue=None, maxValue=30.0)
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        zone = entity(hass, 2)
        assert zone._target() is None
        with pytest.raises(ServiceValidationError):
            await zone.async_set_temperature(temperature=22.0)
        assert not cloud.writes
    finally:
        await teardown(hass, entry, stack)


async def test_without_a_manual_minimum_the_override_limit_is_its_own(
    hass: HomeAssistant,
) -> None:
    cloud = zone_cloud()
    manual, override = path(2, MANUAL), path(2, OVERRIDE)
    cloud.tree[manual] = wire(manual, minValue=None, maxValue=30.0)
    cloud.tree[override] = wire(override, minValue=10.0, maxValue=30.0, value=23.5)
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        target = entity(hass, 2)._target()
        assert target is not None
        assert (target[2], target[3]) == (10.0, 30.0)
    finally:
        await teardown(hass, entry, stack)


async def test_limits_that_cross_offer_no_target(hass: HomeAssistant) -> None:
    cloud = zone_cloud()
    manual = path(2, MANUAL)
    # The manual minimum is the override's floor: above its own maximum (30).
    cloud.tree[manual] = wire(manual, minValue=35.0, maxValue=40.0)
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        zone = entity(hass, 2)
        assert zone._target() is None
        with pytest.raises(ServiceValidationError):
            await zone.async_set_temperature(temperature=22.0)
        assert not cloud.writes
    finally:
        await teardown(hass, entry, stack)


async def test_entries_outside_the_limits_are_refused_by_the_entity(
    hass: HomeAssistant,
) -> None:
    """Home Assistant checks the range before the entity; a direct call must
    not rely on it, and 0 never clears a setpoint through the climate."""
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        zone = entity(hass, 2)
        for value in (4.5, 0.0, 31.0):
            with pytest.raises(ServiceValidationError):
                await zone.async_set_temperature(temperature=value)
        assert not cloud.writes
    finally:
        await teardown(hass, entry, stack)


async def test_an_unsupported_mode_is_refused_by_the_entity(
    hass: HomeAssistant,
) -> None:
    from homeassistant.components.climate import HVACMode

    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        zone = entity(hass, 1)
        with pytest.raises(ServiceValidationError):
            await zone.async_set_hvac_mode(HVACMode.COOL)
        with pytest.raises(ServiceValidationError):
            await zone.async_set_temperature(temperature=21.0, hvac_mode="cool")
        assert not cloud.writes
    finally:
        await teardown(hass, entry, stack)


async def test_the_mode_already_set_is_not_written_again(
    hass: HomeAssistant,
) -> None:
    cloud = zone_cloud()
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        zone = entity(hass, 1)  # manual: heat
        await zone.async_set_temperature(temperature=22.0, hvac_mode="heat")
        assert cloud.written("PUT") == [("PUT", path(1, MANUAL), 22.0)]
    finally:
        await teardown(hass, entry, stack)


# ------------------------------------------------- an unavailable zone
async def test_a_zone_with_an_unknown_mode_shows_nothing_and_offers_nothing(
    hass: HomeAssistant,
) -> None:
    cloud = zone_cloud()
    entry, coordinator, _sleeps, stack, clock = await setup(hass, cloud)
    try:
        mode = path(1, MODE)
        cloud.tree[mode] = replace(cloud.tree[mode], value="eco")  # no mode
        await refresh(hass, coordinator, clock, 301)
        zone = entity(hass, 1)
        assert not zone.available
        assert zone.hvac_action is None
        assert zone.native_target_temperature is None
        assert zone.native_current_temperature is None
        assert zone.extra_state_attributes == {}
        assert zone.hvac_modes == []
    finally:
        await teardown(hass, entry, stack)


async def test_a_mode_the_controller_does_not_list_is_still_shown(
    hass: HomeAssistant,
) -> None:
    """A locked or unusual current mode keeps itself among the modes shown;
    writing is refused elsewhere."""
    cloud = zone_cloud()
    mode_path = path(1, MODE)
    cloud.tree[mode_path] = parse_resource(
        {
            "id": mode_path,
            "type": "stringValue",
            "writeable": 1,
            "recordable": 0,
            "value": "manual",
            "allowedValues": ["clock"],
        }
    )
    entry, _coordinator, _sleeps, stack, _clock = await setup(hass, cloud)
    try:
        zone = entity(hass, 1)
        modes = {mode.value for mode in zone.hvac_modes}
        assert modes == {"auto", "heat"}
    finally:
        await teardown(hass, entry, stack)
