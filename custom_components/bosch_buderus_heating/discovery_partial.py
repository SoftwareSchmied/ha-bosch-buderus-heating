"""Show a first discovery step by step while a slow cloud still answers.

A first discovery (no stored inventory) takes one request after the other, 7 to
18 of them. With a cloud that needs ten seconds or more for each request the
entities of a new installation would only appear after minutes. Once the time
limit has passed, the coordinator therefore shows what has been found so far;
the platforms add the entities of later answers through the listeners they
already have for resources found later. The unique IDs do not depend on this:
they come from the path, so the end state is the one of a complete discovery.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from .circuit_controls import _evidence_settled
from .pointt import Resource
from .rrc import PRODUCT_TYPE_PATH, ZONE_LIST, profile_evidence

# Seconds after which the first partial result is shown, and the least time
# between two of them. Only a block of reads that has been answered counts, so
# a single slow request can delay it.
PARTIAL_AFTER: Final = 60.0

# The evidence of a room controller and the container that lists it: until it
# is answered the plant could still turn out to be one, and its entities carry
# other names than those of a heating system.
_EVIDENCE: Final = ((ZONE_LIST, "/zones"), (PRODUCT_TYPE_PATH, "/gateway"))


def profile_settled(
    report: object, found: Mapping[str, Resource], device_type: str | None
) -> bool:
    """Whether the kind of plant (room controller or not) is already decided.

    True with positive evidence, or when the evidence paths were answered
    definitely or their container was answered without listing them. Entities
    shown before that could belong to the wrong kind of plant.
    """
    if profile_evidence(device_type, found):
        return True
    paths = getattr(report, "paths", {})
    for child, container in _EVIDENCE:
        item = paths.get(child)
        if item is not None:
            if not _evidence_settled(item):
                return False
            continue
        parent = paths.get(container)
        if parent is None or not _evidence_settled(parent):
            return False
    return True


def partial_time_passed(started: float, now: float) -> bool:
    """Whether the time limit since ``started`` has passed."""
    return now - started >= PARTIAL_AFTER
