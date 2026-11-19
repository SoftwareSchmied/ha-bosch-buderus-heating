"""X16: a pass of local readings reaches only the entities it concerns.

The managed entry runs with its real platforms against a synthetic cloud and
published local readings; entity writes are counted per entity.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.gateway_identity import GatewayNamespace
from tests.test_local_backed import (
    _READ_TIMES,
    CURRENT,
    GATEWAY,
    SENSOR,
    ReadingBackend,
    local_values,
)
from tests.test_local_selection import local
from tests.test_managed_platforms import native
from tests.test_managed_pointt import eventually, running, source_for

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")

# A cloud reading without a local counterpart.
LIMIT = "/heatingCircuits/hc2/maxFlowTemp"
LIMIT_SENSOR = GATEWAY + ":heatingCircuits:hc2:maxFlowTemp"


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    hass.config.components.add(DOMAIN)


class Backend(ReadingBackend):
    def resource(self, gateway, path):
        if path == "/heatingCircuits/hc2":
            references = super().resource(gateway, path)["references"]
            return {"references": [*references, {"id": LIMIT}]}
        if path == LIMIT:
            return {
                "id": LIMIT,
                "type": "floatValue",
                "value": 60.0,
                "unitOfMeasure": "C",
                "writeable": False,
            }
        return super().resource(gateway, path)


def publish(owner, current=None, limit=None):
    """The current local readings, as the local source publishes them."""
    runtime = owner.connections
    context = runtime._contexts["local"]
    readings = []
    for path, value in ((CURRENT, current), (LIMIT, limit)):
        if value is not None:
            readings.append(local(path, value, monotonic=next(_READ_TIMES)))
    runtime._local_states["local"] = SimpleNamespace(
        context=context.identity_context(GatewayNamespace.K40_LOCAL),
        current_observations=tuple(readings),
    )
    runtime._notify()


class Counter:
    """Entity writes by entity, counted without changing them."""

    def __init__(self, entities):
        self.counts = dict.fromkeys(entities, 0)
        self._patches = []
        for entity in entities:
            original = entity.async_write_ha_state

            def counted(entity=entity, original=original):
                self.counts[entity] += 1
                original()

            patcher = patch.object(entity, "async_write_ha_state", counted)
            patcher.start()
            self._patches.append(patcher)

    def stop(self):
        for patcher in self._patches:
            patcher.stop()


def shown(hass, entity):
    return hass.states.get(entity.entity_id).state


async def test_a_pass_of_local_readings_reaches_only_the_changed_entities(hass):
    async with running(hass, Backend(), configure=local_values()) as (
        entry,
        _,
        owner,
        _,
    ):
        # Enabled from the start (it is a disabled entity by default).
        er.async_get(hass).async_get_or_create(
            "sensor",
            DOMAIN,
            LIMIT_SENSOR,
            config_entry=entry,
            disabled_by=None,
        )
        await owner.start_platforms()
        await eventually(lambda: native(owner, "sensor", SENSOR))
        await eventually(lambda: native(owner, "sensor", LIMIT_SENSOR))
        current = native(owner, "sensor", SENSOR)
        limit = native(owner, "sensor", LIMIT_SENSOR)
        publish(owner, 23.5, 58.0)
        await eventually(lambda: shown(hass, current) == "23.5")
        await eventually(lambda: shown(hass, limit) == "58.0")
        coordinator = source_for(owner, GATEWAY)
        counter = Counter((current, limit))
        try:
            # The same answers again with a new time: nothing is written.
            publish(owner, 23.5, 58.0)
            assert counter.counts == {current: 0, limit: 0}
            # A changed value arrives at once; only its entity is written.
            publish(owner, 24.0, 58.0)
            assert shown(hass, current) == "24.0"
            assert counter.counts == {current: 1, limit: 0}
            publish(owner, 24.0, 57.5)
            assert shown(hass, limit) == "57.5"
            assert counter.counts == {current: 1, limit: 1}
            # The readings are gone (a paused access): the cloud values again.
            publish(owner)
            assert counter.counts == {current: 2, limit: 2}
            assert shown(hass, current) == "20.0" and shown(hass, limit) == "60.0"
            # They come back.
            publish(owner, 25.0, 59.0)
            assert shown(hass, current) == "25.0" and shown(hass, limit) == "59.0"
            assert counter.counts == {current: 3, limit: 3}
            # More than a reading changed (the cloud became unavailable):
            # every entity looks again for a single changed value.
            with patch.object(
                type(coordinator), "_cloud_down", return_value=("rate_limited", None)
            ):
                publish(owner, 26.0, 59.0)
            assert shown(hass, current) == "26.0"
            assert counter.counts == {current: 4, limit: 4}
            assert coordinator.local_changed_paths is None  # only during a pass
        finally:
            counter.stop()
