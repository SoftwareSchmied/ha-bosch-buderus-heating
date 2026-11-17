"""Which zone a radio room device of a zone controller belongs to.

A zone controller (for example an MX400 behind a K40 gateway) gives its radio
devices no name of its own (``/devices/<id>/name`` is not found), but each
device reports the number of its zone in ``/devices/<id>/zoneId``. The zone
has the name the installer gave it (``/zones/zone<N>/name``). The entities of
a radio device are named after that zone; a zone with several devices adds
the position of the device among them (by device number).

Only the assignment is decided here; the zone's name and the labels come from
``device.py``. A device without a valid ``zoneId``, or one that names a zone
the plant does not report, has no assignment and keeps its generic name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final, NamedTuple

from .pointt import Resource

_ZONE_ID: Final = re.compile(r"^/devices/(?!list$)([^/]+)/zoneId$")
_NUMBER: Final = re.compile(r"(\d+)$")


class ZoneAssignment(NamedTuple):
    """The zone of a radio device and its place among the zone's devices."""

    zone: int
    position: int  # 1-based, by device number
    members: int  # the devices of the zone


def zone_number(resource: Resource | None) -> int | None:
    """The zone number a ``zoneId`` reading names, ``None`` when it names none.

    A whole number from 1 on; a flag, text, negative number, a fraction or a
    non-finite number names no zone.
    """
    if resource is None or not resource.has_value:
        return None
    value = resource.value
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    number = int(value)
    return number if number >= 1 else None


def _order(device: str) -> tuple[int, str]:
    match = _NUMBER.search(device)
    return (int(match.group(1)) if match else 0, device)


def zone_assignment(
    resources: Mapping[str, Resource], device: str
) -> ZoneAssignment | None:
    """The zone of ``device`` (its id such as ``device5``), if it has one."""
    zone_of: dict[str, int] = {}
    for path, resource in resources.items():
        match = _ZONE_ID.fullmatch(path)
        if match is not None and (zone := zone_number(resource)) is not None:
            zone_of[match.group(1)] = zone
    zone = zone_of.get(device)
    if zone is None or not _zone_reported(resources, zone):
        return None
    members = sorted(
        (item for item, number in zone_of.items() if number == zone), key=_order
    )
    return ZoneAssignment(zone, members.index(device) + 1, len(members))


def _zone_reported(paths: Iterable[str], zone: int) -> bool:
    prefix = f"/zones/zone{zone}/"
    exact = f"/zones/zone{zone}"
    return any(path == exact or path.startswith(prefix) for path in paths)
