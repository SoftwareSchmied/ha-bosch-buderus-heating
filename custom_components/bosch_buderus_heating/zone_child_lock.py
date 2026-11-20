"""The child lock of a zone as a switch (0.9.3).

``/zones/zoneN/childLock`` is a string resource with the two values
``active`` and ``inactive``; the manufacturer's app offers it as a switch.
The switch exists only when the gateway reports the path as writable, with
exactly these two advertised values and one of them as the current value;
without that path nothing is created. It is enabled by default. Pure data
without Home Assistant imports. Expected, untested on a real installation.
"""

from __future__ import annotations

from typing import Final

from .write_policies import EnumWritePolicy

CHILD_LOCK_PATTERN: Final = r"^/zones/zone[^/:]+/childLock$"
ACTIVE: Final = "active"
INACTIVE: Final = "inactive"
SWITCH_KEY: Final = "zone_child_lock"

CHILD_LOCK_POLICY: Final = EnumWritePolicy(
    CHILD_LOCK_PATTERN,
    frozenset({"stringValue"}),
    frozenset({ACTIVE, INACTIVE}),
    known_only=True,
    options_rule="exact",
    current_required=True,
    on_value=ACTIVE,
    off_value=INACTIVE,
    enabled_by_default=True,
)

ENUM_POLICIES: Final = (CHILD_LOCK_POLICY,)


def switch_key(policy: object) -> str | None:
    """The translation key of the child lock switch, else None."""
    return SWITCH_KEY if policy is CHILD_LOCK_POLICY else None
