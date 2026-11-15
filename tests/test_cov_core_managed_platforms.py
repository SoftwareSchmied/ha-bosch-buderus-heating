"""Managed platforms: what a source may hand to the entity adder."""

from __future__ import annotations

import pytest
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import Platform

from custom_components.bosch_buderus_heating.configuration import ConfigurationError
from tests.test_connection_runtime import settle
from tests.test_managed_platforms import (  # noqa: F401
    HEAT,
    NUMBER,
    Backend,
    available,
    capture_adds,
    eventually,
    heat_add,
    loaded_integration,
    registered,
    running,
)

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")


class _Probe(SensorEntity):
    _attr_should_poll = False
    _attr_native_value = 1

    def __init__(self, unique_id):
        self._attr_unique_id = unique_id


async def _started(hass):
    captured, capturing = capture_adds()
    manager = running(hass, Backend())
    context = await manager.__aenter__()
    owner = context[2]
    with capturing:
        await owner.start_platforms()
    await eventually(lambda: available(owner, "number", NUMBER))
    await settle(hass)
    return manager, owner, heat_add(captured, Platform.SENSOR)


async def test_a_source_cannot_ask_for_an_update_before_adding(hass) -> None:
    manager, _owner, add = await _started(hass)
    try:
        with pytest.raises(ValueError, match="current data"):
            add([_Probe(HEAT + ":probe_a")], True)
        with pytest.raises(ValueError, match="current data"):
            add([_Probe(HEAT + ":probe_a")], config_subentry_id="sub")
        await settle(hass)
        assert registered(hass, "sensor", HEAT + ":probe_a") is None
    finally:
        await manager.__aexit__(None, None, None)


async def test_an_entity_of_another_system_is_refused_without_registering_it(
    hass,
) -> None:
    manager, _owner, add = await _started(hass)
    try:
        with pytest.raises(ConfigurationError) as error:
            add([_Probe("another-gateway:probe_b")])
        assert str(error.value) == "entity_source_identity_conflict"
        # An entity without any identity is refused the same way.
        with pytest.raises(ConfigurationError):
            add([_Probe(None)])
        await settle(hass)
        assert registered(hass, "sensor", "another-gateway:probe_b") is None
    finally:
        await manager.__aexit__(None, None, None)


async def test_an_entity_offered_twice_is_added_once(hass) -> None:
    manager, _owner, add = await _started(hass)
    try:
        unique_id = HEAT + ":probe_c"
        add([_Probe(unique_id)])
        add([_Probe(unique_id)])
        await eventually(lambda: registered(hass, "sensor", unique_id))
        await settle(hass)
        entries = [
            item
            for item in hass.states.async_all("sensor")
            if item.entity_id == registered(hass, "sensor", unique_id).entity_id
        ]
        assert len(entries) == 1
    finally:
        await manager.__aexit__(None, None, None)
