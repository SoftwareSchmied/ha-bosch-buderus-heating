"""Deep review 0.9.0-alpha.5: texts, bell wording, dismissals and display
levels of notifications (SYSTEM-06, -07, -08, -13).

SYSTEM-06, -07 and -13 apply to every gateway. SYSTEM-08 applies only to an
RRC 2.0 room controller, recognised by its readings; a K40 keeps its
classes (its real fault 6249 came with display level 4).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components import persistent_notification as pn
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating import i18n
from custom_components.bosch_buderus_heating.const import CONF_NOTIFY_NOTICES
from custom_components.bosch_buderus_heating.event import (
    BoschBuderusNotificationEvent,
)
from custom_components.bosch_buderus_heating.faults import (
    FaultSeverity,
    FaultTracker,
    fault_summary,
    parse_fault_resource,
)
from custom_components.bosch_buderus_heating.notifications import (
    FaultNotifications,
    notification_id,
)
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource
from custom_components.bosch_buderus_heating.pointt.models import (
    BatchItemResult,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests import test_notification_notices
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches, load_tree
from tests.test_entry import _entry
from tests.test_notification_notices import FAULT, SERVICE, WARNING, _shown
from tests.test_notifications import NOW, _apply

PACKAGE = "custom_components.bosch_buderus_heating"
GW = "gateway-one"
NOTIFICATIONS = "/notifications"
# A notification for the installer (display level 2) with a numeric class.
INSTALLER = {"ccd": "300", "dcd": "Z1", "fc": "4", "dlv": "2"}
USER_FAULT = {"ccd": "207", "dcd": "H07", "fc": "4", "dlv": "8"}
# The bell fixture of the notice tests: a K40 coordinator and its manager.
bell = test_notification_notices.bell
OTHER_LANGUAGES = [code for code in i18n.LANGUAGES if code not in ("en", "de")]


def notifications(*items: object) -> Resource:
    return Resource(path=NOTIFICATIONS, values=items, has_values=True)  # type: ignore[arg-type]


def only(*, room_controller: bool = False, **item: object):
    (fault,) = parse_fault_resource(
        notifications(item), observed_at=NOW, room_controller=room_controller
    ).faults
    return fault


# --------------------------------------------------------------- SYSTEM-06


@pytest.mark.parametrize(
    ("item", "german", "english"),
    [
        (
            {"ccd": "1234", "dcd": "H01", "fc": "18", "dlv": "8"},
            "Wartungsmeldung (Code 1234)",
            "Maintenance message (code 1234)",
        ),
        (
            {"ccd": "1235", "fc": 20},
            "Wartungsmeldung (Code 1235)",
            "Maintenance message (code 1235)",
        ),
        (
            {"ccd": "5000", "fc": "WARNING"},
            "Warnung (Code 5000)",
            "Warning (code 5000)",
        ),
        (
            {"ccd": "7", "fc": "INFO"},
            "Information (Code 7)",
            "Information (code 7)",
        ),
        # A radio device maintenance without a text of its own is no fault.
        (
            {"ccd": "1875", "dcd": "A84", "fc": 18, "dlv": 8},
            "Wartungsmeldung (Code 1875)",
            "Maintenance message (code 1875)",
        ),
        # Without a code: the severity itself.
        ({"occurrenceId": "m", "fc": "18"}, "Wartung", "Maintenance"),
        ({"occurrenceId": "w", "fc": "WARNING"}, "Warnung", "Warning"),
    ],
)
def test_maintenance_and_warnings_are_not_called_unknown_faults(
    item: dict[str, object], german: str, english: str
) -> None:
    fault = only(**item)
    assert fault_summary(fault, "de") == german
    assert fault_summary(fault, "en") == english
    for language in OTHER_LANGUAGES:
        text = fault_summary(fault, language)
        assert "{" not in text, language
        assert text != fault_summary(only(ccd=fault.code or "x"), language)


@pytest.mark.parametrize(
    ("item", "english"),
    [
        # Faults and unknown classes keep their texts.
        ({"ccd": "4711", "fc": "12"}, "Unknown fault (code 4711)"),
        ({"ccd": "4711"}, "Unknown fault (code 4711)"),
        (
            {"ccd": "1875", "dcd": "A84", "fc": 4, "dlv": 8},
            "Radio device fault (code 1875)",
        ),
        # A text of its own always wins, also for a maintenance class.
        (
            {"ccd": "1870", "dcd": "A84", "fc": 18, "dlv": 8},
            "Battery of a radio device low",
        ),
        (
            {"ccd": "6249", "fc": "12", "dlv": "4"},
            "Communication between indoor and outdoor unit interrupted",
        ),
    ],
)
def test_faults_and_own_texts_are_unchanged(
    item: dict[str, object], english: str
) -> None:
    assert fault_summary(only(**item), "en") == english


@pytest.mark.parametrize("language", OTHER_LANGUAGES)
def test_the_new_summaries_are_translated(language: str) -> None:
    catalog = i18n.CATALOGS[language]
    for item, english in (
        ({"ccd": "1", "fc": "18"}, "Maintenance message (code {code})"),
        ({"ccd": "1", "fc": "WARNING"}, "Warning (code {code})"),
        ({"ccd": "1", "fc": "INFO"}, "Information (code {code})"),
    ):
        assert fault_summary(only(**item), language) == catalog[english].format(
            code="1"
        )


async def test_bell_and_event_name_a_maintenance_message(
    hass: HomeAssistant, bell
) -> None:
    """On a K40 gateway too (class 18/20), with the option on."""
    entry, coordinator, manager = await bell(notices=True)
    event = BoschBuderusNotificationEvent(coordinator)
    event._trigger_event = Mock()  # type: ignore[method-assign]
    event.async_write_ha_state = Mock()  # type: ignore[method-assign]
    remove = coordinator.faults.async_add_listener(event._handle_fault_event)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, SERVICE, when=NOW + timedelta(minutes=1))
    await hass.async_block_till_done()
    message = _shown(hass, entry)["message"]
    # The bell escapes the brackets of the text.
    assert r"Maintenance message \(code 1234\)" in message
    assert "Unknown fault" not in message
    (call,) = event._trigger_event.call_args_list
    assert call.args[1]["summary"] == "Maintenance message (code 1234)"
    remove()


# --------------------------------------------------------------- SYSTEM-07


async def test_resolved_maintenance_is_not_called_a_fault(
    hass: HomeAssistant, bell
) -> None:
    entry, coordinator, manager = await bell(notices=True)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, SERVICE, when=NOW + timedelta(minutes=1))
    await hass.async_block_till_done()
    assert "notifications" in _shown(hass, entry)["title"]
    _apply(coordinator.faults, when=NOW + timedelta(minutes=2))
    _apply(coordinator.faults, when=NOW + timedelta(minutes=3))
    await hass.async_block_till_done()
    shown = _shown(hass, entry)
    assert shown["title"] == "Heating system 1: notifications cleared"
    assert shown["message"].startswith(
        "The previously reported notifications are no longer active."
    )
    assert "faults" not in shown["message"].split("\n\n")[0]
    assert "Confirmed resolved: 1" in shown["message"]


async def test_resolved_texts_follow_the_language(hass: HomeAssistant, bell) -> None:
    hass.config.language = "de"
    entry, coordinator, manager = await bell(notices=True)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, WARNING, when=NOW + timedelta(minutes=1))
    _apply(coordinator.faults, when=NOW + timedelta(minutes=2))
    _apply(coordinator.faults, when=NOW + timedelta(minutes=3))
    await hass.async_block_till_done()
    shown = _shown(hass, entry)
    assert shown["title"] == "Anlage 1: Meldungen erledigt"
    assert "Die zuvor gemeldeten Meldungen sind nicht mehr aktiv." in shown["message"]
    assert "Störungen behoben" not in shown["title"]


async def test_a_fault_with_a_warning_resolves_as_notifications(
    hass: HomeAssistant, bell
) -> None:
    entry, coordinator, manager = await bell(notices=True)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, FAULT, WARNING, when=NOW + timedelta(minutes=1))
    _apply(coordinator.faults, when=NOW + timedelta(minutes=2))
    _apply(coordinator.faults, when=NOW + timedelta(minutes=3))
    await hass.async_block_till_done()
    assert _shown(hass, entry)["title"] == "Heating system 1: notifications cleared"


@pytest.mark.parametrize("notices", [True, False])
async def test_resolved_faults_keep_their_wording(
    hass: HomeAssistant, bell, notices: bool
) -> None:
    """With faults only (and always with the option off) nothing changes."""
    entry, coordinator, manager = await bell(notices=notices)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, FAULT, SERVICE, when=NOW + timedelta(minutes=1))
    await hass.async_block_till_done()
    if notices:
        # The maintenance message ends first; the fault stays alone.
        _apply(coordinator.faults, FAULT, when=NOW + timedelta(minutes=2))
        _apply(coordinator.faults, FAULT, when=NOW + timedelta(minutes=3))
        pn.async_dismiss(hass, notification_id(entry.entry_id, GW))
        await hass.async_block_till_done()
        _apply(coordinator.faults, FAULT, when=NOW + timedelta(minutes=4))
        _apply(coordinator.faults, when=NOW + timedelta(minutes=5))
        await hass.async_block_till_done()
        assert _shown(hass, entry) is None  # dismissed: no resolved message
        return
    _apply(coordinator.faults, SERVICE, when=NOW + timedelta(minutes=2))
    _apply(coordinator.faults, SERVICE, when=NOW + timedelta(minutes=3))
    await hass.async_block_till_done()
    shown = _shown(hass, entry)
    assert shown["title"] == "Heating system 1: faults resolved"
    assert shown["message"].startswith(
        "The previously reported faults are no longer active."
    )


async def test_unconfirmed_notices_are_called_notifications(
    hass: HomeAssistant, bell
) -> None:
    entry, coordinator, manager = await bell(notices=True)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, WARNING, when=NOW + timedelta(minutes=1))
    coordinator.faults.mark_unavailable()
    await hass.async_block_till_done()
    message = _shown(hass, entry)["message"]
    assert (
        "Last reported notifications. Their current status has not yet been confirmed."
        in message
    )
    assert "Last reported faults" not in message


async def test_unconfirmed_faults_keep_their_wording(hass: HomeAssistant, bell) -> None:
    entry, coordinator, manager = await bell(notices=False)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, FAULT, WARNING, when=NOW + timedelta(minutes=1))
    coordinator.faults.mark_unavailable()
    await hass.async_block_till_done()
    message = _shown(hass, entry)["message"]
    assert "Last reported faults. Their current status" in message


async def test_omitted_resolved_notifications_are_counted_as_notifications(
    hass: HomeAssistant, bell
) -> None:
    entry, coordinator, manager = await bell(notices=True)
    await manager.async_start()
    _apply(coordinator.faults)
    many = [
        {"ccd": str(5000 + index), "fc": "WARNING", "occurrenceId": f"w{index}"}
        for index in range(27)
    ]
    _apply(coordinator.faults, *many, when=NOW + timedelta(minutes=1))
    _apply(coordinator.faults, when=NOW + timedelta(minutes=2))
    _apply(coordinator.faults, when=NOW + timedelta(minutes=3))
    await hass.async_block_till_done()
    message = _shown(hass, entry)["message"]
    assert "2 older cleared notifications are no longer listed." in message
    assert "resolved faults" not in message


# --------------------------------------------------------------- SYSTEM-13


async def test_toggle_off_on_keeps_a_dismissed_warning_dismissed(
    hass: HomeAssistant, bell
) -> None:
    entry, coordinator, manager = await bell(notices=True)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, WARNING, when=NOW + timedelta(minutes=1))
    await hass.async_block_till_done()
    assert _shown(hass, entry) is not None
    pn.async_dismiss(hass, notification_id(entry.entry_id, GW))
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(entry, options={CONF_NOTIFY_NOTICES: False})
    manager.async_update()
    _apply(coordinator.faults, WARNING, when=NOW + timedelta(minutes=2))
    hass.config_entries.async_update_entry(entry, options={CONF_NOTIFY_NOTICES: True})
    manager.async_update()
    await hass.async_block_till_done()
    assert _shown(hass, entry) is None, "dismissed warning shown again"


async def test_dismissing_a_fault_keeps_an_earlier_warning_dismissal(
    hass: HomeAssistant, bell
) -> None:
    entry, coordinator, manager = await bell(notices=True)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, WARNING, when=NOW + timedelta(minutes=1))
    await hass.async_block_till_done()
    pn.async_dismiss(hass, notification_id(entry.entry_id, GW))
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(entry, options={CONF_NOTIFY_NOTICES: False})
    manager.async_update()
    _apply(coordinator.faults, WARNING, FAULT, when=NOW + timedelta(minutes=2))
    await hass.async_block_till_done()
    assert "6249" in _shown(hass, entry)["message"]
    pn.async_dismiss(hass, notification_id(entry.entry_id, GW))
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(entry, options={CONF_NOTIFY_NOTICES: True})
    manager.async_update()
    await hass.async_block_till_done()
    assert _shown(hass, entry) is None


async def test_a_resolved_dismissed_warning_loses_its_dismissal(
    hass: HomeAssistant, bell
) -> None:
    """Failure path: no stale dismissal; a recurrence shows again."""
    entry, coordinator, manager = await bell(notices=True)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, WARNING, when=NOW + timedelta(minutes=1))
    await hass.async_block_till_done()
    pn.async_dismiss(hass, notification_id(entry.entry_id, GW))
    await hass.async_block_till_done()
    assert manager._dismissed
    hass.config_entries.async_update_entry(entry, options={CONF_NOTIFY_NOTICES: False})
    manager.async_update()
    _apply(coordinator.faults, when=NOW + timedelta(minutes=2))
    _apply(coordinator.faults, when=NOW + timedelta(minutes=3))
    await hass.async_block_till_done()
    assert manager._dismissed == {}
    hass.config_entries.async_update_entry(entry, options={CONF_NOTIFY_NOTICES: True})
    manager.async_update()
    _apply(coordinator.faults, WARNING, when=NOW + timedelta(minutes=4))
    await hass.async_block_till_done()
    assert "5000" in _shown(hass, entry)["message"]


async def test_a_failed_read_keeps_the_dismissal(hass: HomeAssistant, bell) -> None:
    entry, coordinator, manager = await bell(notices=False)
    await manager.async_start()
    _apply(coordinator.faults)
    _apply(coordinator.faults, WARNING, when=NOW + timedelta(minutes=1))
    hass.config_entries.async_update_entry(entry, options={CONF_NOTIFY_NOTICES: True})
    manager.async_update()
    await hass.async_block_till_done()
    pn.async_dismiss(hass, notification_id(entry.entry_id, GW))
    await hass.async_block_till_done()
    coordinator.faults.mark_unavailable()
    hass.config_entries.async_update_entry(entry, options={CONF_NOTIFY_NOTICES: False})
    manager.async_update()
    hass.config_entries.async_update_entry(entry, options={CONF_NOTIFY_NOTICES: True})
    manager.async_update()
    await hass.async_block_till_done()
    assert _shown(hass, entry) is None
    assert len(manager._dismissed) == 1


# --------------------------------------------------------------- SYSTEM-08


@pytest.mark.parametrize(
    ("item", "severity"),
    [
        ({"fc": "4", "dlv": "2"}, FaultSeverity.INFO),
        ({"fc": 4, "dlv": 0}, FaultSeverity.INFO),
        ({"fc": "17", "dlv": "4"}, FaultSeverity.INFO),
        ({"fc": "4", "dlv": "8"}, FaultSeverity.FAULT),
        # A missing or another level stays unknown, so a problem.
        ({"fc": "4"}, FaultSeverity.UNKNOWN),
        ({"fc": "4", "dlv": "16"}, FaultSeverity.UNKNOWN),
        ({"fc": "4", "dlv": "x"}, FaultSeverity.UNKNOWN),
        ({"fc": "19", "dlv": "2"}, FaultSeverity.UNKNOWN),
        # Known meanings win over the display level.
        ({"fc": "12", "dlv": "4"}, FaultSeverity.FAULT),
        ({"fc": "18", "dlv": "2"}, FaultSeverity.MAINTENANCE),
        ({"fc": "WARNING", "dlv": "0"}, FaultSeverity.WARNING),
    ],
)
def test_display_levels_of_a_room_controller(
    item: dict[str, object], severity: FaultSeverity
) -> None:
    fault = only(ccd="300", dcd="Z1", room_controller=True, **item)
    assert fault.severity is severity
    # Other gateways keep their classes: no display level hides anything.
    other = only(ccd="300", dcd="Z1", **item)
    if severity is FaultSeverity.INFO:
        assert other.severity is FaultSeverity.UNKNOWN
    else:
        assert other.severity is severity
    # The display level never changes the identity of the notification.
    assert other.fingerprint == fault.fingerprint


def test_a_k40_tracker_keeps_counting_installer_levels(hass: HomeAssistant) -> None:
    tracker = FaultTracker(hass, "entry", GW)
    # The zone list as a real K40 answers it: no RRC zone array.
    zones = Resource(
        "/zones/list",
        values=(),
        has_values=True,
        metadata=ResourceMetadata(resource_type="zoneArray"),
    )
    tracker.record_results([BatchItemResult(GW, "/zones/list", 200, zones)])
    tracker.process_resources({NOTIFICATIONS: notifications(INSTALLER)})
    assert not tracker.room_controller
    (fault,) = tracker.active_faults
    assert fault.severity is FaultSeverity.UNKNOWN
    assert "below_user_display_level" not in tracker.diagnostics()


async def test_a_room_controller_lists_but_does_not_count_installer_levels(
    hass: HomeAssistant, hass_storage
) -> None:
    tracker = FaultTracker(hass, "entry", GW)
    # Learnt from a zone reading of the cycle, as the coordinator passes it.
    tracker.record_results(
        [BatchItemResult(GW, "/zones/zn1", 200, Resource("/zones/zn1"))]
    )
    assert tracker.room_controller
    tracker.process_resources(
        {NOTIFICATIONS: notifications(INSTALLER, USER_FAULT)}, observed_at=NOW
    )
    assert [item.code for item in tracker.active] == ["207", "300"]
    (fault,) = tracker.active_faults
    assert fault.code == "207"
    info = next(item for item in tracker.active if item.code == "300")
    assert info.severity is FaultSeverity.INFO
    assert info.display_level_raw == "2"
    assert fault_summary(info, "en") == "Information (code 300)"
    diagnostics = tracker.diagnostics()
    assert diagnostics["below_user_display_level"] == 1
    assert diagnostics["severity_counts"] == {"fault": 1, "info": 1}
    assert diagnostics["active_faults"] == 1
    await tracker.async_flush()
    stored = next(
        value["data"] for key, value in hass_storage.items() if ".faults." in key
    )
    assert stored["room_controller"] is True
    # A restart reads the levels the same way from its first list.
    restarted = FaultTracker(hass, "entry", GW)
    await restarted.async_load()
    assert restarted.room_controller
    events = restarted.process_resources(
        {NOTIFICATIONS: notifications(INSTALLER, USER_FAULT)}
    )
    assert events == ()
    assert len(restarted.active_faults) == 1


def test_evidence_of_another_gateway_does_not_count(hass: HomeAssistant) -> None:
    tracker = FaultTracker(hass, "entry", GW)
    tracker.record_results(
        [
            # A failed zone read is no evidence.
            BatchItemResult(GW, "/zones/zn1", 500),
            BatchItemResult(
                GW,
                "/gateway/productType",
                200,
                Resource("/gateway/productType", value="K40", has_value=True),
            ),
            BatchItemResult(
                GW, "/heatingCircuits/hc1", 200, Resource("/heatingCircuits/hc1")
            ),
        ]
    )
    tracker.reconcile_discovery({"/system": Resource("/system")})
    assert not tracker.room_controller
    # A fully answered discovery with RRC zones is evidence.
    tracker.reconcile_discovery({"/zones/zn2": Resource("/zones/zn2")})
    assert tracker.room_controller


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_installer_levels_on_a_real_room_controller(
    hass: HomeAssistant,
) -> None:
    """Real setup and coordinator cycles: the System fault sensor, Active
    faults and the bell count only the user-level fault; Active
    notifications lists both."""
    tree = load_tree("rrc/ct200-shapes.json")
    tree[NOTIFICATIONS] = parse_resource(
        {
            "id": NOTIFICATIONS,
            "type": "errorList",
            "writeable": 0,
            "recordable": 0,
            "value": [INSTALLER],
        }
    )
    cloud, clock = RrcCloud(tree), Clock()
    entry = _entry(hass)
    stack = cloud_patches(cloud, clock, Gateway(GW))
    try:
        with patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        coordinator = entry.runtime_data.coordinators[0]
        assert coordinator.faults.room_controller
        manager = FaultNotifications(hass, entry, coordinator)
        await manager.async_start()

        def state(unique_id: str):
            from homeassistant.helpers import entity_registry as er

            entity_id = er.async_get(hass).async_get_entity_id(
                unique_id[0], "bosch_buderus_heating", unique_id[1]
            )
            assert entity_id is not None, unique_id
            return hass.states.get(entity_id)

        fault_sensor = ("binary_sensor", f"{GW}:system_fault")
        assert state(fault_sensor).state == "off"
        assert state(("sensor", f"{GW}:active_faults")).state == "0"
        listed = state(("sensor", f"{GW}:active_notifications"))
        assert listed.state == "1"
        assert listed.attributes["severity_counts"] == {"info": 1}
        assert listed.attributes["faults"][0]["summary"] == "Information (code 300)"
        assert _current_notification(hass, entry) is None

        # A user-level fault next to it: only that one is a problem.
        tree[NOTIFICATIONS] = parse_resource(
            {
                "id": NOTIFICATIONS,
                "type": "errorList",
                "writeable": 0,
                "recordable": 0,
                "value": [INSTALLER, USER_FAULT],
            }
        )
        cloud.tree[NOTIFICATIONS] = tree[NOTIFICATIONS]
        clock.now += 15 * 60
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert state(fault_sensor).state == "on"
        assert state(("sensor", f"{GW}:active_faults")).state == "1"
        assert state(("sensor", f"{GW}:active_notifications")).state == "2"
        message = _current_notification(hass, entry)["message"]
        assert "207" in message and "300" not in message
        await manager.async_close()
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        stack.close()


def _current_notification(hass: HomeAssistant, entry) -> dict[str, str] | None:
    notification = pn._async_get_or_create_notifications(hass).get(  # type: ignore[attr-defined]
        notification_id(entry.entry_id, GW)
    )
    return None if notification is None else dict(notification)
