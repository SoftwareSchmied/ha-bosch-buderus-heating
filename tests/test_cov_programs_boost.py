"""Boost presets: what the number entities refuse, and a start whose limits
disappeared."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.bosch_buderus_heating import rrc_boost
from tests.helpers.rrc_absence import (
    BOOST,
    absence_tree,
    entity_id,
    set_value,
    setup,
    teardown,
    variants,
)
from tests.helpers.rrc_cloud import RrcCloud
from tests.test_rrc_boost import (  # noqa: F401 - fixtures and helpers
    DURATION,
    SWITCH,
    TEMPERATURE,
    _number,
    _struct,
    _turn,
    legacy_setup_path,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def refresh(hass, coordinator, clock):
    clock.now += 3600
    await coordinator.async_refresh()
    await hass.async_block_till_done()


def number_entity(hass: HomeAssistant, unique_id: str):
    return hass.data["entity_components"]["number"].get_entity(
        entity_id(hass, "number", unique_id)
    )


@pytest.mark.parametrize(
    ("variant", "key"),
    [
        ("boost_extra_field", "rrc_boost_unsupported_shape"),
        ("boost_on", "rrc_boost_running"),
    ],
)
async def test_a_preset_is_not_kept_for_a_structure_it_may_not_change(
    hass: HomeAssistant, variant, key
) -> None:
    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    try:
        set_value(cloud, BOOST, variants()[variant])
        await refresh(hass, coordinator, clock)
        with pytest.raises(ServiceValidationError) as refused:
            await _number(hass, TEMPERATURE, 22.0)
        assert refused.value.translation_key == key
        assert cloud.written() == []
    finally:
        await teardown(hass, entry, stack)


async def test_a_preset_is_not_kept_while_the_boost_is_locked(
    hass: HomeAssistant,
) -> None:
    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    try:
        cloud.tree[BOOST] = _struct(
            absence_tree()[BOOST].value, used="true", available="false"
        )
        await refresh(hass, coordinator, clock)
        with pytest.raises(ServiceValidationError) as locked:
            await _number(hass, DURATION, 3.0)
        assert locked.value.translation_key == "rrc_boost_locked"
        assert cloud.written() == []
    finally:
        await teardown(hass, entry, stack)


async def test_an_unreadable_boost_shows_no_value_and_keeps_no_preset(
    hass: HomeAssistant,
) -> None:
    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    try:
        set_value(cloud, BOOST, [{"mode": "off"}])
        await refresh(hass, coordinator, clock)
        entity = number_entity(hass, TEMPERATURE)
        # An unavailable entity is skipped by the service; a direct call must
        # still refuse.
        assert not entity.available and entity.native_value is None
        with pytest.raises(ServiceValidationError) as refused:
            await entity.async_set_native_value(22.0)
        assert refused.value.translation_key == "write_validation_failed"
        assert cloud.written() == []
    finally:
        await teardown(hass, entry, stack)


async def test_a_start_whose_limits_disappeared_sends_nothing(
    hass: HomeAssistant,
) -> None:
    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    try:
        await _number(hass, TEMPERATURE, 24.0)
        # The leaf with the limits is gone: the kept preset cannot be checked.
        del cloud.tree[rrc_boost.BOOST_TEMPERATURE_PATH]
        await refresh(hass, coordinator, clock)
        with pytest.raises(ServiceValidationError) as refused:
            await _turn(hass, True)
        assert refused.value.translation_key == "write_validation_failed"
        assert cloud.written() == []
        assert hass.states.get(entity_id(hass, "switch", SWITCH)).state == "off"
    finally:
        await teardown(hass, entry, stack)


async def test_a_preset_is_not_kept_for_a_boost_that_cannot_be_written(
    hass: HomeAssistant,
) -> None:
    cloud = RrcCloud(absence_tree())
    entry, coordinator, clock, _sleeps, stack = await setup(hass, cloud)
    try:
        cloud.tree[BOOST] = _struct(absence_tree()[BOOST].value, writeable=0)
        await refresh(hass, coordinator, clock)
        entity = number_entity(hass, TEMPERATURE)
        assert not entity.available
        with pytest.raises(ServiceValidationError) as refused:
            await entity.async_set_native_value(22.0)
        assert refused.value.translation_key == "write_validation_failed"
        assert cloud.written() == []
    finally:
        await teardown(hass, entry, stack)
