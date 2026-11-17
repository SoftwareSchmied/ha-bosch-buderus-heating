"""L16: the setting a write changes is read live in the write's own request.

Every control write (``Coordinator.async_write_control``) reads the written
resource from the gateway before the one request goes out. Its flags
(``used``, ``available``, ``writeable``), limits, options and value are then
checked against that reading, never against the reading of the last poll. The
reading travels in the same bulk request as the guards and ``fresh`` readings
of the write, so a write costs no extra request when it has any of them.

A write is refused, with nothing sent, when

* the live reading fails (no answer, refused, an authentication problem),
* the live reading is not usable (``used`` false, ``available`` false, a
  value or a limit outside the policy), or
* a guard of the write (``expected``) changed in the meantime.

A value that already stands is confirmed from the live reading without a
request. After a PUT the contract stays as it was: one PUT, read-back, no
retry, the send window.

A package that adds a control needs nothing special: it passes its policy to
``async_write_control`` (directly or through ``control.write_control``). It
names further readings its rules depend on in ``fresh=`` (read together with
the target, optional when the gateway may not have them) and the readings a
dialog showed in ``expected=``.
"""

from __future__ import annotations

from .pointt.models import Resource


def with_live_target(
    reads: tuple[Resource, ...], target: Resource
) -> tuple[Resource, ...]:
    """``reads`` plus the written setting, read once and only once."""
    if any(item.path == target.path for item in reads):
        return reads
    return (*reads, target)
