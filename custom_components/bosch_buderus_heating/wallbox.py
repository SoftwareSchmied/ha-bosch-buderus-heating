"""PointT wallbox adapter for the cp0 objects and single values.

The three objects ``telemetry``, ``conf`` and ``info`` are polled as before.
Single values (``conf/locked`` ...) are read only once the control
platforms ask for them: once to learn whether they exist and are writeable,
and again in the configuration interval while an enabled entity shows
them. Writes and commands live in ``wallbox_control``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import isfinite
from re import compile as re_compile
from time import monotonic
from typing import TYPE_CHECKING, Final, Literal

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .configuration_access import EntryConfiguration
from .const import DOMAIN, PollingProfile
from .coordinator import POLL_DUE_TOLERANCE_SECONDS
from .pointt import AuthenticationError, Gateway, PointTClient, PointTError
from .pointt.exceptions import ProtocolError, RateLimited, ResourceError
from .wallbox_history import WallboxHistory, history_enabled
from .wallbox_leaves import (
    LEAVES,
    Firmware,
    Leaf,
    firmware_reaches,
    firmware_version,
    parse_leaf,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import BoschBuderusConfigEntry

type Section = Literal["telemetry", "conf", "info"]
type Reading = str | float | int | datetime | None
SECTIONS: tuple[Section, ...] = ("telemetry", "conf", "info")
STATES = {
    "Init": "initializing",
    "Available": "available",
    "Authenticated": "authenticated",
    "Preparing": "preparing",
    "Charging": "charging",
    "SuspendedEV": "suspended_ev",
    "SuspendedEVSE": "suspended_evse",
    "Faulted": "faulted",
    "Locked": "locked",
    "Limited": "limited",
    "PhaseSwitch": "phase_switch",
}
# The limiting source the wallbox reports, as HA state keys.
LIMITERS: Final = {
    "none": "none",
    "nocar": "no_car",
    "derating": "derating",
    "key": "key",
    "paragraph14a": "paragraph_14a",
    "app": "app",
    "web": "web",
    "emma": "energy_manager",
    "modbustcp": "modbus_tcp",
    "ocpp": "ocpp",
    "loadbalancer": "load_balancer",
    "watchdog": "watchdog",
    "pv": "solar",
    "dynLmm": "dynamic_load_management",
    "staticLmm": "static_load_management",
    "powermeterFailSafe": "meter_fail_safe",
    "extlimit": "external_limit",
}
ENERGY_MANAGER_STATES: Final = {
    "inactive": "inactive",
    "activeCh": "active_heating",
    "activeDhw": "active_hot_water",
    "activeEm": "active_energy_manager",
}
# At most this many single values are read in one polling cycle.
MAX_LEAF_READS: Final = 4
# The fields the object parsers read; every other key of an answer is only
# counted by name (diagnostics), never by value.
KNOWN_FIELDS: Final[dict[str, frozenset[str]]] = {
    "telemetry": frozenset(
        {
            "meter",
            "wbState",
            "activeChargingTime",
            "limit",
            "chargingStart",
            "limiter",
            "energymanager",
            "emStatus",
        }
    ),
    "conf": frozenset({"wallboxMode", "chargingStrategy", "locked", "auth", "label"}),
    "info": frozenset(
        {
            "hwCurrentLimit",
            "productName",
            "softwareVersion",
            "paragraph14aConfigured",
            "paragraph14aActive",
            "relaisAvailable",
        }
    ),
}
# A field name is kept for the diagnostics only as a short plain token.
_SAFE_NAME: Final = re_compile(r"[A-Za-z][A-Za-z0-9_]{0,47}")
MAX_UNKNOWN_NAMES: Final = 64
_LOGGER = logging.getLogger(__name__)


def is_wallbox(gateway: Gateway) -> bool:
    """Use the reported transport family, never a retail-name substring."""
    return (gateway.device_type or "").lower() in {"commodule", "commodule2"}


def _number(value: object) -> float | None:
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if isfinite(number) and number >= 0 else None


def _text(value: object) -> str | None:
    if (
        isinstance(value, str)
        and 0 < len(value.strip()) <= 128
        and all(character.isprintable() for character in value)
    ):
        return value.strip()
    return None


def _on_off(value: object) -> str | None:
    text = value.strip().lower() if isinstance(value, str) else None
    return text if text in ("on", "off") else None


def _token(value: object) -> str | None:
    """A short printable token kept for the control rules, never displayed."""
    if (
        isinstance(value, str)
        and 0 < len(value) <= 64
        and all(character.isprintable() for character in value)
    ):
        return value
    return None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True, slots=True)
class SectionSnapshot:
    """Retain only interpreted, allowlisted fields from one response."""

    values: dict[str, Reading] = field(default_factory=dict)
    received: float = 0
    error: str | None = None
    # Names (never values) of keys the parser does not read, and how many
    # keys had no plain-token name or exceeded the limit.
    unknown_names: tuple[str, ...] = ()
    unnamed_count: int = 0


def unknown_field_names(
    section: Section, raw: dict[object, object]
) -> tuple[tuple[str, ...], int]:
    """The sorted safe names of keys no parser reads, and the rest's count."""
    known = KNOWN_FIELDS[section]
    names: set[str] = set()
    unnamed = 0
    for key in raw:
        if isinstance(key, str) and key in known:
            continue
        if isinstance(key, str) and _SAFE_NAME.fullmatch(key):
            names.add(key)
        else:
            unnamed += 1
    ordered = sorted(names)
    unnamed += max(0, len(ordered) - MAX_UNKNOWN_NAMES)
    return tuple(ordered[:MAX_UNKNOWN_NAMES]), unnamed


def parse_section(section: Section, payload: object) -> SectionSnapshot:
    """Parse object envelopes without defaulting missing or invalid data to zero."""
    if not isinstance(payload, dict) or not isinstance(payload.get("values"), dict):
        raise ProtocolError("Invalid wallbox object envelope")
    raw = payload["values"]
    values: dict[str, Reading]
    if section == "telemetry":
        meter = raw.get("meter")
        parts = (
            meter.split(";") if isinstance(meter, str) and len(meter) <= 1024 else []
        )
        energy = _number(parts[0]) if len(parts) >= 2 else None
        power = _number(parts[-1]) if len(parts) >= 2 else None
        state = raw.get("wbState")
        duration = raw.get("activeChargingTime")
        values = {
            "state": STATES.get(state) if isinstance(state, str) else None,
            "energy": energy / 1000 if energy is not None else None,
            "power": power,
            "duration": duration
            if type(duration) is int and 0 <= duration <= 2**63 - 1
            else None,
            "current_limit": _number(raw.get("limit")),
            "charging_start": _timestamp(raw.get("chargingStart")),
            # The raw tokens below decide whether a command is allowed.
            "wb_state": state if isinstance(state, str) and state in STATES else None,
            "limiter_token": _token(raw.get("limiter")),
            "limiter": LIMITERS.get(limiter)
            if isinstance(limiter := raw.get("limiter"), str)
            else None,
            "energy_manager_token": _token(raw.get("energymanager")),
            "energy_manager": ENERGY_MANAGER_STATES.get(em_status)
            if isinstance(em_status := raw.get("emStatus"), str)
            else None,
        }
    elif section == "conf":
        mode = raw.get("wallboxMode")
        strategy = raw.get("chargingStrategy")
        values = {
            "role": mode if mode in ("standalone", "leader", "follower") else None,
            "charging_strategy": strategy
            if strategy in ("default", "solar-eco")
            else None,
            "locked": _on_off(raw.get("locked")),
            "authentication": _on_off(raw.get("auth")),
            # Kept for the display-name entity only; never in diagnostics.
            "label": _text(raw.get("label")),
        }
    else:
        values = {
            "hardware_current_limit": _number(raw.get("hwCurrentLimit")),
            "product": _text(raw.get("productName")),
            "software": _text(raw.get("softwareVersion")),
            "grid_friendly_configured": _on_off(raw.get("paragraph14aConfigured")),
            "grid_friendly_active": _on_off(raw.get("paragraph14aActive")),
            "relays_available": _on_off(raw.get("relaisAvailable")),
        }
    unknown, unnamed = unknown_field_names(section, raw)
    return SectionSnapshot(
        values, monotonic(), unknown_names=unknown, unnamed_count=unnamed
    )


class WallboxCoordinator(DataUpdateCoordinator[dict[Section, SectionSnapshot]]):
    """Poll a selected wallbox without running generic heating discovery."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PointTClient,
        gateway: Gateway,
        entry: BoschBuderusConfigEntry,
        *,
        connection_id: str | None = None,
    ) -> None:
        self.connection_id = connection_id
        friendly = (
            EntryConfiguration(entry, connection_id).polling_profile()
            is PollingProfile.CLOUD_FRIENDLY
        )
        self.fast_interval = 120 if friendly else 60
        self.intervals: dict[Section, int] = {
            "telemetry": self.fast_interval,
            "conf": 600 if friendly else 300,
            "info": 1800 if friendly else 900,
        }
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} wallbox",
            update_interval=timedelta(seconds=self.fast_interval),
            config_entry=entry,
        )
        self.client = client
        self.gateway = gateway
        self.data = {}
        self._next: dict[Section, float] = {}
        self._poll_lock = asyncio.Lock()
        # Single values: read once the control platforms ask (``want_leaves``).
        self.leaves_wanted = False
        self.leaves: dict[str, Leaf] = {}
        # 403/404/406 or an unusable answer: not read again until the
        # firmware changes or the integration starts again.
        self.leaf_errors: dict[str, str] = {}
        self._leaf_next: dict[str, float] = {}
        self._leaf_demand: dict[str, int] = {}
        self._firmware_text: str | None = None
        # Opt-in history (the wallbox option); nothing is read while it is off.
        self.history = WallboxHistory(interval=1800 if not friendly else 3600)
        # Writes: the last command (5-second spacing) and counters without
        # values for the diagnostics.
        self.last_command: float | None = None
        self.write_counts: dict[str, int] = {}

    def reading(self, section: Section, key: str) -> Reading:
        """Expire values even while a later request is still pending."""
        return (
            self.data[section].values.get(key) if self.section_fresh(section) else None
        )

    def section_fresh(self, section: Section) -> bool:
        snapshot = self.data.get(section)
        return bool(
            self.last_update_success
            and snapshot is not None
            and snapshot.error is None
            and monotonic() - snapshot.received <= 2 * self.intervals[section]
        )

    async def _async_update_data(self) -> dict[Section, SectionSnapshot]:
        async with self._poll_lock:
            return await self._read_due_sections()

    async def _read_due_sections(self) -> dict[Section, SectionSnapshot]:
        snapshots = dict(self.data)
        for section in SECTIONS:
            # HA may fire just before the exact section deadline; the shared
            # tolerance keeps the cadence without touching error or rate-limit
            # pauses, which the PointT transport enforces before every request.
            if monotonic() + POLL_DUE_TOLERANCE_SECONDS < self._next.get(section, 0):
                continue
            try:
                payload = await self.client.get_wallbox_section(
                    self.gateway.gateway_id, section
                )
                snapshots[section] = parse_section(section, payload)
            except (ResourceError, ProtocolError) as err:
                category = (
                    f"http_{err.status}"
                    if isinstance(err, ResourceError)
                    else "invalid_payload"
                )
                snapshots[section] = SectionSnapshot(error=category)
            except PointTError as err:
                category = (
                    "authentication"
                    if isinstance(err, AuthenticationError)
                    else "request_failed"
                )
                if isinstance(err, RateLimited):
                    category = "rate_limited"
                self.data = {part: SectionSnapshot(error=category) for part in SECTIONS}
                self._next.clear()
                if isinstance(err, AuthenticationError):
                    raise ConfigEntryAuthFailed(
                        "Wallbox authentication failed"
                    ) from err
                raise UpdateFailed("Wallbox request failed") from err
            self._next[section] = monotonic() + self.intervals[section]
        info = snapshots.get("info")
        if info is not None and info.error is None:
            self._note_firmware(info.values.get("software"))
        await self._read_due_leaves()
        await self._read_history()
        return snapshots

    async def _read_history(self) -> None:
        entry = self.config_entry
        await self.history.read_due(
            self.client,
            self.gateway.gateway_id,
            enabled=history_enabled(entry.options if entry is not None else None),
            local_now=dt_util.now(),
            tolerance=POLL_DUE_TOLERANCE_SECONDS,
        )

    # Single values ----------------------------------------------------------

    def want_leaves(self) -> None:
        """The control platforms are set up: learn which values exist."""
        self.leaves_wanted = True

    def firmware(self) -> Firmware | None:
        """The last reported software version, as a comparable triple."""
        return firmware_version(self._firmware_text)

    def _note_firmware(self, software: object) -> None:
        text = software if isinstance(software, str) else None
        if text is not None and text != self._firmware_text:
            if self._firmware_text is not None:
                # New firmware may bring values an older one refused.
                self.leaf_errors.clear()
                self.history.new_software()
            self._firmware_text = text

    def leaf(self, path: str) -> Leaf | None:
        """The last answer of a single value, however old (its capability)."""
        return self.leaves.get(path)

    def leaf_fresh(self, path: str) -> bool:
        leaf = self.leaves.get(path)
        return bool(
            self.last_update_success
            and leaf is not None
            and path not in self.leaf_errors
            and monotonic() - leaf.received <= 2 * self.intervals["conf"]
        )

    def leaf_value(self, path: str) -> object:
        """A single value while it is current, else None."""
        leaf = self.leaves.get(path)
        return leaf.value if leaf is not None and self.leaf_fresh(path) else None

    def demand_leaf(self, path: str) -> Callable[[], None]:
        """An enabled entity shows this value: poll it in the configuration
        interval until the returned function is called."""
        self._leaf_demand[path] = self._leaf_demand.get(path, 0) + 1
        if not self.leaf_fresh(path):
            self._leaf_next[path] = 0

        def release() -> None:
            remaining = self._leaf_demand.get(path, 0) - 1
            if remaining > 0:
                self._leaf_demand[path] = remaining
            else:
                self._leaf_demand.pop(path, None)

        return release

    def _due_leaves(self) -> list[str]:
        now = monotonic() + POLL_DUE_TOLERANCE_SECONDS
        firmware = self.firmware()
        polled = sorted(
            (
                path
                for path in self._leaf_demand
                if path in LEAVES
                and path not in self.leaf_errors
                and self._leaf_next.get(path, 0) <= now
            ),
            key=lambda path: self._leaf_next.get(path, 0),
        )
        unknown = [
            path
            for path, spec in LEAVES.items()
            if self.leaves_wanted
            and path not in self.leaves
            and path not in self.leaf_errors
            and path not in polled
            and firmware is not None
            and firmware_reaches(firmware, spec.firmware)
        ]
        return [*polled, *unknown][:MAX_LEAF_READS]

    async def _read_due_leaves(self) -> None:
        getter = getattr(self.client, "get_wallbox_leaf", None)
        if getter is None:
            return
        for path in self._due_leaves():
            try:
                payload = await getter(self.gateway.gateway_id, path)
                self.leaves[path] = parse_leaf(LEAVES[path], payload, monotonic())
                self.leaf_errors.pop(path, None)
            except ResourceError as err:
                self.leaf_errors[path] = f"http_{err.status}"
            except ProtocolError:
                self.leaf_errors[path] = "invalid_payload"
            except PointTError:
                # A transport or account problem ends the single reads of
                # this cycle; the objects already read stay valid.
                return
            self._leaf_next[path] = monotonic() + self.intervals["conf"]

    def store_leaf(self, path: str, leaf: Leaf) -> None:
        """A fresh reading of a write transaction."""
        self.leaves[path] = leaf
        self.leaf_errors.pop(path, None)
        self._leaf_next[path] = monotonic() + self.intervals["conf"]

    def store_section(self, section: Section, snapshot: SectionSnapshot) -> None:
        """A fresh object read by a write transaction."""
        self.data = {**self.data, section: snapshot}
        if section == "info":
            self._note_firmware(snapshot.values.get("software"))

    # Controls ---------------------------------------------------------------

    def control_status(self) -> tuple[str | None, datetime | None]:
        """Why the cloud cannot take a command now (``cloud_down_error``)."""
        if isinstance(self.last_exception, ConfigEntryAuthFailed):
            return "auth_required", None
        shared = getattr(getattr(self.client, "backoff", None), "remaining", 0.0)
        if isinstance(shared, int | float) and shared > 0:
            return "rate_limited", None
        if not self.last_update_success:
            return "cloud_unreachable", None
        return None, None

    @property
    def poll_lock(self) -> asyncio.Lock:
        """Held by every poll and every write: a write never runs while the
        wallbox is being read."""
        return self._poll_lock

    def diagnostics(self) -> dict[str, object]:
        """Describe shape and freshness without identifiers, raw values or names."""
        return {
            "profile": "pointt_wallbox_cp0",
            "write_support": True,
            # Single values by state, never their contents, and write
            # outcomes as counters only.
            "values": {
                "known": sorted(self.leaves),
                "writeable": sorted(
                    path for path, leaf in self.leaves.items() if leaf.writeable
                ),
                "refused": dict(sorted(self.leaf_errors.items())),
                "polled": sorted(self._leaf_demand),
            },
            "firmware_known": self.firmware() is not None,
            "write_counts": dict(sorted(self.write_counts.items())),
            "history": self.history.diagnostics(),
            "sections": {
                section: {
                    "error": snapshot.error,
                    "valid_field_count": sum(
                        value is not None for value in snapshot.values.values()
                    ),
                    "invalid_field_count": sum(
                        value is None for value in snapshot.values.values()
                    ),
                    "fresh": self.section_fresh(section),
                    "interval_seconds": self.intervals[section],
                    # Names only (no values) of keys no parser reads.
                    "unknown_field_names": list(snapshot.unknown_names),
                    "unnamed_field_count": snapshot.unnamed_count,
                }
                for section, snapshot in self.data.items()
            },
        }
