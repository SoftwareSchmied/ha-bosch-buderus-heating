"""Read-only PointT wallbox adapter for the documented cp0 object endpoints."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import isfinite
from time import monotonic
from typing import TYPE_CHECKING, Literal

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .configuration_access import EntryConfiguration
from .const import DOMAIN, PollingProfile
from .coordinator import POLL_DUE_TOLERANCE_SECONDS
from .pointt import AuthenticationError, Gateway, PointTClient, PointTError
from .pointt.exceptions import ProtocolError, RateLimited, ResourceError

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
        }
    elif section == "conf":
        mode = raw.get("wallboxMode")
        values = {
            "role": mode if mode in ("standalone", "leader", "follower") else None
        }
    else:
        values = {
            "hardware_current_limit": _number(raw.get("hwCurrentLimit")),
            "product": _text(raw.get("productName")),
            "software": _text(raw.get("softwareVersion")),
        }
    return SectionSnapshot(values, monotonic())


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
        return snapshots

    def diagnostics(self) -> dict[str, object]:
        """Describe shape and freshness without identifiers, raw values or names."""
        return {
            "profile": "pointt_wallbox_cp0",
            "write_support": False,
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
                }
                for section, snapshot in self.data.items()
            },
        }
