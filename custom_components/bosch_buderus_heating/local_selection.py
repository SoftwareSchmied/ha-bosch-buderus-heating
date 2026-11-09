"""Supply eligible existing entities from paired local readings.

The selection is a view over data the connections already publish. It owns no
credentials and never changes write paths; its only request is a hint to a
running local source to read a circuit again after a confirmed cloud write.
When the target's ``local_values`` option is enabled, a reading that the pair
catalog lists and that is neither a counter nor adjustable through the cloud
uses the current local value. Without a cloud (switched off, sign-in required,
paused, limited or unreachable) the local value is used also with the option
off; paired counters then follow the counter rule, never backwards. If the
local value is not current, the current cloud value is kept; otherwise the
existing cloud freshness rules decide availability. Type and unit must match
the cloud resource that created the entity.

After a confirmed cloud write, a local reading of the same circuit received
before the confirmation loses against a cloud reading received after it; a
running local source is asked to read the circuit again. The local access can
never confirm a write.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Any, TypeVar, cast

from .configuration import CLOUD_KIND, LOCAL_KIND
from .configuration_management import section
from .observations import (
    Observation,
    PairResult,
    ValueStatus,
    canonical_unit,
    cloud_items,
    compare,
    from_cloud_snapshot,
    from_local_observation,
    is_working_time,
    pair_for_path,
)

if TYPE_CHECKING:
    from .configuration import ConfigurationSnapshot
    from .connection_runtime import ConnectionRuntime
    from .coordinator import ResourceSnapshot
    from .local_api.models import LocalObservation
    from .local_state import LocalSourceState
    from .pointt import Resource
    from .pointt.models import JsonValue


_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")
# A current local reading and the binding of the local access that supplied it.
type _Reading = tuple[str, LocalObservation]

# A written value whose local read-back does not arrive within this time is
# counted as unchecked; regular reads continue unchanged.
WRITE_CHECK_SECONDS = 600.0
# Bounded bookkeeping per target, also with unusual write patterns.
MAX_WRITE_SCOPES = 64


def write_scope(path: str) -> str:
    """The circuit a written path belongs to, e.g. ``/heatingCircuits/hc1``."""
    parts = path.split("/")
    return "/".join(parts[:3]) if len(parts) > 3 else path


def _in_scope(path: str, scope: str) -> bool:
    return path == scope or path.startswith(scope + "/")


class LocalSelection:
    """Per-target view: local preferred for eligible readings, cloud otherwise."""

    def __init__(self, runtime: ConnectionRuntime, target_id: str) -> None:
        self._runtime = runtime
        self._target_id = target_id
        self._last_marker: (
            tuple[
                bool,
                dict[str, _Reading],
                dict[str, _Reading | None],
                tuple[object, ...],
            ]
            | None
        ) = None
        # Circuit scope -> time of the latest confirmed cloud write.
        self._confirmed_writes: dict[str, datetime] = {}
        # Written catalog path -> (confirmed cloud snapshot, confirmation time).
        self._pending_checks: dict[str, tuple[ResourceSnapshot, datetime]] = {}
        self.write_checks: dict[str, Counter[str]] = {}
        # Values worked out once per synchronous update pass (None outside).
        self._pass: dict[object, Any] | None = None
        # Last shown source per path, only to log changes (debug level).
        self._shown_source: dict[str, str] = {}
        # Local-only paths with an enabled entity (P45); only these count as
        # changes, so disabled signals never cause entity updates.
        self._tracked_local_only: Counter[str] = Counter()
        self._bindings_cache: tuple[object, tuple[str, ...]] | None = None
        self._local_only_cache: (
            tuple[tuple[tuple[str, object], ...], dict[str, LocalObservation]] | None
        ) = None
        # 4A (P49b): registered local-only entities of documented readings
        # without a reviewed entry (created by the local plant view).
        self._registered_local_only: frozenset[str] = frozenset()
        # Last answer of the cloud check, only for the diagnostics summary.
        self._without_cloud = False

    def serve_registered_local_only(self, paths: frozenset[str]) -> None:
        """Also supply these registered local-only paths (never new ones)."""
        if paths != self._registered_local_only:
            self._registered_local_only = paths
            self._local_only_cache = None

    @contextmanager
    def update_pass(self) -> Iterator[None]:
        """Work out options and local readings once for one update pass.

        Entities read them several times each (availability, value, unit,
        attributes). Within one synchronous pass neither the options nor the
        published local readings can change, so they are computed on first use
        and dropped when the pass ends. Outside a pass nothing is kept.
        """
        if self._pass is not None:
            yield
            return
        self._pass = {}
        try:
            yield
        finally:
            self._pass = None

    def _cached(self, key: object, compute: Callable[[], _T]) -> _T:
        cache = self._pass
        if cache is None:
            return compute()
        if key not in cache:
            cache[key] = compute()
        return cast("_T", cache[key])

    def note_confirmed_write(self, path: str, confirmed: ResourceSnapshot) -> None:
        """Older local readings of the circuit no longer count for the display."""
        at = confirmed.last_success
        if at is None:
            return
        scope = write_scope(path)
        if scope not in self._confirmed_writes and (
            len(self._confirmed_writes) >= MAX_WRITE_SCOPES
        ):
            # Oldest first: keep the bookkeeping bounded.
            oldest = min(self._confirmed_writes, key=self._confirmed_writes.__getitem__)
            del self._confirmed_writes[oldest]
        self._confirmed_writes[scope] = at
        pair = pair_for_path(path)
        if (
            pair is None
            or pair.counter
            or not self.enabled
            or path not in self.local_observations()
        ):
            # Only a setting the local access already reads can be checked.
            return
        if path in self._pending_checks:
            # A newer write replaces the earlier one before its read-back.
            self._count(pair.measurement, "unchecked")
        self._pending_checks[path] = (confirmed, at)

    def _count(self, measurement: str, result: str) -> None:
        self.write_checks.setdefault(measurement, Counter())[result] += 1

    def _older_than_write(
        self, path: str, observation: LocalObservation, cloud: ResourceSnapshot
    ) -> bool:
        """A local reading from before a confirmed write of its circuit loses
        against a cloud reading received after it, never against an older one."""
        written = self._confirmed_writes.get(write_scope(path))
        return (
            written is not None
            and observation.observed_at <= written
            and cloud.last_success is not None
            and cloud.last_success > observation.observed_at
        )

    @property
    def has_local_access(self) -> bool:
        """An enabled local binding exists for this target."""
        return bool(self._local_bindings())

    def local_only(self, path: str, cloud: ResourceSnapshot) -> ResourceSnapshot | None:
        """The current local reading alone, independent of the option.

        Used by the optional per-source entities for troubleshooting; ``None``
        when there is no current, convertible local reading.
        """
        # Display only: also a setting adjustable through the cloud may be
        # shown as read locally; nothing here is written.
        if not eligible_path(path):
            return None
        observation = self.local_observations().get(path)
        if observation is None:
            return None
        return local_snapshot(cloud, observation, self._target_id)

    @property
    def has_configured_local_access(self) -> bool:
        """A local binding exists for this target, also while switched off."""
        return any(
            binding.target_id == self._target_id and binding.adapter == LOCAL_KIND
            for binding in self._runtime.snapshot.bindings.values()
        )

    def local_only_observations(self) -> Mapping[str, LocalObservation]:
        """Current local readings of local-only paths (P45), independent of the
        ``local_values`` option. Within an update pass: read only."""
        return self._cached("local_only", self._compute_local_only)

    def _compute_local_only(self) -> dict[str, LocalObservation]:
        from .local_only import definition_for
        from .local_plant_view import private_path

        states = tuple(
            (binding_id, self._runtime.local_state(binding_id))
            for binding_id in self._local_bindings()
        )
        cached = self._local_only_cache
        # Published states are replaced, never changed: the same objects give
        # the same result, also outside an update pass (entity registration).
        if (
            cached is not None
            and len(cached[0]) == len(states)
            and all(
                a[0] == b[0] and a[1] is b[1]
                for a, b in zip(cached[0], states, strict=True)
            )
        ):
            return cached[1]
        result: dict[str, LocalObservation] = {}
        for _, state in states:
            if state is None:
                continue
            for observation in state.current_observations:
                path = observation.request.path
                # C7: every reading the cloud pair catalog does not cover;
                # the entity rules decide (identifiers and structures never).
                if (
                    definition_for(path) is not None
                    or path in self._registered_local_only
                    or (pair_for_path(path) is None and not private_path(path))
                ):
                    result[path] = observation
        self._local_only_cache = (states, result)
        return result

    def local_only_reading(self, path: str) -> Observation | None:
        """The interpreted current reading of one local-only path, if any.

        Within an update pass worked out once per path: the entity asks for
        it for its availability, value, unit and attributes.
        """
        return self._cached(
            ("local_only_reading", path), lambda: self._local_only_reading(path)
        )

    def _local_only_reading(self, path: str) -> Observation | None:
        observation = self.local_only_observations().get(path)
        if observation is None:
            return None
        reading = from_local_observation(
            observation, connection_id="local", target_id=self._target_id, current=True
        )
        if getattr(observation.data, "matched_states", ()):
            # A declared device state (for example "lowflow" or "OFF_COOL"), not
            # a measurement; states and on/off forms use its name.
            return replace(reading, status=ValueStatus.STATE_MARKER, value=None)
        return reading

    def fault_reading(
        self,
    ) -> tuple[bool, LocalObservation | None, int | None]:
        """(local access ready, current local error list, its interval) for P42.

        Only a fresh reading counts; a late one (for example after a timeout)
        is no new evidence. Independent of the ``local_values`` option.
        """
        from .local_state import LocalCondition

        ready = False
        for binding_id in self._local_bindings():
            state = self._runtime.local_state(binding_id)
            if state is None:
                continue
            ready = ready or getattr(state, "condition", None) is LocalCondition.READY
            for view in getattr(state, "resources", ()) or ():
                if (
                    view.request.path == "/notifications"
                    and view.available
                    and view.freshness.value == "fresh"
                    and view.observation is not None
                ):
                    interval = getattr(view, "target_interval_seconds", None)
                    return ready, view.observation, interval
        return ready, None, None

    def device_error_lists(self) -> tuple[LocalObservation, ...]:
        """B3: the current local device error lists (``/devices/…/errors``).

        Only readings that are still current (fresh or late, from a ready
        access); extra information next to the faults, never evidence.
        """
        from .local_state import LocalCondition

        found: dict[str, LocalObservation] = {}
        for binding_id in self._local_bindings():
            state = self._runtime.local_state(binding_id)
            if getattr(state, "condition", None) is not LocalCondition.READY:
                continue
            for view in getattr(state, "resources", ()) or ():
                path = view.request.path
                if (
                    path.startswith("/devices/")
                    and path.endswith("/errors")
                    and view.available
                    and view.observation is not None
                ):
                    found.setdefault(path, view.observation)
        return tuple(found.values())

    def local_states(self) -> tuple[LocalSourceState | None, ...]:
        """The published states of this target's enabled local bindings
        (P49b), independent of the ``local_values`` option."""
        return tuple(
            self._runtime.local_state(binding_id)
            for binding_id in self._local_bindings()
        )

    def track_local_only(self, path: str) -> None:
        self._tracked_local_only[path] += 1

    def untrack_local_only(self, path: str) -> None:
        self._tracked_local_only[path] -= 1
        if self._tracked_local_only[path] <= 0:
            del self._tracked_local_only[path]

    def _local_bindings(self) -> list[str]:
        if self._pass is not None:
            return list(self._cached("bindings", self._compute_local_bindings))
        # Outside a pass (entity registration, change checks): the snapshot is
        # replaced on every configuration change, so its identity is the key.
        snapshot = self._runtime.snapshot
        cached = self._bindings_cache
        if cached is None or cached[0] is not snapshot:
            cached = (snapshot, self._compute_local_bindings())
            self._bindings_cache = cached
        return list(cached[1])

    def _compute_local_bindings(self) -> tuple[str, ...]:
        snapshot = self._runtime.snapshot
        options = snapshot.to_options()
        bindings = section(options, "bindings")
        raw = options.get("connections")
        connections: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        result = []
        for binding_id, binding in snapshot.bindings.items():
            if (
                binding.target_id != self._target_id
                or binding.adapter != LOCAL_KIND
                or bindings.get(binding_id, {}).get("enabled") is not True
            ):
                continue
            connection_id = getattr(binding, "connection_id", None)
            settings = (
                connections.get(connection_id)
                if isinstance(connection_id, str)
                else None
            )
            # A switched-off access stops its requests: no local access then.
            if isinstance(settings, Mapping) and settings.get("enabled") is False:
                continue
            result.append(binding_id)
        return tuple(result)

    def request_refresh(self, path: str) -> int:
        """Ask running local sources to read the written circuit again."""
        if not self.enabled:
            return 0
        paths = self.refresh_paths(path)
        if not paths:
            return 0
        sent = 0
        for binding_id in self._local_bindings():
            driver = self._runtime.source_driver(binding_id)
            refresh = getattr(driver, "refresh", None)
            if callable(refresh):
                sent += int(refresh(paths))
        return sent

    def refresh_paths(self, path: str) -> tuple[str, ...]:
        """Local readings to read again after a confirmed write of ``path``."""
        scope = write_scope(path)
        # Only readings the local access already has: no new inventory entries.
        paths = {item for item in self.local_observations() if _in_scope(item, scope)}
        return tuple(sorted(paths))

    def check_writes(self, now: datetime) -> None:
        """Compare each confirmed write with the first newer local reading."""
        if not self._pending_checks:
            return
        local = self.local_observations()
        for path, (confirmed, at) in tuple(self._pending_checks.items()):
            pair = pair_for_path(path)
            if pair is None:  # pragma: no cover - only catalog paths are pending
                del self._pending_checks[path]
                continue
            observation = local.get(path)
            late = (
                observation is not None
                and (observation.observed_at - at).total_seconds() > WRITE_CHECK_SECONDS
            )
            if observation is not None and observation.observed_at > at and not late:
                comparison = compare(
                    from_cloud_snapshot(
                        confirmed, connection_id="cloud", target_id=self._target_id
                    ),
                    from_local_observation(
                        observation,
                        connection_id="local",
                        target_id=self._target_id,
                        current=True,
                    ),
                )
                kind = (
                    comparison.result
                    if comparison is not None
                    else PairResult.INCOMPARABLE
                )
                if kind is PairResult.CHANGED_BETWEEN_READS:
                    # A written setting does not change by itself: a readback
                    # that does not confirm it differs, however late it came
                    # (A1 applies to the pair diagnostics only).
                    kind = PairResult.DIFFER
                result = kind.value
            elif late or (now - at).total_seconds() >= WRITE_CHECK_SECONDS:
                result = "unchecked"
            else:
                continue
            self._count(pair.measurement, result)
            del self._pending_checks[path]

    def write_check_summary(self) -> dict[str, dict[str, int]]:
        """Per measurement: agreeing, differing, incomparable, unchecked checks."""
        return {
            name: dict(sorted(counts.items()))
            for name, counts in sorted(self.write_checks.items())
        }

    @property
    def enabled(self) -> bool:
        return self._cached("enabled", self._compute_enabled)

    def _compute_enabled(self) -> bool:
        options = self._runtime.snapshot.to_options()
        target = section(options, "targets").get(self._target_id, {})
        return target.get("local_values", False) is True

    def uses_local(
        self,
        without_cloud: Callable[[], bool] | None = None,
        *,
        counters: bool = False,
    ) -> bool:
        """Local values come first: with the option on, or without a cloud.

        ``without_cloud`` reports that the cloud cannot be used now; it is
        asked once per update pass and only with the option off. Counters
        use their own, stricter answer (a durable outage only).
        """
        if self.enabled:
            return True
        if without_cloud is None:
            return False
        down = self._cached(("without_cloud", counters), without_cloud)
        if not counters:
            self._without_cloud = down
        return down

    def local_observations(
        self, *, counters: bool = False
    ) -> Mapping[str, LocalObservation]:
        """Current local observations of enabled local bindings for this target.

        Within an update pass all callers share one mapping: read it only.
        """
        return self._cached(
            ("observations", counters),
            lambda: self._compute_local_observations(counters=counters),
        )

    def _compute_local_observations(
        self, *, counters: bool
    ) -> dict[str, LocalObservation]:
        result: dict[str, LocalObservation] = {}
        for binding_id in self._local_bindings():
            state = self._runtime.local_state(binding_id)
            if state is None:
                continue
            for observation in state.current_observations:
                path = observation.request.path
                if eligible_path(path) or (counters and counter_path(path)):
                    result[path] = observation
        return result

    def select(
        self,
        path: str,
        cloud: ResourceSnapshot | None,
        local: Mapping[str, LocalObservation] | None = None,
        *,
        without_cloud: Callable[[], bool] | None = None,
    ) -> ResourceSnapshot | None:
        """Return the snapshot an existing entity should display.

        ``without_cloud`` (see ``uses_local``) lets local values come first
        while the cloud cannot be used, also with the option off.
        """
        if (
            cloud is None
            or not eligible(path, cloud)
            or not self.uses_local(without_cloud)
        ):
            if cloud is not None and path in self._shown_source:
                self._note_source(path, "cloud")  # e.g. after switching the option off
            return cloud
        observation = (local if local is not None else self.local_observations()).get(
            path
        )
        if observation is None or self._older_than_write(path, observation, cloud):
            shown = cloud
        else:
            shown = local_snapshot(cloud, observation, self._target_id) or cloud
        self._note_source(path, "cloud" if shown is cloud else "local")
        return shown

    def _note_source(self, path: str, source: str) -> None:
        """Log a source change of a paired reading, without values or IDs."""
        previous = self._shown_source.get(path)
        self._shown_source[path] = source
        if previous is not None and previous != source:
            pair = pair_for_path(path)
            _LOGGER.debug(
                "Paired reading %s is now shown from the %s access",
                pair.measurement if pair is not None else "unknown",
                source,
            )

    def counter_candidate(
        self,
        path: str,
        cloud: ResourceSnapshot | None,
        *,
        without_cloud: Callable[[], bool] | None = None,
    ) -> ResourceSnapshot | None:
        """Current local counter in the cloud entity's shape, if comparable.

        Offered with the option on or without a cloud; the counter rule
        decides whether it may be shown.
        """
        if (
            cloud is None
            or not counter_path(path)
            or not self.uses_local(without_cloud, counters=True)
        ):
            return None
        if cloud.resource.metadata.writable:
            return None
        observation = self.local_observations(counters=True).get(path)
        if observation is None:
            return None
        return local_counter_snapshot(cloud, observation, self._target_id)

    def changed(self) -> bool:
        """Report changed local readings or a changed option since the last call.

        Also with the option off while a local access exists: the per-source
        entities (P18) show the local reading independently of the option.
        Switching the option itself always counts as a change.

        A reading counts as changed when its answer (value, unit, states), its
        currency (it became current or no longer is) or the local access that
        supplied it changed. Reading the same answer again is no change, so
        it causes no entity update; the expiry of a reading is published by
        the local source and counts as a change.
        """
        enabled = self.enabled
        readings: dict[str, _Reading] = {}
        # Readable local-only paths count, so new entities can be added;
        # their answers only for paths with an enabled entity (P45).
        local_only: dict[str, _Reading | None] = {}
        if enabled or self.has_local_access:
            readable = self.local_only_observations()
            tracked = self._tracked_local_only
            for binding_id in self._local_bindings():
                state = self._runtime.local_state(binding_id)
                if state is None:
                    continue
                for observation in state.current_observations:
                    path = observation.request.path
                    if pair_for_path(path) is not None:
                        readings[path] = (binding_id, observation)
                    if path in readable:
                        local_only[path] = (
                            (binding_id, observation) if path in tracked else None
                        )
        writes = self._write_marker(readings)
        previous = self._last_marker
        self._last_marker = (enabled, readings, local_only, writes)
        return not (
            previous is not None
            and previous[0] == enabled
            and previous[3] == writes
            and _same_readings(previous[1], readings)
            and _same_readings(previous[2], local_only)
        )

    def _write_marker(self, readings: Mapping[str, _Reading]) -> tuple[object, ...]:
        """What a confirmed cloud write changes about the readings.

        A reading from before the write loses against the cloud (``select``),
        so a newer reading with the same answer is still a change; a pending
        write check waits for the first newer reading (``check_writes``).
        """
        if not self._confirmed_writes and not self._pending_checks:
            return ()
        older = frozenset(
            path
            for path, (_, item) in readings.items()
            if (written := self._confirmed_writes.get(write_scope(path))) is not None
            and item.observed_at <= written
        )
        pending = tuple(
            (path, readings[path][1].observed_monotonic)
            for path in self._pending_checks
            if path in readings
        )
        return older, pending

    def summary(self, cloud: dict[str, ResourceSnapshot]) -> dict[str, Any]:
        """Diagnostics counts without values, paths or identifiers."""
        using = self.enabled or self._without_cloud
        local = self.local_observations() if using else {}
        eligible_paths = [path for path, item in cloud.items() if eligible(path, item)]
        from .coordinator import SOURCE_STOPPED_CATEGORY

        reasons: Counter[str] = Counter()
        names: dict[str, set[str]] = {}
        for path in eligible_paths:
            observation = local.get(path)
            reason: str | None
            if not using:
                reason = "option_off"
            elif cloud[path].last_error_category == SOURCE_STOPPED_CATEGORY:
                reason = "source_stopped"
            elif observation is None:
                reason = "local_not_current"
            elif self._older_than_write(path, observation, cloud[path]):
                reason = "older_than_write"
            else:
                reason = _local_value(cloud[path], observation, self._target_id)[1]
            if reason is None:
                continue
            reasons[reason] += 1
            if reason != "option_off" and (pair := pair_for_path(path)) is not None:
                names.setdefault(reason, set()).add(pair.measurement)
        using_local = len(eligible_paths) - sum(reasons.values())
        return {
            "enabled": self.enabled,
            "eligible_entities_sources": len(eligible_paths),
            "using_local": using_local,
            "using_cloud": len(eligible_paths) - using_local,
            "cloud_reasons": dict(sorted(reasons.items())),
            "cloud_reason_measurements": {
                reason: sorted(items) for reason, items in sorted(names.items())
            },
            "write_checks": self.write_check_summary(),
        }


def same_answer(before: LocalObservation, after: LocalObservation) -> bool:
    """Two readings of one path with exactly the same answer.

    The decoded reading follows from the request and the received document,
    so equal documents give equal readings; the reading times are ignored.
    Numbers count as equal only with the same type and spelling.
    """
    return before is after or _same_json(before.document, after.document)


def _same_json(before: object, after: object) -> bool:
    if type(before) is not type(after):
        return False
    if isinstance(before, Mapping):
        other = cast("Mapping[str, object]", after)
        return before.keys() == other.keys() and all(
            _same_json(value, other[key]) for key, value in before.items()
        )
    if isinstance(before, tuple):
        items = cast("tuple[object, ...]", after)
        return len(before) == len(items) and all(
            map(_same_json, before, items, strict=True)
        )
    if isinstance(before, Decimal):
        return str(before) == str(after)
    return before == after


def _same_readings(
    before: Mapping[str, _Reading | None], after: Mapping[str, _Reading | None]
) -> bool:
    """The same paths, each from the same access with the same answer."""
    if before.keys() != after.keys():
        return False
    for path, now in after.items():
        then = before[path]
        if then is now:
            continue
        if (
            then is None
            or now is None
            or then[0] != now[0]
            or not same_answer(then[1], now[1])
        ):
            return False
    return True


def eligible_path(path: str) -> bool:
    pair = pair_for_path(path)
    return pair is not None and not pair.counter


def counter_path(path: str) -> bool:
    pair = pair_for_path(path)
    return pair is not None and pair.counter


def _counter_units_match(cloud: Resource, local_unit: str | None) -> bool:
    """Same unit, or an established unitless cloud contract: kWh for energy
    counters, seconds for the working time of a heat source."""
    from .energy_contracts import is_energy_collection

    if canonical_unit(local_unit) == canonical_unit(cloud.metadata.unit):
        return True
    if cloud.metadata.unit is not None:
        return False
    if is_energy_collection(cloud):
        return canonical_unit(local_unit) == "kWh"
    return is_working_time(cloud.path) and canonical_unit(local_unit) == "s"


def local_counter_snapshot(
    cloud: ResourceSnapshot, observation: LocalObservation, target_id: str
) -> ResourceSnapshot | None:
    """Local counter with exactly the cloud's parts, in the cloud's shape."""
    from .coordinator import Freshness, SnapshotSource

    item = from_local_observation(
        observation, connection_id="local", target_id=target_id, current=True
    )
    resource = cloud.resource
    if (
        item.status is not ValueStatus.VALID
        or item.resource_type != resource.metadata.resource_type
        or not _counter_units_match(resource, item.unit)
    ):
        return None
    if item.parts:
        local_parts = {name: value for name, value in item.parts}
        if len(local_parts) != len(item.parts):
            return None
        if resource.values:
            shaped: list[dict[str, JsonValue]] = []
            names: set[str] = set()
            for part in resource.values:
                if not isinstance(part, Mapping):
                    return None
                numbers = {
                    name: _like(local_parts.get(name), raw)
                    for name, raw in part.items()
                }
                if any(value is None for value in numbers.values()):
                    return None
                shaped.append(dict(numbers))
                names.update(part)
            if names != set(local_parts):
                return None
            new_resource = replace(resource, values=tuple(shaped))
        elif isinstance(resource.value, Mapping):
            if set(resource.value) != set(local_parts):
                return None
            new_resource = replace(
                resource,
                value={
                    name: _like(local_parts[name], raw)
                    for name, raw in resource.value.items()
                },
            )
        else:
            return None
    else:
        if resource.values or isinstance(resource.value, Mapping):
            return None
        value = _like(item.value, resource.value)
        if value is None:
            return None
        new_resource = replace(resource, value=value, has_value=True)
    return replace(
        cloud,
        resource=new_resource,
        available=True,
        last_success=observation.observed_at,
        source=SnapshotSource.LOCAL,
        freshness=Freshness.FRESH,
        last_error_category=None,
        consecutive_failures=0,
        ended=False,
    )


def _like(value: object, template: object) -> int | float | None:
    """A local number with the Python type the cloud used for the same field."""
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, int | float | Decimal):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if type(template) is int and number.is_integer():
        return int(number)
    return number


def eligible(path: str, cloud: ResourceSnapshot) -> bool:
    """Paired, not a counter and not adjustable through the cloud."""
    return eligible_path(path) and not cloud.resource.metadata.writable


def local_snapshot(
    cloud: ResourceSnapshot, observation: LocalObservation, target_id: str
) -> ResourceSnapshot | None:
    """Translate a compatible local reading into the entity's snapshot shape."""
    from .coordinator import Freshness, SnapshotSource

    value, reason = _local_value(cloud, observation, target_id)
    if reason is not None:
        return None
    if isinstance(value, tuple):
        # B2: a list of texts replaces the cloud's list, in the cloud's form.
        return replace(
            cloud,
            resource=replace(cloud.resource, values=value, has_values=True),
            available=True,
            last_success=observation.observed_at,
            source=SnapshotSource.LOCAL,
            freshness=Freshness.FRESH,
            last_error_category=None,
            consecutive_failures=0,
        )
    if (
        isinstance(value, float)
        and type(cloud.resource.value) is int
        and value.is_integer()
    ):
        # Same reading, same state text: "100" must not become "100.0".
        value = int(value)
    return replace(
        cloud,
        resource=replace(cloud.resource, value=value, has_value=True),
        available=True,
        last_success=observation.observed_at,
        source=SnapshotSource.LOCAL,
        freshness=Freshness.FRESH,
        last_error_category=None,
        consecutive_failures=0,
    )


def _local_value(
    cloud: ResourceSnapshot, observation: LocalObservation, target_id: str
) -> tuple[int | float | str | tuple[str, ...] | None, str | None]:
    """Return the native local value, or the reason it cannot replace the cloud.

    A list of texts (B2) is returned as a tuple of its entries.
    """
    item = from_local_observation(
        observation, connection_id="local", target_id=target_id, current=True
    )
    metadata = cloud.resource.metadata
    if item.status is ValueStatus.UNSUPPORTED:
        return None, "shape_differs"  # a shape this reading cannot take
    if item.status is not ValueStatus.VALID:
        return None, "local_invalid"
    if item.parts:
        return None, "shape_differs"
    if item.resource_type != metadata.resource_type:
        return None, "type_differs"
    cloud_list = cloud_items(cloud.resource)
    if item.items is not None or cloud_list is not None:
        if item.items is None or cloud_list is None:
            return None, "shape_differs"
        # Only entries the cloud declares (else the local answer declares):
        # an unknown entry would change the entity's state or options.
        declared = tuple(str(entry) for entry in metadata.allowed_values) or tuple(
            getattr(observation.data, "allowed_values", ())
        )
        if not declared or any(entry not in declared for entry in item.items):
            return None, "unknown_entry"
        return item.items, None
    if canonical_unit(item.unit) != canonical_unit(metadata.unit):
        return None, "unit_differs"
    value = _native(item.value, metadata.resource_type)
    if value is None:
        return None, "not_convertible"
    if any(value == marker for marker in metadata.numeric_error_values):
        # The cloud declares this number an error marker: never show it.
        return None, "local_invalid"
    return value, None


def _native(value: object, resource_type: str | None) -> int | float | str | None:
    if isinstance(value, bool) or value is None:
        return None
    if resource_type == "floatValue" and isinstance(value, (int, Decimal)):
        return float(value)
    if resource_type == "integerValue" and isinstance(value, int):
        return value
    if resource_type == "stringValue" and isinstance(value, str):
        return value
    return None


# G3: a signal whose entities are all disabled is read at most this often.
UNUSED_SIGNAL_PERIOD = 3600
# G3: signals that keep their pace also without an enabled entity: counters
# and faults (by name), besides the pairs and the installation date.
_COUNTER_SIGNAL = re.compile(
    r"stats\.|count|starts|energy|consumption|hours|runtime|workingtime", re.I
)
_FAULT_SIGNAL = re.compile(r"error|fault|alarm|fail|warn", re.I)


@lru_cache(maxsize=4096)
def signal_slows_when_unused(path: str) -> bool:
    """G3: whether a local signal may be read hourly while every entity that
    shows it is disabled.

    Never for observation pairs (the cloud compares them, counters included),
    counters, faults and the parts of the installation date.
    """
    from .local_only import DURATION, definition_for

    if not path.startswith("/signals/") or pair_for_path(path) is not None:
        return False
    signal = path.removeprefix("/signals/")
    if (
        signal.startswith("SC.InstallationDate.")
        or _COUNTER_SIGNAL.search(signal) is not None
        or _FAULT_SIGNAL.search(signal) is not None
    ):
        return False
    definition = definition_for(path)
    return definition is None or definition.kind != DURATION


@lru_cache(maxsize=4096)
def _paired_reading(path: str) -> bool:
    """Catalog pair that is not a counter (cached: the catalog is constant)."""
    pair = pair_for_path(path)
    return pair is not None and not pair.counter


def paired_cloud_interval(
    snapshot: ConfigurationSnapshot,
    target_id: str,
    path: str,
    *,
    options: dict[str, Any] | None = None,
    scheduled: Callable[[str, str], tuple[bool, timedelta | None]] | None = None,
) -> int | None:
    """Return the cloud polling interval of a paired reading for this target.

    Local polling uses min(local interval, this interval) for paired readings
    that are not counters, so the local source is never polled less often than
    the cloud. It applies only with local values enabled for the target.
    ``scheduled(binding, path)`` reports the interval the cloud actually uses
    (``None`` for static, adjustable or unknown readings and while the cloud is
    not running). Until a running cloud has discovered its readings, the
    shortest interval of its profile applies. ``None`` without a paired reading
    or an enabled cloud binding.
    """
    from .const import PollingProfile
    from .coordinator import POLL_INTERVALS, POLL_INTERVALS_CLOUD_FRIENDLY
    from .resource_catalog import PollGroup

    if not _paired_reading(path):
        return None
    options = options if options is not None else snapshot.to_options()
    target = section(options, "targets").get(target_id, {})
    if target.get("local_values", False) is not True:
        return None  # nothing uses the local value: no extra local load
    binding_options = section(options, "bindings")
    connection_options = section(options, "connections")
    intervals: list[int] = []
    for key, binding in snapshot.bindings.items():
        connection = snapshot.connections.get(binding.connection_id)
        if (
            binding.target_id != target_id
            or connection is None
            or connection.kind != CLOUD_KIND
            or binding_options.get(key, {}).get("enabled") is not True
            or connection_options.get(binding.connection_id, {}).get("enabled")
            is not True
        ):
            continue
        known, interval = scheduled(key, path) if scheduled else (False, None)
        if not known:
            profile = connection_options[binding.connection_id].get("polling_profile")
            table = (
                POLL_INTERVALS_CLOUD_FRIENDLY
                if profile == PollingProfile.CLOUD_FRIENDLY
                else POLL_INTERVALS
            )
            interval = table[PollGroup.FAST]
        if interval is not None:
            intervals.append(int(interval.total_seconds()))
    return min(intervals, default=None)
