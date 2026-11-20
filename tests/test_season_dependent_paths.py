"""A leaf that exists only in some season mode is never given up.

A room controller answers 404 for ``suWiThreshold`` while the circuit's
``suWiSwitchMode`` is ``off`` (summer). Neither the five answers of a
season nor a restart in that season may end the read for a day or for good;
the value returns with the regular reading after the mode changes.
"""

from __future__ import annotations

from time import monotonic
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.pointt import (
    BatchItemResult,
    Gateway,
    RequestMetrics,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.resource_catalog import PollGroup
from custom_components.bosch_buderus_heating.season_paths import (
    is_season_dependent,
)
from tests.ha_test_helpers import MockConfigEntry

CIRCUIT = "/heatingCircuits/hc1"
MODE = CIRCUIT + "/suWiSwitchMode"
THRESHOLD = CIRCUIT + "/suWiThreshold"
GATEWAY = "gateway-one"


def _mode(value: str) -> Resource:
    return Resource(
        MODE,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="stringValue",
            allowed_values=("automatic", "off"),
            writable=True,
        ),
    )


def _threshold(value: float = 17.0) -> Resource:
    return Resource(
        THRESHOLD,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(
            resource_type="floatValue", unit="C", minimum=10, maximum=30, writable=True
        ),
    )


class _Cloud:
    """Answers like the controller: no threshold while the mode is off."""

    def __init__(self) -> None:
        self.mode = "off"
        self.calls = 0

    async def read(self, gateway, paths, **_kwargs):
        self.calls += 1
        results = []
        for path in paths:
            if path == MODE:
                results.append(
                    BatchItemResult(gateway, path, 200, resource=_mode(self.mode))
                )
            elif self.mode == "off":
                results.append(BatchItemResult(gateway, path, 404))
            else:
                results.append(
                    BatchItemResult(gateway, path, 200, resource=_threshold())
                )
        return tuple(results)


@pytest.fixture
def plant(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.metrics = RequestMetrics()
    cloud = _Cloud()
    client.get_resources_bulk.side_effect = cloud.read
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway(GATEWAY), entry
    )
    coordinator.resources = {MODE: _mode("off"), THRESHOLD: _threshold()}
    coordinator.data = {}
    coordinator.last_update_success = True
    coordinator._paths_by_group = {PollGroup.CONTROL: (MODE, THRESHOLD)}
    return coordinator, cloud


async def _cycles(coordinator, count: int) -> None:
    for _ in range(count):
        coordinator._next_update[PollGroup.CONTROL] = 0
        coordinator.data = await coordinator._async_update_data_locked()


def test_only_the_season_dependent_leaves_are_named() -> None:
    assert is_season_dependent(THRESHOLD)
    assert is_season_dependent(CIRCUIT + "/suWiCoolingThreshold")
    assert not is_season_dependent(CIRCUIT + "/manualRoomSetpoint")
    assert not is_season_dependent("/system/suWiThreshold")


@pytest.mark.parametrize("known", [False, True])
async def test_a_season_of_404_answers_does_not_end_the_leaf(plant, known) -> None:
    """Five or more answers in summer, then the mode changes: the value returns.

    ``known`` is a plant read before; otherwise this is the first read after a
    restart in summer, which used to pause the leaf for a day.
    """
    coordinator, cloud = plant
    if known:
        cloud.mode = "automatic"
        await _cycles(coordinator, 1)
        assert coordinator.data[THRESHOLD].available
        cloud.mode = "off"
    await _cycles(coordinator, 7)
    assert not coordinator.data[THRESHOLD].available if known else True
    assert not coordinator._resource_is_paused(THRESHOLD, monotonic())
    assert THRESHOLD in coordinator._paths_by_group[PollGroup.CONTROL]

    cloud.mode = "automatic"
    await _cycles(coordinator, 1)
    assert coordinator.data[THRESHOLD].available
    assert coordinator.data[THRESHOLD].resource.value == 17.0


async def test_other_leaves_keep_the_day_long_pause(plant) -> None:
    """The exception is narrow: a 404 of an ordinary leaf still pauses it."""
    coordinator, _cloud = plant
    other = CIRCUIT + "/boostTemperature"
    coordinator.resources[other] = Resource(other)
    coordinator._paths_by_group[PollGroup.CONTROL] += (other,)
    await _cycles(coordinator, 1)
    assert coordinator._resource_is_paused(other, monotonic())
    assert not coordinator._resource_is_paused(THRESHOLD, monotonic())
