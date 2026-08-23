"""Tests for PointT notification lifecycle events."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.event import (
    BoschBuderusNotificationEvent,
    async_setup_entry,
)
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource


def _coordinator(hass: HomeAssistant) -> BoschBuderusDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type="K40"), entry
    )
    coordinator.last_update_success = True
    return coordinator


async def test_event_platform_adds_one_stream_per_gateway(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    entry = SimpleNamespace(runtime_data=SimpleNamespace(coordinators=(coordinator,)))
    added: list[BoschBuderusNotificationEvent] = []

    await async_setup_entry(hass, entry, added.extend)

    assert len(added) == 1
    assert added[0].unique_id == "gateway-one:system_notifications"
    assert added[0].event_types == ["appeared", "resolved"]
    assert not added[0].available


def test_event_contains_bounded_localized_attributes(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    event_entity = BoschBuderusNotificationEvent(coordinator)
    event_entity._trigger_event = Mock()  # type: ignore[method-assign]
    event_entity.async_write_ha_state = Mock()  # type: ignore[method-assign]
    empty = Resource(path="/notifications", values=())
    active = Resource(
        path="/notifications",
        values=({"ccd": 6249, "fc": "12", "orig": "8"},),
    )
    coordinator.faults.process_resources(
        {empty.path: empty}, observed_at=datetime(2026, 8, 19, tzinfo=UTC)
    )
    lifecycle = coordinator.faults.process_resources(
        {active.path: active},
        observed_at=datetime(2026, 8, 19, 0, 2, tzinfo=UTC),
    )[0]

    event_entity._handle_fault_event(lifecycle)

    assert event_entity.available
    event_entity._trigger_event.assert_called_once()
    event_type, attributes = event_entity._trigger_event.call_args.args
    assert event_type == "appeared"
    assert attributes == {
        "severity": "fault",
        "severity_label": "Fault",
        "component": "system",
        "summary": "Communication between indoor and outdoor unit interrupted",
        "observed_at": "2026-08-19T00:02:00+00:00",
        "time_source": "home_assistant_observed",
        "code": "6249",
    }
    event_entity.async_write_ha_state.assert_called_once_with()
    assert event_entity.device_info["model"] == "Heating system"


async def test_added_event_entity_subscribes_and_includes_optional_fields(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    entity = BoschBuderusNotificationEvent(coordinator)
    entity.hass = hass
    entity._trigger_event = Mock()  # type: ignore[method-assign]
    entity.async_write_ha_state = Mock()  # type: ignore[method-assign]
    await entity.async_added_to_hass()
    empty = Resource(path="/notifications", values=())
    active = Resource(
        path="/notifications",
        values=(
            {
                "ccd": 1038,
                "dcd": "A11",
                "fc": "BLOCKING",
                "t": "2026-08-18T23:59:00Z",
            },
        ),
    )
    coordinator.faults.process_resources({empty.path: empty})
    coordinator.faults.process_resources({active.path: active})

    attributes = entity._trigger_event.call_args.args[1]
    assert attributes["subcode"] == "A11"
    assert attributes["occurred_at"] == "2026-08-18T23:59:00+00:00"
    entity.async_write_ha_state.assert_called_once_with()
    await entity.async_will_remove_from_hass()
    await coordinator.async_shutdown()
