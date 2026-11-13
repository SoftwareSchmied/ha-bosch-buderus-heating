"""Search the system again on request (A6), within the existing limits.

The options dialog offers one action for the whole entry. Every running cloud
source plans one discovery with its next regular cycle (the discovery that
follows a firmware change, P22/P41: readings it does not report stay, so no
entity is removed), and every running local access searches its resources
again in its usual search slots and read budget.

Parts that are paused (a cloud pause, a protection pause or the local read
budget) are skipped while the ready parts search; the result says so
(``partial``). Nothing starts while any part is already searching, or when
every reachable part is paused: the result names the reason.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from .configuration import LOCAL_KIND

if TYPE_CHECKING:
    from .managed_entry import ManagedEntry

_LOGGER = logging.getLogger(__name__)

PLANNED = "planned"
PARTIAL = "partial"
PAUSED = "paused"
READY = "ready"
RUNNING = "running"
UNAVAILABLE = "unavailable"


class _Searchable(Protocol):
    def rescan_state(self) -> str: ...

    def request_rescan(self) -> str: ...


class _LocalSearchable(Protocol):
    def rescan_state(self) -> str: ...

    def rescan(self) -> str: ...


def _parts(owner: ManagedEntry) -> tuple[list[_Searchable], list[_LocalSearchable]]:
    runtime = owner.connections
    cloud: list[_Searchable] = []
    local: list[_LocalSearchable] = []
    for key, binding in runtime.snapshot.bindings.items():
        if binding.adapter == LOCAL_KIND:
            driver = runtime.source_driver(key)
            if callable(getattr(driver, "rescan", None)) and callable(
                getattr(driver, "rescan_state", None)
            ):
                local.append(driver)  # type: ignore[arg-type]
            continue
        data = runtime.source_data(key)
        if callable(getattr(data, "request_rescan", None)) and callable(
            getattr(data, "rescan_state", None)
        ):
            cloud.append(data)  # type: ignore[arg-type]
    return cloud, local


def request_rescan(owner: ManagedEntry) -> str:
    """Plan the search for every ready part, or name why it is refused.

    ``planned``, ``partial`` (planned for the ready parts; paused parts were
    skipped), ``paused`` (every reachable part is paused), ``running`` or
    ``unavailable`` (no part of the system is reachable right now).
    """
    cloud, local = _parts(owner)
    cloud_states = [item.rescan_state() for item in cloud]
    local_states = [part.rescan_state() for part in local]
    states = cloud_states + local_states
    if RUNNING in states:
        return RUNNING
    planned = 0
    for item, state in zip(cloud, cloud_states, strict=True):
        if state == READY:
            planned += item.request_rescan() == PLANNED
    for part, state in zip(local, local_states, strict=True):
        if state == READY:
            planned += part.rescan() == PLANNED
    paused = states.count(PAUSED)
    if not planned:
        return PAUSED if paused else UNAVAILABLE
    _LOGGER.info(
        "Searching the system again on request (%d parts, %d paused)",
        planned,
        paused,
    )
    return PARTIAL if paused else PLANNED
