"""Explicit polling classes and bounded candidate/history request generation."""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterator, Mapping
from datetime import date
from enum import StrEnum
from itertools import product

from ..local_api.catalog import CATALOG, SOURCE_SHA256, ReadRequest, make_request
from ._data import ANCHORS, CLASSES, SIGNAL_CLASSES

MAX_RECORDS = 2048
MAX_SIGNALS = 512
MAX_RETAINED_BYTES = 8 * 1024 * 1024
MAX_RETAINED_NODES = 65536
EXPLICIT_LIMIT = 8
EXPLICIT_TIMEOUT = 120.0
SLICE_ATTEMPTS = 64
SLICE_SECONDS = 600.0
SLICE_PAUSE = 300.0
RESCAN_SECONDS = 86400.0
POLICY_VERSION = 1
REVISION = hashlib.sha256(
    repr(
        (
            SOURCE_SHA256,
            POLICY_VERSION,
            tuple(CLASSES.items()),
            ANCHORS,
            tuple(SIGNAL_CLASSES.items()),
        )
    ).encode()
).hexdigest()
_SIGNAL_TEMPLATE = "/signals/{signalId}"


class PollClass(StrEnum):
    LIVE = "live"
    SETTINGS = "settings"
    ENERGY = "energy"
    DIAGNOSTICS = "diagnostics"
    COUNTERS = "counters"
    METADATA = "metadata"
    # O43: descriptive gateway and system values that rarely change.
    SYSTEM_METADATA = "system_metadata"
    # The gateway firmware: a change rechecks the paths not found (P41).
    FIRMWARE = "firmware"
    STATIC = "static"
    HISTORY = "history"
    ADMINISTRATION = "administration"
    DISCOVERY = "discovery"


PERIODS: Mapping[PollClass, int] = {
    PollClass.LIVE: 60,
    PollClass.SETTINGS: 300,
    PollClass.ENERGY: 300,
    PollClass.DIAGNOSTICS: 300,
    PollClass.COUNTERS: 900,
    # Device and zone values (battery, radio pairing, names) stay at 15 min.
    PollClass.METADATA: 900,
    PollClass.SYSTEM_METADATA: 3600,
    PollClass.FIRMWARE: 900,
    PollClass.STATIC: 86400,
    PollClass.DISCOVERY: 900,
}


def validate_interval(value: int) -> int:
    if type(value) is not int or not 60 <= value <= 900 or value % 60:
        raise ValueError("invalid_local_poll_interval")
    return value


def category(request: ReadRequest) -> PollClass:
    template = request.definition.template
    if template == _SIGNAL_TEMPLATE and (
        special := SIGNAL_CLASSES.get(request.path.removeprefix("/signals/"))
    ):
        return PollClass(special)
    return PollClass(CLASSES[template])


def period(request: ReadRequest, configured: int, cap: int | None = None) -> int | None:
    """Class period, slowed by the configured interval, capped by a paired cloud.

    ``cap`` keeps a reading that the cloud also polls at least as frequent
    locally: min(configured local interval, cloud interval), never below 60 s.
    """
    standard = PERIODS.get(category(request))
    if standard is None:
        return None
    interval = max(standard, configured)
    return interval if cap is None else max(60, min(interval, cap))


def _group_candidates(group: str) -> Iterator[ReadRequest]:
    for definition in CATALOG:
        if (
            definition.group != group
            or PollClass(CLASSES[definition.template]) not in PERIODS
        ):
            continue
        parameters = [p for p in definition.parameters if p.location == "path"]
        if any(not p.options for p in parameters):
            continue  # Dynamic signals require a complete validated list.
        for values in product(*(p.options for p in parameters)):
            yield make_request(
                definition.template,
                parameters={
                    p.name: str(v) for p, v in zip(parameters, values, strict=True)
                },
            )


def candidates() -> Iterator[ReadRequest]:
    """Two prioritized candidates, then one other; never expand all paths."""
    from ..local_api.catalog import request_for_path

    anchors: Iterator[ReadRequest] = (request_for_path(path) for path in ANCHORS)
    anchor_set = frozenset(ANCHORS)
    groups = deque(
        _group_candidates(group) for group in dict.fromkeys(d.group for d in CATALOG)
    )

    def other() -> Iterator[ReadRequest]:
        while groups:
            group = groups.popleft()
            request = next(group, None)
            if request is not None:
                groups.append(group)
                if request.path not in anchor_set:
                    yield request

    others = other()
    pending = [anchors, others]
    while pending:
        for source in (anchors, anchors, others):
            if source not in pending:
                continue
            value = next(source, None)
            if value is None:
                pending.remove(source)
            else:
                yield value


def validate_explicit(requests: tuple[ReadRequest, ...]) -> None:
    if not 1 <= len(requests) <= 4 or len({r.target for r in requests}) != len(
        requests
    ):
        raise ValueError("invalid_local_explicit_operation")
    for request in requests:
        if category(request) is not PollClass.HISTORY:
            continue
        query = dict(request.query)
        if not isinstance(query.get("startDate"), str) or not isinstance(
            query.get("endDate"), str
        ):
            raise ValueError("local_history_window_required")
        start = date.fromisoformat(str(query["startDate"]))
        end = date.fromisoformat(str(query["endDate"]))
        rate = str(query.get("sampleRate", "all"))
        limit = {"PT1H": 7, "P1D": 31, "P1M": 366, "all": 1}.get(rate, 1)
        if not 1 <= (end - start).days + 1 <= limit:
            raise ValueError("local_history_window_limit")


def start_offset(scope: str) -> float:
    return int.from_bytes(hashlib.sha256(scope.encode()).digest()[:2]) / 65535 * 5
