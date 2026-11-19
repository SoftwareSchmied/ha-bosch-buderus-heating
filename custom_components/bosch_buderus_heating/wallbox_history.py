"""Opt-in wallbox history: the last charging session and monthly totals.

Off by default (option ``wallbox_history`` of the wallbox settings). While it
is off nothing is requested and nothing is kept. While it is on, the charge
log and the energy history are read every 30 minutes (60 minutes in the
cloud-friendly profile), one request each.

Privacy: only an allowlist is read from the answers. Of a session that are
start, end, charging duration and energy; of the energy history the grid and
solar energy of the month. Authentication names, card and user data, meter
serial numbers, session identifiers and costs are never read, kept, logged or
put into the diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from math import isfinite
from time import monotonic
from typing import TYPE_CHECKING, Final, Literal

from .pointt import PointTClient, PointTError
from .pointt.exceptions import ProtocolError, RateLimited, ResourceError

if TYPE_CHECKING:
    from collections.abc import Mapping

type Kind = Literal["chargelog", "energyhistory"]
KINDS: Final[tuple[Kind, ...]] = ("chargelog", "energyhistory")
OPTION_WALLBOX_HISTORY: Final = "wallbox_history"
# The charge log covers this many days; the last session is taken from it.
SESSION_WINDOW_DAYS: Final = 35
# Entries read from one answer, at most.
MAX_SESSIONS: Final = 2000
MAX_SAMPLES: Final = 2000
_MAX_SECONDS: Final = 366 * 86400
# A session may end this much after the local clock (clock differences).
_FUTURE_TOLERANCE: Final = timedelta(minutes=10)
_ENERGY_UNITS: Final = {"kwh": 1.0, "wh": 0.001}
_SIGNALS: Final = {"gridEnergy": "grid_kwh", "solarEnergy": "solar_kwh"}


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """The newest finished session: four values, nothing that names a person."""

    start: datetime
    end: datetime
    duration_seconds: int | None
    energy_kwh: float | None


@dataclass(frozen=True, slots=True)
class MonthTotals:
    """Energy of the current month by source, ``None`` when not reported."""

    grid_kwh: float | None
    solar_kwh: float | None


def history_enabled(options: Mapping[str, object] | None) -> bool:
    return options is not None and options.get(OPTION_WALLBOX_HISTORY) is True


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if isfinite(number) and number >= 0 else None


def _timestamp(value: object, local: tzinfo | None = None) -> datetime | None:
    """A time with an offset as given; one without is local time of ``local``."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed
    return parsed.replace(tzinfo=local) if local is not None else None


def _session_energy(item: dict[object, object]) -> float | None:
    """Energy of a session in kWh; kWh when the entry names no unit.

    An entry that names a unit (``unitOfMeasure``) is converted from Wh or
    kWh; any other unit leaves the value unknown.
    """
    number = _number(item.get("energy"))
    unit = item.get("unitOfMeasure")
    if number is None or unit is None:
        return number
    factor = (
        _ENERGY_UNITS.get(unit.strip().casefold()) if isinstance(unit, str) else None
    )
    return number * factor if factor is not None else None


def parse_chargelog(
    payload: object, now: datetime | None = None
) -> SessionSummary | None:
    """The newest finished session of a charge log answer.

    ``None``: the log holds no usable finished session (a valid answer).
    Anything but an object with a ``values`` list is a protocol error.
    With ``now`` (local time of Home Assistant) a time without an offset is
    taken as that local time, as the query is sent, and a session ending
    after ``now`` (a wrong clock of the wallbox) is not a finished session.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("values"), list):
        raise ProtocolError("Invalid wallbox charge log")
    best: SessionSummary | None = None
    for item in payload["values"][:MAX_SESSIONS]:
        if not isinstance(item, dict):
            continue
        local = now.tzinfo if now is not None else None
        start = _timestamp(item.get("begin"), local)
        end = _timestamp(item.get("end"), local)
        if start is None or end is None or end < start:
            continue
        if now is not None and end > now + _FUTURE_TOLERANCE:
            continue
        if best is not None and end <= best.end:
            continue
        duration = item.get("chargingDuration")
        seconds = (
            duration
            if type(duration) is int and 0 <= duration <= _MAX_SECONDS
            else None
        )
        best = SessionSummary(start, end, seconds, _session_energy(item))
    return best


def _total(recording: object) -> float | None:
    if not isinstance(recording, dict):
        return None
    factor = None
    unit = recording.get("unitOfMeasure")
    if isinstance(unit, str):
        factor = _ENERGY_UNITS.get(unit.strip().casefold())
    samples = recording.get("recording")
    if factor is None or not isinstance(samples, list):
        return None
    parts = [
        number
        for item in samples[:MAX_SAMPLES]
        if isinstance(item, dict) and (number := _number(item.get("y"))) is not None
    ]
    return sum(parts) * factor if parts else None


def parse_energyhistory(payload: object) -> MonthTotals | None:
    """Grid and solar energy of an energy history answer.

    ``None``: neither signal is reported in a known unit (a valid answer).
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("values"), list):
        raise ProtocolError("Invalid wallbox energy history")
    found: dict[str, float | None] = {"grid_kwh": None, "solar_kwh": None}
    for recording in payload["values"]:
        signal = recording.get("signal") if isinstance(recording, dict) else None
        if isinstance(signal, str) and signal in _SIGNALS:
            found[_SIGNALS[signal]] = _total(recording)
    if found["grid_kwh"] is None and found["solar_kwh"] is None:
        return None
    return MonthTotals(found["grid_kwh"], found["solar_kwh"])


@dataclass(slots=True)
class WallboxHistory:
    """What the history option has read; freshness and counters only."""

    interval: int
    session: SessionSummary | None = None
    totals: MonthTotals | None = None
    received: dict[Kind, float] = field(default_factory=dict)
    # 403/404/406 are not asked again until new software; other failures
    # are tried again at the next interval.
    errors: dict[Kind, str] = field(default_factory=dict)
    sticky: set[Kind] = field(default_factory=set)
    requests: int = 0
    active: bool = False
    next_due: float = 0

    def fresh(self, kind: Kind) -> bool:
        stamp = self.received.get(kind)
        return (
            stamp is not None
            and kind not in self.errors
            and monotonic() - stamp <= 2 * self.interval
        )

    def reset(self) -> None:
        """The option is off: forget everything."""
        self.session = None
        self.totals = None
        self.received.clear()
        self.errors.clear()
        self.sticky.clear()
        self.active = False
        self.next_due = 0

    def new_software(self) -> None:
        """New firmware may bring an answer an older one refused."""
        for kind in self.sticky:
            self.errors.pop(kind, None)
        self.sticky.clear()
        self.next_due = 0

    async def read_due(
        self,
        client: PointTClient,
        gateway_id: str,
        *,
        enabled: bool,
        local_now: datetime,
        tolerance: float = 0,
    ) -> None:
        """Read what is due. With the option off nothing is requested."""
        if not enabled:
            if self.active:
                self.reset()
            return
        self.active = True
        if monotonic() + tolerance < self.next_due:
            return
        for kind in KINDS:
            if kind in self.sticky:
                continue
            try:
                await self._read(client, gateway_id, kind, local_now)
            except ResourceError as err:
                self._fail(kind, f"http_{err.status}", clear=True)
                if err.status in (403, 404, 406):
                    self.sticky.add(kind)
            except ProtocolError:
                self._fail(kind, "invalid_payload", clear=True)
            except PointTError as err:
                # A transport or account problem ends this cycle; readings
                # already held expire by themselves.
                self._fail(
                    kind,
                    "rate_limited"
                    if isinstance(err, RateLimited)
                    else "request_failed",
                    clear=False,
                )
                break
        self.next_due = monotonic() + self.interval

    async def _read(
        self, client: PointTClient, gateway_id: str, kind: Kind, local_now: datetime
    ) -> None:
        self.requests += 1
        if kind == "chargelog":
            payload = await client.get_wallbox_history(
                gateway_id,
                kind,
                local_now - timedelta(days=SESSION_WINDOW_DAYS),
                local_now,
            )
            self.session = parse_chargelog(payload, local_now)
        else:
            month = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            payload = await client.get_wallbox_history(
                gateway_id, kind, month, local_now
            )
            self.totals = parse_energyhistory(payload)
        self.received[kind] = monotonic()
        self.errors.pop(kind, None)

    def _fail(self, kind: Kind, category: str, *, clear: bool) -> None:
        self.errors[kind] = category
        if clear:
            self.received.pop(kind, None)
            if kind == "chargelog":
                self.session = None
            else:
                self.totals = None

    def diagnostics(self) -> dict[str, object]:
        """State and counters only; no timestamps of sessions, no values."""
        return {
            "enabled": self.active,
            "requests": self.requests,
            "interval_seconds": self.interval,
            "parts": {
                kind: {
                    "fresh": self.fresh(kind),
                    "error": self.errors.get(kind),
                    "has_data": (
                        self.session is not None
                        if kind == "chargelog"
                        else self.totals is not None
                    ),
                }
                for kind in KINDS
            },
        }
