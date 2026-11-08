"""Search the system again on request (A6), within the existing limits.

The options dialog offers one action for the whole entry. Every running cloud
source plans one discovery with its next regular cycle (the discovery that
follows a firmware change, P22/P41: readings it does not report stay, so no
entity is removed), and every running local access searches its resources
again in its usual search slots and read budget.

Nothing starts while any part is paused (a cloud pause, a protection pause or
the local read budget) or already searching: the result names the reason.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from .configuration import LOCAL_KIND

if TYPE_CHECKING:
    from .managed_entry import ManagedEntry

_LOGGER = logging.getLogger(__name__)

PLANNED = "planned"
PAUSED = "paused"
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

    ``planned``, ``paused``, ``running`` or ``unavailable`` (no part of the
    system is reachable right now).
    """
    cloud, local = _parts(owner)
    states = [item.rescan_state() for item in cloud]
    states += [part.rescan_state() for part in local]
    if PAUSED in states:
        return PAUSED
    if RUNNING in states:
        return RUNNING
    planned = 0
    for item in cloud:
        planned += item.request_rescan() == PLANNED
    for part in local:
        planned += part.rescan() == PLANNED
    if not planned:
        return UNAVAILABLE
    _LOGGER.info("Searching the system again on request (%d parts)", planned)
    return PLANNED
