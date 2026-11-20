"""HTTP 200 with a null value is no new reading (a K40 answered the hot water
operation mode with null for 88 minutes): the last good value stays."""

from __future__ import annotations

from dataclasses import replace

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.null_reading import (
    holds_value,
    is_null_reading,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from tests.helpers.rrc_cloud import Clock, RrcCloud
from tests.test_rrc_dhw import (
    DHW1,
    PACKAGE,
    MODE,
    _entity_id,
    _setup,
    _teardown,
    _uid,
    tree,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def legacy_setup_path():
    with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
        yield


def _scalar(resource_type: str, value: object) -> Resource:
    return Resource(
        "/x", value=value, has_value=True, metadata=ResourceMetadata(resource_type=resource_type)
    )


def test_only_a_scalar_with_a_null_value_is_a_null_reading() -> None:
    for resource_type in ("stringValue", "floatValue", "integerValue"):
        assert is_null_reading(_scalar(resource_type, None))
        assert not is_null_reading(_scalar(resource_type, 1))
    assert not is_null_reading(_scalar("refEnum", None))
    assert not is_null_reading(Resource("/x", metadata=ResourceMetadata("stringValue")))
    assert holds_value(_scalar("stringValue", "off"))
    assert not holds_value(_scalar("stringValue", None))


async def test_a_null_mode_keeps_the_last_value_in_real_cycles(
    hass: HomeAssistant,
) -> None:
    cloud, clock_ = RrcCloud(tree()), Clock()
    entry, coordinator, _sleeps, stack = await _setup(hass, cloud, clock_)
    heater = _entity_id(hass, "water_heater", _uid(DHW1, "water_heater"))
    try:
        before = hass.states.get(heater).state
        assert before != STATE_UNAVAILABLE
        good = cloud.tree[MODE]
        cloud.tree[MODE] = replace(good, value=None)
        for _ in range(3):
            clock_.now += 1200
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            # The stored reading is the last good one, the entity keeps it.
            assert coordinator.data[MODE].resource.value == good.value
            assert hass.states.get(heater).state == before
        # The cloud answers again: the new value is taken.
        cloud.tree[MODE] = replace(good, value="off")
        clock_.now += 1200
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data[MODE].resource.value == "off"
        assert coordinator.data[MODE].available
        assert hass.states.get(heater).state == "off"
        # A path that is always null keeps today's behaviour (no last value).
    finally:
        await _teardown(hass, entry, stack)
