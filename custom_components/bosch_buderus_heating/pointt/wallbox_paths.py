"""The only cp0 paths of a wallbox the client reads, sets or commands.

Every path is fixed here; nothing is put together from user input. Access
control, RFID cards, the charge log and the energy history are not part of
any list and can therefore never be requested.
"""

from __future__ import annotations

from typing import Final

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
