"""Discovery rules for zone controllers with radio room devices.

A zone controller (for example an MX400 with radio room thermostats behind a
K40 gateway) lists many zones and radio devices. Two rules keep its request
budget for the values that matter:

* The mode and setpoint leaves of each side a zone advertises (``heating``,
  ``cooling``) are read like catalog paths, before the optional probes. The
  cloud often refuses the side directories themselves (HTTP 403) while their
  leaves stay readable one by one.
* Radio devices that list exactly the same children are the same kind of
  device. A leaf the cloud refuses (HTTP 403) on one of them is not requested
  again for the others; any other answer requests it for every device. A
  device that lists other children is a different kind and keeps its own
  requests.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import NamedTuple

from .pointt import Resource

ZONE_SIDES = ("heating", "cooling")
ZONE_SIDE_LEAVES = ("operationMode", "manualRoomSetpoint", "temporaryRoomSetpoint")
# Older names of the sides; read only as optional probes.
ZONE_SIDE_VARIANTS = ("heat", "cool", "heatCool")

_ZONE = re.compile(r"^/zones/zone[0-9]+$", re.IGNORECASE)
_DEVICE = re.compile(r"^/devices/(?!list$)[^/]+$", re.IGNORECASE)

# The read policy of a leaf left out because an identical device was refused.
IDENTICAL_DEVICE_REFUSED = "identical_device_refused"
REFUSED_STATUS = 403


def zone_side_paths(container: str, resource: Resource) -> tuple[str, ...]:
    """The mode and setpoint leaves of every side the zone directory lists."""
    if _ZONE.fullmatch(container) is None:
        return ()
    listed = {reference.path for reference in resource.references}
    return tuple(
        f"{container}/{side}/{leaf}"
        for side in ZONE_SIDES
        if f"{container}/{side}" in listed
        for leaf in ZONE_SIDE_LEAVES
    )


class Admission(Enum):
    """What the next batch does with one queued path."""

    SEND = "send"
    HOLD = "hold"
    SKIP = "skip"


class _Group(NamedTuple):
    children: frozenset[str]
    leaf: str


class IdenticalDeviceRefusals:
    """Request a leaf refused on one device not again on identical devices.

    ``begin_batch`` starts each batch: the first path of a group in it is
    sent, the others of that group wait for its answer (``answered``). A
    group whose leaf was refused skips its remaining paths; any other answer
    opens it for every device. Nothing waits longer than one batch.
    """

    def __init__(self) -> None:
        self._groups: dict[str, _Group] = {}
        self._refused: dict[_Group, str] = {}
        self._open: set[_Group] = set()
        self._batch: dict[_Group, str] = {}
        self.skipped: dict[str, str] = {}

    def listed(self, container: str, resource: Resource) -> None:
        """Group the leaves of a device directory by its list of children."""
        if _DEVICE.fullmatch(container) is None:
            return
        prefix = f"{container}/"
        leaves = frozenset(
            reference.path[len(prefix) :]
            for reference in resource.references
            if reference.path.startswith(prefix)
            and "/" not in reference.path[len(prefix) :]
        )
        if len(leaves) < 2:
            return  # a single leaf says nothing about the kind of device
        for leaf in leaves:
            self._groups.setdefault(f"{prefix}{leaf}", _Group(leaves, leaf))

    def begin_batch(self) -> None:
        self._batch.clear()

    def admit(self, path: str) -> Admission:
        group = self._groups.get(path)
        if group is None or group in self._open:
            return Admission.SEND
        if (refused := self._refused.get(group)) is not None:
            self.skipped[path] = refused
            return Admission.SKIP
        leader = self._batch.setdefault(group, path)
        return Admission.SEND if leader == path else Admission.HOLD

    def answered(self, path: str, status: int | None) -> None:
        group = self._groups.get(path)
        if group is None or group in self._open or group in self._refused:
            return
        if status == REFUSED_STATUS:
            self._refused[group] = path
        else:
            self._open.add(group)
