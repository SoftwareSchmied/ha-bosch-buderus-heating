"""The only cp0 paths of a wallbox the client reads, sets or commands.

Every path is fixed here; nothing is put together from user input. Access
control and RFID cards are not part of any list and can therefore never be
requested. The charge log and the energy history are not single values
either: only the two dedicated queries below can reach them, and only the
history option of the wallbox enables them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from urllib.parse import urlencode

from .models import JsonValue

# Single values read below ``/rest/v1/cp0/``.
READ_LEAVES: Final = frozenset(
    {
        "telemetry/extmeterState",
        "telemetry/forceActive",
        "telemetry/extInputState",
        "info/extmeterCurrentMaximum",
        "info/brand",
        "conf/chargingStrategy",
        "conf/locked",
        "conf/label",
        "conf/currency",
        "conf/price",
        "conf/feedinPrice",
        "conf/solarSurplus/supplementEnable",
        "conf/solarSurplus/supplementLimit",
        "conf/solarSurplus/supplementThreshold",
        "conf/strategy/powerOffset",
        "conf/strategy/filterTime",
        "conf/overloadProtection/currentLimiter",
        "conf/overloadProtection/currentLimit",
        "conf/overloadProtection/powerLimiter",
        "conf/overloadProtection/powerLimit",
    }
)

# Settings written with ``{"value": ...}``. Never ``conf/auth``, ``conf/rfid``,
# ``conf/rfid/secure`` or ``conf/currency``.
SETTING_LEAVES: Final = frozenset(
    {
        "conf/chargingStrategy",
        "conf/locked",
        "conf/label",
        "conf/price",
        "conf/feedinPrice",
        "conf/solarSurplus/supplementEnable",
        "conf/solarSurplus/supplementLimit",
        "conf/solarSurplus/supplementThreshold",
        "conf/strategy/powerOffset",
        "conf/strategy/filterTime",
        "conf/overloadProtection/currentLimiter",
        "conf/overloadProtection/currentLimit",
        "conf/overloadProtection/powerLimiter",
        "conf/overloadProtection/powerLimit",
    }
)

# Commands below ``cmd/`` and the body each one takes. Never ``rfid/teachIn``.
COMMANDS: Final = frozenset(
    {"start", "pause", "authenticate", "limit", "force/limit", "force/limitReset"}
)


def command_body_valid(command: str, body: JsonValue) -> bool:
    """Whether ``body`` has exactly the shape of ``command``."""
    if command not in COMMANDS or not isinstance(body, dict):
        return False
    if command in ("start", "pause", "authenticate"):
        name = body.get("name")
        return (
            body.keys() == {"name"}
            and isinstance(name, str)
            and len(name) <= 64
            and all(character.isprintable() for character in name)
        )
    if command in ("limit", "force/limit"):
        limit = body.get("limit")
        return body.keys() == {"limit"} and type(limit) is int and 6 <= limit <= 63
    return body == {}


def setting_value_valid(leaf: str, value: JsonValue) -> bool:
    """Whether ``value`` is a plain value the setting can take at all."""
    if leaf not in SETTING_LEAVES or isinstance(value, bool):
        return False
    if leaf == "conf/overloadProtection/currentLimit":
        return (
            isinstance(value, list)
            and len(value) == 3
            and all(type(item) is float for item in value)
        )
    return isinstance(value, str | int | float)


# History (opt-in): the charge log and the energy history. Their queries are
# built only from timestamps and the fixed values below.
HISTORY_SIGNALS: Final = ("gridEnergy", "solarEnergy")
HISTORY_SAMPLE_RATE: Final = "P31D"


def _local_time(value: datetime) -> str:
    """The app sends local date and time without an offset."""
    return value.replace(microsecond=0, tzinfo=None).isoformat()


def chargelog_query(after: datetime, before: datetime) -> str:
    """The query string of a charge log request for one period."""
    return urlencode(
        {"filter_after": _local_time(after), "filter_before": _local_time(before)}
    )


def energyhistory_query(start: datetime, end: datetime) -> str:
    """The query string of an energy history request (one sample for the
    period, grid and solar energy only; never a cost signal)."""
    return urlencode(
        {
            "filter_start": _local_time(start),
            "filter_end": _local_time(end),
            "sample_rate": HISTORY_SAMPLE_RATE,
            "filter_signals": ",".join(HISTORY_SIGNALS),
        }
    )
