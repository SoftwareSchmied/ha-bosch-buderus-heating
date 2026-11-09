"""Spread the cloud paths of longer polling groups over the regular wake-ups.

The coordinator wakes once per tick (one minute) and reads every due path in
batches of up to 30. Groups read every 5, 15 or 60 minutes all fall due on
the same wake-up, so that wake-up needs extra batches while the others have
room left. A slot moves a path's read a fixed number of ticks before its
group's deadline: slot 0 is read at the deadline, slot ``s`` ``s`` ticks
earlier. A path therefore keeps exactly its group's period; only its phase
changes. Slots are chosen so that the paths fill the room the shorter groups
leave in each batch, never on a wake-up that would otherwise send nothing.

Planning assumes the worst case: every path of a group is read at the group's
own period (no path is read less often because its value is supplied
locally), so the plan stays valid whatever the local access does.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence

# A larger common period only makes the model coarser, never wrong.
MAX_PLAN_TICKS = 1440


def plan_poll_slots[G](
    paths_by_group: Mapping[G, Sequence[str]],
    periods: Mapping[G, int],
    *,
    capacity: int,
    previous: Mapping[str, tuple[G, int]] | None = None,
    atomic: Collection[G] = (),
) -> dict[str, tuple[G, int]]:
    """Return ``path -> (group, slot)`` for every path of a planned group.

    ``periods`` gives each group's period in ticks. Groups read every tick
    keep slot 0, and so do the ``atomic`` groups: their paths are judged
    together, so every read of the group must contain all of them (the fault
    sources). Every other path takes the earliest slot whose wake-ups all
    have room left in an already needed batch, otherwise the slot that adds
    the fewest batches (slot 0 on a tie, as without spreading). A path keeps
    its earlier slot while its group and period stay the same, so a running
    schedule never moves a read later than its period allows.
    """
    if capacity < 1:
        raise ValueError("capacity must be positive")
    planned = {
        group: max(1, int(period))
        for group, period in periods.items()
        if group in paths_by_group
    }
    horizon = 1
    for period in planned.values():
        horizon = math.lcm(horizon, period)
    horizon = min(horizon, MAX_PLAN_TICKS)
    load = [0] * horizon
    slots: dict[str, tuple[G, int]] = {}
    pending: list[tuple[G, int, str]] = []
    earlier = previous or {}
    seen: set[str] = set()
    for group, period in sorted(planned.items(), key=lambda item: item[1]):
        for path in paths_by_group[group]:
            if path in seen:
                continue
            seen.add(path)
            kept = earlier.get(path)
            if period == 1 or group in atomic:
                slots[path] = (group, 0)
                _add(load, period, 0)
            elif kept is not None and kept[0] == group and 0 <= kept[1] < period:
                slots[path] = kept
                _add(load, period, kept[1])
            else:
                pending.append((group, period, path))
    for group, period, path in pending:
        best, best_cost = 0, -1
        for slot in range(period):
            ticks = _ticks(period, slot, len(load))
            cost = sum(1 for tick in ticks if load[tick] % capacity == 0)
            if best_cost < 0 or cost < best_cost:
                best, best_cost = slot, cost
            if cost == 0:
                break
        slots[path] = (group, best)
        _add(load, period, best)
    return slots


def _ticks(period: int, slot: int, horizon: int) -> range:
    """Wake-ups (within one common period) at which this slot is read."""
    return range((period - slot) % period, horizon, period)


def _add(load: list[int], period: int, slot: int) -> None:
    for tick in _ticks(period, slot, len(load)):
        load[tick] += 1


def batches_per_period[G](
    paths_by_group: Mapping[G, Sequence[str]],
    periods: Mapping[G, int],
    slots: Mapping[str, tuple[G, int]],
    *,
    capacity: int,
) -> int:
    """Batches one common period needs with these slots (for tests and checks)."""
    horizon = 1
    for group in paths_by_group:
        horizon = math.lcm(horizon, max(1, int(periods[group])))
    horizon = min(horizon, MAX_PLAN_TICKS)
    load = [0] * horizon
    for group, paths in paths_by_group.items():
        period = max(1, int(periods[group]))
        for path in paths:
            _add(load, period, slots.get(path, (group, 0))[1] if period > 1 else 0)
    return sum(math.ceil(item / capacity) for item in load)
