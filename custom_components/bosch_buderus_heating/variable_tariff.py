"""The variable electricity tariff of an iCom gateway, read only (0.9.2).

An iCom gateway reports whether its tariff optimisation is on
(``/application/tbso/enable``, ``on``/``off``) and the number of the chosen
tariff (``/application/tbso/electricity/tariffID``). The manufacturer's app
takes the tariff's name from a separate cloud service of the manufacturer;
this integration never asks that service, so only the number is shown. The
number 5 is the value the app writes when it switches the tariff off: it
means "no tariff chosen".

Both paths are probed once during discovery, only on an iCom gateway, and
read with the static group afterwards. A refusal, a missing path or a
server error counts as "not there" and changes nothing else. Nothing is
ever written: setting a tariff in the app takes several requests and
changes the heating circuits as well.

The tariff values of a K30/K40 (``/system/variableTariff/*``) are read
since 0.9.0 and stay as they are. Expected, untested on a real
installation.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Final

from .pointt import Resource

TARIFF_ENABLE_PATH: Final = "/application/tbso/enable"
TARIFF_ID_PATH: Final = "/application/tbso/electricity/tariffID"
# Probed once during discovery, only on an iCom gateway.
ICOM_TARIFF_PATHS: Final = (TARIFF_ENABLE_PATH, TARIFF_ID_PATH)
# The tariff number the app writes when it switches the tariff off.
NO_TARIFF_ID: Final = 5


def tariff_enabled(resource: Resource | None) -> bool | None:
    """``on``/``off`` as a state; anything else is unknown."""
    if resource is None or not resource.has_value:
        return None
    return {"on": True, "off": False}.get(
        resource.value if isinstance(resource.value, str) else ""
    )


def tariff_id(resource: Resource | None) -> int | None:
    """A whole, non-negative tariff number; anything else is unknown."""
    if resource is None or not resource.has_value:
        return None
    value = resource.value
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
        or not float(value).is_integer()
    ):
        return None
    return int(value)


def diagnostics(
    resources: Mapping[str, Resource], probed: Iterable[str]
) -> dict[str, str]:
    """Per tariff path only whether it is there and its type; no value."""
    probed_paths = set(probed)
    result: dict[str, str] = {}
    for key, path in (("enable", TARIFF_ENABLE_PATH), ("tariff_id", TARIFF_ID_PATH)):
        resource = resources.get(path)
        if resource is not None:
            result[key] = resource.metadata.resource_type or "unknown_type"
        else:
            result[key] = "missing" if path in probed_paths else "not_probed"
    return result
