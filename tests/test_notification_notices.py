"""Classes 18 and 20 as maintenance on every gateway, and the bell option
for warnings and maintenance (off by default)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components import persistent_notification as pn
from homeassistant.core import HomeAssistant, callback

from custom_components.bosch_buderus_heating.const import (
    CONF_GATEWAY_IDS,
    CONF_NOTIFY_NOTICES,
    DOMAIN,
)
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.event import (
    BoschBuderusNotificationEvent,
)
from custom_components.bosch_buderus_heating.faults import FaultSeverity
from custom_components.bosch_buderus_heating.notifications import (
    FaultNotifications,
    notification_id,
    notification_includes_notices,
)
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusActiveFaultsSensor,
    BoschBuderusActiveNotificationsSensor,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_notifications import NOW, _apply

FAULT = {"ccd": "6249", "fc": "12", "occurrenceId": "a"}
SERVICE = {"ccd": "1234", "dcd": "H01", "fc": "18", "occurrenceId": "m"}
INSPECTION = {"ccd": "1235", "fc": 20, "occurrenceId": "n"}
WARNING = {"ccd": "5000", "fc": "WARNING", "occurrenceId": "w"}


def _coordinator(hass: HomeAssistant, entry, device_type: str | None):
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway("gateway-one", device_type=device_type), entry
    )
    coordinator.last_update_success = True
    return coordinator


@pytest.mark.parametrize("device_type", ["K40", None, "rrc2"])
async def test_classes_18_and_20_are_maintenance_on_every_gateway(
    hass: HomeAssistant, device_type: str | None
) -> None:
    """Decision B: maintenance, not a fault, also on an EMS or K40 gateway;
    still counted and listed as a notification, and an event appears."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_GATEWAY_IDS: ["gateway-one"]})
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry, device_type)
    event = BoschBuderusNotificationEvent(coordinator)
    event._trigger_event = Mock()  # type: ignore[method-assign]
    event.async_write_ha_state = Mock()  # type: ignore[method-assign]
    _apply(coordinator.faults)
    lifecycle = coordinator.faults.process_resources(
        {
            "/notifications": Resource(
                path="/notifications",
                values=(SERVICE, INSPECTION),
                has_values=True,
            )
        },
        observed_at=NOW + timedelta(minutes=1),
    )
    assert {item.fault.severity for item in lifecycle} == {FaultSeverity.MAINTENANCE}
    assert not coordinator.faults.active_faults
    assert coordinator.faults.highest_severity is FaultSeverity.MAINTENANCE

    faults = BoschBuderusActiveFaultsSensor(coordinator)
    notifications = BoschBuderusActiveNotificationsSensor(coordinator)
    assert faults.native_value == 0
    assert notifications.native_value == 2
    attributes = notifications.extra_state_attributes
    assert attributes["severity_counts"] == {"maintenance": 2}
    assert {item["code"] for item in attributes["faults"]} == {"1234", "1235"}

    for item in lifecycle:
        event._handle_fault_event(item)
    assert event._trigger_event.call_count == 2
    kinds = {call.args[0] for call in event._trigger_event.call_args_list}
    assert kinds == {"appeared"}
    assert {
        call.args[1]["severity"] for call in event._trigger_event.call_args_list
    } == {"maintenance"}
    await coordinator.async_shutdown()


CURRENT: dict[str, dict[str, str]] = {}


@pytest.fixture
async def bell(hass):
    await hass.config.async_set_time_zone("UTC")
    CURRENT.clear()

    @callback
    def record(kind, notifications):
        for key, value in notifications.items():
            if kind is pn.UpdateType.REMOVED:
                CURRENT.pop(key, None)
            else:
                CURRENT[key] = value

    remove = pn.async_register_callback(hass, record)

    async def make(*, notices: bool | None):
        options = {} if notices is None else {CONF_NOTIFY_NOTICES: notices}
        entry = MockConfigEntry(
            domain=DOMAIN, data={CONF_GATEWAY_IDS: ["gateway-one"]}, options=options
        )
        entry.add_to_hass(hass)
        coordinator = _coordinator(hass, entry, "K40")
        manager = FaultNotifications(hass, entry, coordinator)
        made.append((manager, coordinator))
        return entry, coordinator, manager

    made: list[tuple[FaultNotifications, BoschBuderusDataUpdateCoordinator]] = []
    yield make
    for manager, coordinator in made:
        await manager.async_close()
        await coordinator.async_shutdown()
    remove()


def _shown(hass: HomeAssistant, entry) -> dict[str, str] | None:
    return CURRENT.get(notification_id(entry.entry_id, "gateway-one"))


def test_the_option_is_off_by_default() -> None:
    assert not notification_includes_notices({})
    assert not notification_includes_notices({CONF_NOTIFY_NOTICES: "yes"})
    assert notification_includes_notices({CONF_NOTIFY_NOTICES: True})


@pytest.mark.parametrize("notices", [None, False])
async def test_off_keeps_warnings_and_maintenance_out_of_the_bell(
    hass: HomeAssistant, bell, notices
) -> None:
    entry, coordinator, manager = await bell(notices=notices)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, SERVICE, WARNING, when=NOW + timedelta(minutes=1))
    await hass.async_block_till_done()
    assert _shown(hass, entry) is None
    _apply(coordinator.faults, SERVICE, FAULT, when=NOW + timedelta(minutes=2))
    await hass.async_block_till_done()
    message = _shown(hass, entry)["message"]
    assert "Active faults: 1" in message
    assert "6249" in message and "1234" not in message


async def test_on_shows_resolves_and_respects_dismissal_like_faults(
    hass: HomeAssistant, bell
) -> None:
    entry, coordinator, manager = await bell(notices=True)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, SERVICE, WARNING, when=NOW + timedelta(minutes=1))
    await hass.async_block_till_done()
    shown = _shown(hass, entry)
    assert "heating system notifications" in shown["title"]
    assert "Active notifications: 2" in shown["message"]
    assert "Maintenance" in shown["message"] and "1234" in shown["message"]
    assert "Warning" in shown["message"] and "5000" in shown["message"]
    # Resolution after two complete, valid confirmations, as for faults.
    _apply(coordinator.faults, WARNING, when=NOW + timedelta(minutes=2))
    _apply(coordinator.faults, WARNING, when=NOW + timedelta(minutes=3))
    await hass.async_block_till_done()
    message = _shown(hass, entry)["message"]
    assert "Active notifications: 1" in message
    assert "Confirmed resolved: 1" in message
    # A dismissed warning stays dismissed; a fault shows the bell again.
    pn.async_dismiss(hass, notification_id(entry.entry_id, "gateway-one"))
    await hass.async_block_till_done()
    _apply(coordinator.faults, WARNING, when=NOW + timedelta(minutes=4))
    await hass.async_block_till_done()
    assert _shown(hass, entry) is None
    _apply(coordinator.faults, WARNING, FAULT, when=NOW + timedelta(minutes=5))
    await hass.async_block_till_done()
    shown = _shown(hass, entry)
    assert "Active notifications: 2" in shown["message"]
    # Faults rank first.
    assert shown["message"].index("6249") < shown["message"].index("5000")


async def test_a_fault_alone_keeps_its_title_with_the_option_on(
    hass: HomeAssistant, bell
) -> None:
    entry, coordinator, manager = await bell(notices=True)
    await manager.async_start()
    _apply(coordinator.faults, FAULT)
    await hass.async_block_till_done()
    shown = _shown(hass, entry)
    assert "heating system fault" in shown["title"]
    assert "Active faults: 1" in shown["message"]


async def test_switching_the_option_applies_at_once(hass: HomeAssistant, bell) -> None:
    entry, coordinator, manager = await bell(notices=False)
    await manager.async_start()
    _apply(coordinator.faults, SERVICE)
    await hass.async_block_till_done()
    assert _shown(hass, entry) is None
    hass.config_entries.async_update_entry(entry, options={CONF_NOTIFY_NOTICES: True})
    manager.async_update()
    await hass.async_block_till_done()
    assert "Active notifications: 1" in _shown(hass, entry)["message"]
    hass.config_entries.async_update_entry(entry, options={CONF_NOTIFY_NOTICES: False})
    manager.async_update()
    await hass.async_block_till_done()
    # Removed, not turned into a "changed classification" message.
    assert _shown(hass, entry) is None
