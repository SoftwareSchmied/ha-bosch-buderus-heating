"""Persistent Home Assistant fault notifications using the existing fault state."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from homeassistant.components import persistent_notification as pn
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from .const import CONF_FAULT_NOTIFICATIONS, CONF_GATEWAY_IDS, DOMAIN
from .faults import (
    ActiveFault,
    FaultEventType,
    FaultLifecycleEvent,
    FaultSeverity,
    fault_severity_label,
    fault_summary,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import BoschBuderusDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)
_MAX_DETAILS = 25
_RANK = {FaultSeverity.UNKNOWN: 0, FaultSeverity.FAULT: 1, FaultSeverity.CRITICAL: 2}
_TEXT = {
    "en": {
        "title": "{name}: heating system fault",
        "resolved_title": "{name}: faults resolved",
        "changed_title": "{name}: heating system notifications",
        "active": "Active faults: {count}",
        "changed": "Still reported with a changed classification: {count}",
        "resolved_details": "Confirmed resolved: {count}",
        "first_seen": "First detected",
        "resolved_at": "Confirmed resolved",
        "observation_times": "Times observed by Home Assistant ({zone}).",
        "unconfirmed": (
            "Last reported faults. Their current status has not yet been confirmed."
        ),
        "resolved": "The previously reported faults are no longer active.",
        "more": "{count} additional active faults.",
        "more_changed": "{count} additional notifications with changed classification.",
        "more_resolved": "{count} older resolved faults are no longer listed.",
        "open": "Open heating system",
        "dismiss": (
            "Dismissing this notification does not acknowledge or reset a fault "
            "on the heating system."
        ),
        "system": "Heating system {number}",
    },
    "de": {
        "title": "{name}: Anlagenstörung",
        "resolved_title": "{name}: Störungen behoben",
        "changed_title": "{name}: Anlagenmeldungen",
        "active": "Aktive Störungen: {count}",
        "changed": "Weiterhin gemeldet, mit geänderter Einstufung: {count}",
        "resolved_details": "Bestätigt behoben: {count}",
        "first_seen": "Erstmals erkannt",
        "resolved_at": "Als behoben bestätigt",
        "observation_times": (
            "Zeitpunkte nach Beobachtung durch Home Assistant ({zone})."
        ),
        "unconfirmed": (
            "Zuletzt gemeldete Störungen. "
            "Ihr aktueller Status ist noch nicht bestätigt."
        ),
        "resolved": "Die zuvor gemeldeten Störungen sind nicht mehr aktiv.",
        "more": "{count} weitere aktive Störungen.",
        "more_changed": "{count} weitere Meldungen mit geänderter Einstufung.",
        "more_resolved": (
            "{count} ältere behobene Störungen werden nicht mehr aufgelistet."
        ),
        "open": "Anlage öffnen",
        "dismiss": (
            "Das Entfernen dieser Benachrichtigung quittiert oder setzt keine "
            "Störung an der Anlage zurück."
        ),
        "system": "Anlage {number}",
    },
}


def gateway_notification_key(gateway_id: str) -> str:
    """Use an opaque local key instead of exposing a cloud identifier."""
    return hashlib.sha256(gateway_id.encode()).hexdigest()[:24]


def notification_id(entry_id: str, gateway_id: str) -> str:
    """Keep one notification identity per configured installation."""
    return f"{DOMAIN}_faults_{entry_id}_{gateway_notification_key(gateway_id)}"


def notification_policy(
    options: Mapping[str, Any], gateway_id: str
) -> tuple[bool, int]:
    """Default to enabled; tolerate missing and malformed local options."""
    values = options.get(CONF_FAULT_NOTIFICATIONS)
    value = (
        values.get(gateway_notification_key(gateway_id))
        if isinstance(values, dict)
        else None
    )
    if not isinstance(value, dict):
        return True, 0
    enabled = value.get("enabled")
    reset = value.get("reset", 0)
    return (
        enabled if isinstance(enabled, bool) else True,
        reset
        if isinstance(reset, int) and not isinstance(reset, bool) and reset >= 0
        else 0,
    )


def notification_store(
    hass: HomeAssistant, entry_id: str, gateway_id: str
) -> Store[dict[str, Any]]:
    """Keep only dismissal fingerprints and local option revision in private storage."""
    return Store(hass, 1, notification_id(entry_id, gateway_id), private=True)


def installation_device(
    hass: HomeAssistant, entry_id: str, gateway_id: str
) -> dr.DeviceEntry | None:
    """Find the device within this entry, also on older supported HA versions."""
    return next(
        (
            device
            for device in dr.async_entries_for_config_entry(
                dr.async_get(hass), entry_id
            )
            if (DOMAIN, gateway_id) in device.identifiers
        ),
        None,
    )


def installation_name(
    hass: HomeAssistant, entry_id: str, gateway_id: str, number: int = 1
) -> str:
    """Prefer the user's device name, without falling back to a cloud identifier."""
    device = installation_device(hass, entry_id, gateway_id)
    if device and (name := device.name_by_user or device.name):
        return name
    language = "de" if hass.config.language.lower().startswith("de") else "en"
    return _TEXT[language]["system"].format(number=number)


def _incident_key(fault: ActiveFault) -> str:
    # first_seen_at survives restarts but changes after a confirmed recurrence.
    return hashlib.sha256(
        f"{fault.fingerprint}|{fault.first_seen_at.isoformat()}".encode()
    ).hexdigest()


def _safe_text(value: str) -> str:
    """Render bounded manufacturer text as text rather than Markdown or HTML."""
    value = " ".join(value.split())[:240]
    return re.sub(r"([\\\x60*_{}\[\]()!#|>])", r"\\\1", html.escape(value))


class FaultNotifications:
    """Display confirmed changes without modifying cloud faults or their events."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        coordinator: BoschBuderusDataUpdateCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._tracker = coordinator.faults
        self._gateway_id = coordinator.gateway.gateway_id
        self._id = notification_id(entry.entry_id, self._gateway_id)
        self._store = notification_store(hass, entry.entry_id, self._gateway_id)
        self._dismissed: dict[str, int] = {}
        self._reset = 0
        self._visible = False
        self._last_content: tuple[str, str] | None = None
        self._reported: dict[str, ActiveFault] = {}
        self._resolved: dict[str, FaultLifecycleEvent] = {}
        self._omitted_resolved = 0
        self._remove_callbacks: list[Callable[[], None]] = []
        self._closed = False
        self._dirty = False
        self._update_error_logged = False

    async def async_start(self) -> None:
        """Restore suppression before inspecting already active faults."""
        try:
            stored = await self._store.async_load()
        except Exception:
            _LOGGER.warning("Stored fault notification preferences could not be read")
            stored = None
        if isinstance(stored, dict):
            reset = stored.get("reset")
            if isinstance(reset, int) and not isinstance(reset, bool) and reset >= 0:
                self._reset = reset
            dismissed = stored.get("dismissed")
            if isinstance(dismissed, dict):
                self._dismissed = {
                    key: rank
                    for key, rank in dismissed.items()
                    if isinstance(key, str)
                    and re.fullmatch(r"[0-9a-f]{64}", key)
                    and isinstance(rank, int)
                    and not isinstance(rank, bool)
                    and rank in _RANK.values()
                }
        self._remove_callbacks = [
            pn.async_register_callback(self.hass, self._notification_changed),
            self._tracker.async_add_state_listener(self.async_update),
            self._tracker.async_add_event_observer(self._fault_changed),
        ]
        self.async_update()

    @callback
    def async_update(self) -> None:
        """Apply one complete fault snapshot; never let UI errors break polling."""
        if self._closed:
            return
        try:
            self._update()
        except Exception:
            if not self._update_error_logged:
                _LOGGER.error("The heating fault notification could not be updated")
                self._update_error_logged = True
        else:
            self._update_error_logged = False

    def _update(self) -> None:
        enabled, reset = notification_policy(self.entry.options, self._gateway_id)
        if reset != self._reset:
            self._reset = reset
            self._dismissed.clear()
            self._schedule_save()
        active = self._tracker.active_faults
        current = {_incident_key(fault): _RANK[fault.severity] for fault in active}
        # Only discard obsolete suppression after the tracker has confirmed absence.
        # A retained fault remains in "current" during partial/failed reads.
        if active or self._tracker.current_state_confirmed:
            retained = {
                key: rank for key, rank in self._dismissed.items() if key in current
            }
            if retained != self._dismissed:
                self._dismissed = retained
                self._schedule_save()
        if not enabled:
            self._remove_notification()
            return
        # Keep the current classification of previously shown faults. A change
        # to warning or maintenance alone does not confirm their resolution.
        for fault in self._tracker.active:
            key = _incident_key(fault)
            if key in self._reported:
                self._reported[key] = fault
        if active:
            if not self._visible and not any(
                rank > self._dismissed.get(key, -1) for key, rank in current.items()
            ):
                return
            if not self._visible:
                self._clear_details()
            self._reported.update({_incident_key(fault): fault for fault in active})
            self._show(*self._content(active))
        elif self._visible and (
            self._reported or self._tracker.current_state_confirmed
        ):
            self._show(*self._content(()))

    @callback
    def _fault_changed(self, event: FaultLifecycleEvent) -> None:
        """Retain only confirmed resolutions belonging to the visible notification."""
        if (
            self._closed
            or not self._visible
            or event.event_type is not FaultEventType.RESOLVED
        ):
            return
        key = _incident_key(event.fault)
        if self._reported.pop(key, None) is None:
            return
        self._resolved[key] = event
        if len(self._resolved) > _MAX_DETAILS:
            oldest = min(
                self._resolved,
                key=lambda item: (self._resolved[item].observed_at, item),
            )
            del self._resolved[oldest]
            self._omitted_resolved += 1

    def _time(self, value: datetime, language: str) -> str:
        """Show HA observation times with the configured zone's actual UTC offset."""
        local = value.astimezone(ZoneInfo(self.hass.config.time_zone))
        date_format = "%d.%m.%Y" if language == "de" else "%Y-%m-%d"
        return local.strftime(f"{date_format} %H:%M:%S %z")

    def _detail(
        self, fault: ActiveFault, language: str, resolved_at: datetime | None = None
    ) -> str:
        texts = _TEXT[language]
        details = [
            fault_severity_label(fault.severity, language),
            fault.code or "",
            fault.subcode or "",
            fault_summary(fault, language),
        ]
        line = "- " + ": ".join(_safe_text(part) for part in details if part)
        line += (
            f"  \n  {texts['first_seen']}: {self._time(fault.first_seen_at, language)}"
        )
        if resolved_at is not None:
            line += f"  \n  {texts['resolved_at']}: {self._time(resolved_at, language)}"
        return line

    def _content(self, active: tuple[ActiveFault, ...]) -> tuple[str, str]:
        language = "de" if self.hass.config.language.lower().startswith("de") else "en"
        texts = _TEXT[language]
        configured = self.entry.data.get(CONF_GATEWAY_IDS, [])
        gateways = configured if isinstance(configured, list) else []
        number = (
            gateways.index(self._gateway_id) + 1 if self._gateway_id in gateways else 1
        )
        name = installation_name(
            self.hass, self.entry.entry_id, self._gateway_id, number
        )
        names = [
            installation_name(self.hass, self.entry.entry_id, gateway, index)
            for index, gateway in enumerate(gateways, 1)
            if isinstance(gateway, str)
        ]
        if names.count(name) > 1:
            name = f"{name} ({number})"
        name = " ".join(name.split())[:240]
        changed = tuple(
            fault for fault in self._reported.values() if fault.severity not in _RANK
        )
        if active:
            title = texts["title"].format(name=name)
            lines = [texts["active"].format(count=len(active))]
            if not self._tracker.current_state_confirmed:
                lines += ["", texts["unconfirmed"]]
            ordered = sorted(
                active,
                key=lambda fault: (
                    -_RANK[fault.severity],
                    fault.code or "",
                    fault.fingerprint,
                ),
            )
            for fault in ordered[:_MAX_DETAILS]:
                lines.append(self._detail(fault, language))
            if len(active) > _MAX_DETAILS:
                lines += ["", texts["more"].format(count=len(active) - _MAX_DETAILS)]
        elif changed:
            title = texts["changed_title"].format(name=name)
            lines = []
            if not self._tracker.current_state_confirmed:
                lines.append(texts["unconfirmed"])
        else:
            title = texts["resolved_title"].format(name=name)
            lines = [texts["resolved"]]
        if changed:
            lines += ["", texts["changed"].format(count=len(changed))]
            for fault in sorted(changed, key=_incident_key)[:_MAX_DETAILS]:
                lines.append(self._detail(fault, language))
            if len(changed) > _MAX_DETAILS:
                lines += [
                    "",
                    texts["more_changed"].format(count=len(changed) - _MAX_DETAILS),
                ]
        if self._resolved:
            lines += [
                "",
                texts["resolved_details"].format(
                    count=len(self._resolved) + self._omitted_resolved
                ),
            ]
            for event in sorted(
                self._resolved.values(),
                key=lambda item: (item.observed_at, _incident_key(item.fault)),
                reverse=True,
            ):
                lines.append(self._detail(event.fault, language, event.observed_at))
            if self._omitted_resolved:
                lines += [
                    "",
                    texts["more_resolved"].format(count=self._omitted_resolved),
                ]
        lines += [
            "",
            texts["observation_times"].format(zone=self.hass.config.time_zone),
        ]
        if active or changed:
            lines += ["", texts["dismiss"]]
        device = installation_device(self.hass, self.entry.entry_id, self._gateway_id)
        if device:
            lines += ["", f"[{texts['open']}](/config/devices/device/{device.id})"]
        return title, "\n".join(lines)

    def _show(self, title: str, message: str) -> None:
        content = (title, message)
        if self._visible and self._last_content == content:
            return
        pn.async_create(self.hass, message, title, self._id)
        self._visible = True
        self._last_content = content

    @callback
    def _notification_changed(
        self, update_type: pn.UpdateType, notifications: dict[str, pn.Notification]
    ) -> None:
        if (
            self._closed
            or update_type is not pn.UpdateType.REMOVED
            or self._id not in notifications
        ):
            return
        if self._visible:
            self._dismissed = {
                _incident_key(fault): _RANK[fault.severity]
                for fault in self._tracker.active_faults
            }
            self._schedule_save()
        self._visible = False
        self._last_content = None
        self._clear_details()

    def _clear_details(self) -> None:
        self._reported.clear()
        self._resolved.clear()
        self._omitted_resolved = 0

    def _remove_notification(self) -> None:
        # Our own cleanup is not a user dismissal.
        self._visible = False
        self._last_content = None
        self._clear_details()
        pn.async_dismiss(self.hass, self._id)

    def _schedule_save(self) -> None:
        self._dirty = True
        self._store.async_delay_save(self._serialize, 1)

    def _serialize(self) -> dict[str, Any]:
        return {"dismissed": dict(self._dismissed), "reset": self._reset}

    async def async_close(self) -> None:
        """Flush a just-dismissed notification before an integration reload."""
        self.stop()
        try:
            await self._tracker.async_flush()
            if self._dirty:
                await self._store.async_save(self._serialize())
        except Exception:
            _LOGGER.warning("Fault notification preferences could not be saved")

    @callback
    def stop(self) -> None:
        """Disconnect observers and remove the locally owned notification."""
        self._closed = True
        for remove in self._remove_callbacks:
            remove()
        self._remove_callbacks.clear()
        try:
            self._remove_notification()
        except Exception:
            _LOGGER.warning("The heating fault notification could not be removed")


async def async_setup_notifications(
    hass: HomeAssistant, entry: ConfigEntry[Any]
) -> tuple[FaultNotifications, ...]:
    """Keep notification setup failures independent of heating entities."""
    managers: list[FaultNotifications] = []
    for coordinator in entry.runtime_data.coordinators:
        manager = FaultNotifications(hass, entry, coordinator)
        try:
            await manager.async_start()
        except Exception:
            manager.stop()
            _LOGGER.error("Heating fault notifications could not be initialized")
        else:
            managers.append(manager)
            entry.async_on_unload(manager.stop)
    return tuple(managers)


async def async_update_notification_options(
    hass: HomeAssistant, entry: ConfigEntry[Any]
) -> None:
    """Apply options locally, without reloading or waking the cloud coordinator."""
    for manager in entry.runtime_data.notifications:
        manager.async_update()
