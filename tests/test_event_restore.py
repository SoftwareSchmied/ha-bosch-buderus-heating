"""Restore notification timestamps without reporting a new event."""

import inspect
import json
import logging
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.components.event import EventEntity
from homeassistant.core import State
from homeassistant.helpers import restore_state
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.restore_state import RestoredExtraData
from homeassistant.util import dt as dt_util

from custom_components.bosch_buderus_heating.event import BoschBuderusNotificationEvent
from custom_components.bosch_buderus_heating.faults import FaultTracker
from tests.ha_test_helpers import mock_restore_cache
from tests.test_event import _coordinator

ENTITY_ID = "event.retained_notification"
TIMESTAMP = "2026-10-30T10:11:12.123+00:00"
ATTRIBUTES = {"code": "6249", "summary": "Offline fixture"}
# Newer Home Assistant keeps events of the same millisecond distinct by moving
# the later one forward; older releases keep the clock value unchanged.
NEXT_SAME_MILLISECOND = (
    "2026-10-30T10:11:12.124+00:00"
    if "milliseconds=1" in inspect.getsource(EventEntity._trigger_event)
    else TIMESTAMP
)


def stored(hass, state, extra):
    mock_restore_cache(hass, [State(ENTITY_ID, state)])
    if extra is not None:
        data = restore_state.async_get(hass)
        # Home Assistant 2026.11 keys the restored states by entity ID and by
        # registry ID; older releases have one mapping.
        states = getattr(data, "last_states_by_entity_id", None)
        if states is None:
            states = data.last_states
        states[ENTITY_ID].extra_data = RestoredExtraData(json.loads(json.dumps(extra)))


@pytest.mark.parametrize("last_state", ["unavailable", "unknown", TIMESTAMP])
@pytest.mark.parametrize("initially_available", [False, True])
async def test_serialized_event_restoration_keeps_timestamp_without_trigger(
    hass, last_state, initially_available
):
    stored(
        hass,
        last_state,
        {
            "last_event_type": "resolved",
            "last_event_attributes": ATTRIBUTES,
            "last_event_timestamp": TIMESTAMP,
        },
    )
    coordinator = _coordinator(hass)
    # FAULT-4: the entity is available while a fault source is supported,
    # independently of the cloud's last poll.
    supported = patch.object(
        FaultTracker,
        "has_supported_source",
        new_callable=PropertyMock,
        return_value=initially_available,
    )
    source = supported.start()
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    entity = BoschBuderusNotificationEvent(coordinator)
    entity.entity_id = ENTITY_ID
    try:
        with (
            patch.object(
                entity, "_trigger_event", wraps=entity._trigger_event
            ) as trigger,
            patch.object(
                BoschBuderusNotificationEvent,
                "device_info",
                new_callable=PropertyMock,
                return_value=None,
            ),
        ):
            await component.async_add_entities([entity])
            trigger.assert_not_called()
        current = hass.states.get(ENTITY_ID)
        assert current.state == (TIMESTAMP if initially_available else "unavailable")
        # A cloud outage alone does not make the event unavailable.
        coordinator.last_update_success = False
        entity.async_write_ha_state()
        assert hass.states.get(ENTITY_ID).state == current.state
        source.return_value = False
        entity.async_write_ha_state()
        assert hass.states.get(ENTITY_ID).state == "unavailable"
        # The stored payload remains usable after an offline restart as well.
        assert json.loads(json.dumps(entity.extra_restore_state_data.as_dict())) == {
            "last_event_type": "resolved",
            "last_event_attributes": ATTRIBUTES,
            "last_event_timestamp": TIMESTAMP,
        }
        source.return_value = True
        entity.async_write_ha_state()
        current = hass.states.get(ENTITY_ID)
        assert current.state == TIMESTAMP
        assert current.attributes.items() >= ATTRIBUTES.items()
        assert current.attributes["event_type"] == "resolved"
        with patch.object(
            dt_util, "utcnow", return_value=dt_util.parse_datetime(TIMESTAMP)
        ):
            entity._trigger_event("appeared", {"code": "1038"})
        entity.async_write_ha_state()
        current = hass.states.get(ENTITY_ID)
        assert current.state == NEXT_SAME_MILLISECOND
        assert current.attributes["event_type"] == "appeared"
        assert current.attributes["code"] == "1038"
    finally:
        await entity.async_remove()
        await coordinator.async_shutdown()
        supported.stop()


@pytest.mark.parametrize("last_state", ["unavailable", "unknown", TIMESTAMP])
async def test_legacy_event_storage_is_not_reinterpreted(hass, last_state):
    stored(
        hass,
        last_state,
        {"last_event_type": "resolved", "last_event_attributes": ATTRIBUTES},
    )
    coordinator = _coordinator(hass)
    entity = BoschBuderusNotificationEvent(coordinator)
    entity.hass, entity.entity_id = hass, ENTITY_ID
    state = await entity.async_get_last_state()
    assert state.state == last_state
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    "extra",
    [
        None,
        {},
        {"last_event_timestamp": None},
        {"last_event_timestamp": 123},
        {"last_event_timestamp": []},
        {"last_event_timestamp": "unavailable"},
        {"last_event_timestamp": "not-a-date"},
        {"last_event_timestamp": "2026-02-30T10:00:00+00:00"},
        {"last_event_timestamp": "2026-10-30T10:11:12"},
        {"last_event_timestamp": TIMESTAMP, "last_event_type": "unsupported"},
        {"last_event_timestamp": TIMESTAMP, "last_event_attributes": []},
    ],
)
async def test_missing_or_invalid_backup_timestamp_is_not_invented(hass, extra):
    if extra is not None:
        extra = {
            "last_event_type": "resolved",
            "last_event_attributes": ATTRIBUTES,
            **extra,
        }
    stored(hass, "unavailable", extra)
    coordinator = _coordinator(hass)
    entity = BoschBuderusNotificationEvent(coordinator)
    entity.hass, entity.entity_id = hass, ENTITY_ID
    assert (await entity.async_get_last_state()).state == "unavailable"
    await coordinator.async_shutdown()


async def test_first_start_has_no_event_or_timestamp(hass):
    mock_restore_cache(hass, [])
    coordinator = _coordinator(hass)
    entity = BoschBuderusNotificationEvent(coordinator)
    entity.hass, entity.entity_id = hass, ENTITY_ID
    assert await entity.async_get_last_state() is None
    assert entity.extra_restore_state_data.as_dict()["last_event_timestamp"] is None
    await coordinator.async_shutdown()


@pytest.mark.parametrize("missing", ["last_event_type", "last_event_attributes"])
async def test_incomplete_event_data_does_not_restore_a_timestamp(hass, missing):
    extra = {
        "last_event_type": "resolved",
        "last_event_attributes": ATTRIBUTES,
        "last_event_timestamp": TIMESTAMP,
    }
    del extra[missing]
    stored(hass, "unknown", extra)
    coordinator = _coordinator(hass)
    entity = BoschBuderusNotificationEvent(coordinator)
    entity.hass, entity.entity_id = hass, ENTITY_ID
    assert (await entity.async_get_last_state()).state == "unknown"
    await coordinator.async_shutdown()
