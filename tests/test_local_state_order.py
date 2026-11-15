"""The options of a local state signal do not depend on the gateway's key order.

The gateway reports a signal's state table as a JSON object whose key order
changes between answers (seen in real recordings for ``GWEEBUS.Status`` and
the holiday states). The states are kept in the order of their values, so the
options of the entity, and with them the registry entry, stay the same.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from tests.test_c7_signals import shown, signal
from tests.test_local_only import by_path, coordinator_with, track

NAME = "SC.HolidayNew.1.State"
PATH = f"/signals/{NAME}"
ORDERS = (
    {"PLANNED": 1, "ACTIVE": 2, "IDLE": 0},
    {"ACTIVE": 2, "IDLE": 0, "PLANNED": 1},
    {"IDLE": 0, "PLANNED": 1, "ACTIVE": 2},
)


@pytest.mark.parametrize("states", ORDERS)
def test_the_states_are_in_the_order_of_their_values(states) -> None:
    observation = signal(NAME, 1, state=states)
    assert [state.name for state in observation.data.states] == [
        "IDLE",
        "PLANNED",
        "ACTIVE",
    ]
    sensor, value = shown(observation)
    assert sensor.options == ("idle", "planned", "active")
    assert value == "planned"


async def test_a_restart_with_another_key_order_keeps_the_options(
    hass: HomeAssistant,
) -> None:
    seen = []
    for states in ORDERS:
        coordinator = coordinator_with(hass, [signal(NAME, 2, state=states)])
        entity = by_path(track(coordinator))[PATH]
        seen.append(tuple(entity.options or ()))
        assert entity.native_value == "active"
        await coordinator.async_shutdown()
    assert set(seen) == {("idle", "planned", "active")}
