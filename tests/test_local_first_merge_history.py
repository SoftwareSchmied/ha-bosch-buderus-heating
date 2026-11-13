"""P49b step 8 (8C-c): a local-only sensor keeps its recorded history when the
cloud's sensor continues it after the system joined its cloud.

Runs the managed entry with its real platforms and Home Assistant's recorder.
"""

from __future__ import annotations

import asyncio

from homeassistant.data_entry_flow import FlowResultType

from custom_components.bosch_buderus_heating.const import DOMAIN
from tests.test_cloud_setup import backend as backend
from tests.test_local_first import local_first
from tests.test_local_first_merge import (
    GATEWAY,
    LOCAL_ONLY,
    OfferingBackend,
    add_cloud,
    evidence,
    local_view,
)
from tests.test_managed_platforms import native
from tests.test_managed_pointt import eventually, running, source_for
from tests.test_start_counters import (
    recorder_runtime_annotations,  # noqa: F401 - fixture
)


async def test_the_continued_entity_keeps_its_recorded_states(
    recorder_runtime_annotations,  # noqa: F811
    recorder_mock,
    hass,
    enable_custom_integrations,
    socket_enabled,
    backend,
) -> None:
    # The recorder starts before Home Assistant: no module-level fixtures.
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import (
        state_changes_during_period,
    )
    from homeassistant.helpers import entity_registry as er
    from homeassistant.util import dt as dt_util

    from tests.ha_recorder_helpers import async_wait_recording_done

    hass.config.components.add(DOMAIN)
    start = dt_util.utcnow()
    async with asyncio.timeout(90):
        async with running(hass, OfferingBackend(), configure=local_first) as (
            entry,
            _,
            owner,
            _,
        ):
            await local_view(hass, owner)
            entity_id = native(owner, "sensor", LOCAL_ONLY).entity_id
            await eventually(lambda: hass.states.get(entity_id).state == "21.25")
            await async_wait_recording_done(hass)
            with evidence():
                result = await add_cloud(hass, entry, [GATEWAY])
            assert result["type"] is FlowResultType.CREATE_ENTRY, result
            await eventually(lambda: source_for(owner, GATEWAY) is not None)
            await eventually(
                lambda: (
                    (state := hass.states.get(entity_id)) is not None
                    and state.state == "35.0"
                )
            )
            record = er.async_get(hass).async_get(entity_id)
            assert record.unique_id != LOCAL_ONLY  # now the cloud's sensor
            await async_wait_recording_done(hass)
            history = await get_instance(hass).async_add_executor_job(
                state_changes_during_period, hass, start, None, entity_id
            )
    values = [state.state for state in history.get(entity_id, [])]
    # One entity, one history: the local reading before, the cloud's after.
    assert "21.25" in values and "35.0" in values
    assert values.index("21.25") < values.index("35.0")
