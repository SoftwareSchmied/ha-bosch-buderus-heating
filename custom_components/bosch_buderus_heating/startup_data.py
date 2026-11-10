"""Bounded, entry-local recovery of nonessential startup resources."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType

from .pointt import Resource, ResourceMetadata

LICENSE_PATH = "/gateway/thirdPartyLicenseInformation"
CIRCUIT_COUNT_PATH = "/heatSources/numberOfRefrigerantCircuitsInstalled"
STARTUP_BUDGET_SECONDS = 30.0
RECOVERY_INTERVAL_SECONDS = 60.0
RETRY_DELAYS = (60.0, 120.0, 300.0, 900.0, 1800.0)
MAX_STARTUP_PATHS = 512
_DIAGNOSTIC_PATHS = frozenset(
    {"/gateway/tosAccepted", "/system/dateTime", "/gateway/wifi/mac"}
)
_DEVICE_PATH = re.compile(
    r"^/devices/(?!list/)[^/]+/(productName|inclusionTime|sgtin)$"
)
_TERMINAL_RESULTS = frozenset({"success", "normal_resource", "unsupported"})
_KNOWN_PRODUCT_NAMES = frozenset({"MX300", "MX400", "K30", "K30RF", "K40", "K40RF"})


def safe_technical_value(resource: Resource) -> str | int | None:
    """Export only reviewed technical values, never arbitrary device text."""
    if (
        not resource.has_value
        or resource.references
        or resource.metadata.writable
        or resource.has_values
        or resource.values
    ):
        return None
    value = resource.value
    if resource.path == CIRCUIT_COUNT_PATH and is_static_startup_resource(resource):
        return int(value) if isinstance(value, int | float) else None
    tail = resource.path.rsplit("/", 1)[-1]
    if not isinstance(value, str):
        return None
    if tail in {"productName", "model"} and value in _KNOWN_PRODUCT_NAMES:
        return value
    if tail == "brand" and value.lower() in {"bosch", "buderus", "nefit", "worcester"}:
        return value.lower()
    if tail in {"versionFirmware", "versionHardware"} and re.fullmatch(
        r"[0-9]{1,3}(?:\.[0-9]{1,3}){1,2}", value
    ):
        return value
    return None


def is_startup_path(path: str) -> bool:
    """Recognize only explicitly reviewed leaf paths, never whole subtrees."""
    return (
        path == CIRCUIT_COUNT_PATH
        or path in _DIAGNOSTIC_PATHS
        or _DEVICE_PATH.fullmatch(path) is not None
    )


def is_startup_entity_path(path: str) -> bool:
    """Identify the existing scalar sensors affected by startup-only reads."""
    match = _DEVICE_PATH.fullmatch(path)
    return path == CIRCUIT_COUNT_PATH or (
        match is not None and match.group(1) == "productName"
    )


def is_static_startup_resource(resource: Resource) -> bool:
    """Require a read-only leaf with its expected, usable scalar schema."""
    if (
        not is_startup_path(resource.path)
        or resource.references
        or resource.metadata.writable
        or not resource.has_value
        or resource.has_values
        or resource.values
    ):
        return False
    value = resource.value
    resource_type = resource.metadata.resource_type
    if resource.path == CIRCUIT_COUNT_PATH:
        return (
            resource_type in (None, "floatValue", "integerValue")
            and resource.metadata.unit in (None, "", "count")
            and isinstance(value, int | float)
            and not isinstance(value, bool)
            and 0 <= value <= 65535
            and math.isfinite(value)
            and float(value).is_integer()
        )
    if resource.path == "/gateway/tosAccepted" and isinstance(value, bool):
        return resource_type in (None, "booleanValue")
    return (
        resource_type in (None, "stringValue", "dateTime")
        and isinstance(value, str)
        and bool(value.strip())
    )


def startup_placeholder(path: str) -> Resource:
    """Describe an already registered sensor without inventing its value."""
    if not is_startup_entity_path(path):
        raise ValueError("Only catalogued startup sensors can be restored")
    return Resource(
        path,
        metadata=ResourceMetadata(
            resource_type="floatValue" if path == CIRCUIT_COUNT_PATH else "stringValue"
        ),
    )


@dataclass(frozen=True, slots=True)
class StartupRead:
    """Immutable state of one logical read and its own retry deadline."""

    path: str
    depth: int
    advertised: bool = False
    registered: bool = False
    active: bool = True
    attempts: int = 0
    failures: int = 0
    result: str = "pending"
    next_attempt: float = 0.0
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    resource: Resource | None = None

    @property
    def pending(self) -> bool:
        """A successful read is never put back into periodic polling."""
        return self.active and self.result not in (
            *_TERMINAL_RESULTS,
            "resource_limit",
            "depth_limit",
        )

    def start(self, now: datetime) -> StartupRead:
        """Count a logical call separately from the transport's HTTP attempts."""
        return replace(
            self, attempts=self.attempts + 1, last_attempt=now, result="reading"
        )

    def failed(self, category: str, now: float) -> StartupRead:
        """Retain temporary failures without exhausting a lifetime retry budget."""
        failures = self.failures + 1
        if category in {"http_403", "http_404", "http_406"}:
            if not self.registered and not (
                self.advertised and is_startup_entity_path(self.path)
            ):
                return replace(self, failures=failures, result="unsupported")
            delay = RETRY_DELAYS[-1]
        else:
            delay = RETRY_DELAYS[min(failures - 1, len(RETRY_DELAYS) - 1)]
        return replace(
            self, failures=failures, result=category, next_attempt=now + delay
        )

    def received(
        self, resource: Resource, now: float, timestamp: datetime
    ) -> StartupRead:
        """Accept real values; route structural or writable surprises normally."""
        if (
            not is_startup_path(resource.path)
            or resource.references
            or resource.metadata.writable
        ):
            result = "normal_resource"
        elif is_static_startup_resource(resource):
            result = "success"
        else:
            return self.failed("invalid_value", now)
        return replace(
            self,
            result=result,
            resource=resource,
            last_success=timestamp,
            failures=0,
            next_attempt=0.0,
        )


class StartupGatewayData:
    """Startup values and pending work for exactly one selected gateway."""

    def __init__(
        self, *, maximum_resources: int = MAX_STARTUP_PATHS, maximum_depth: int = 8
    ) -> None:
        self._reads: dict[str, StartupRead] = {}
        self._requested: set[str] = set()
        self._maximum_resources = maximum_resources
        self._maximum_depth = maximum_depth
        self._healthy = False
        self._omitted_license = False
        self._budget_exhaustions = 0

    @property
    def reads(self) -> Mapping[str, StartupRead]:
        """Expose immutable read states without allowing queue changes."""
        return MappingProxyType(self._reads)

    @property
    def requested(self) -> frozenset[str]:
        """Expose the distinct paths charged to the shared discovery budget."""
        return frozenset(self._requested)

    @property
    def healthy(self) -> bool:
        """Whether the gateway's last functional cycle permits recovery."""
        return self._healthy

    @property
    def omitted_license(self) -> bool:
        """Whether discovery encountered the unused license endpoint."""
        return self._omitted_license

    @property
    def budget_exhaustions(self) -> int:
        """Count expired supplementary time budgets."""
        return self._budget_exhaustions

    def record_main_cycle(self, *, successful: bool) -> None:
        """Only a functional cycle may change recovery eligibility."""
        self._healthy = successful

    def omit_license(self) -> None:
        """Record a skipped endpoint without reserving or requesting it."""
        self._omitted_license = True

    def begin_discovery(
        self,
        *,
        maximum_resources: int | None = None,
        maximum_depth: int | None = None,
    ) -> None:
        """Keep successful values across setup retries, but recheck membership."""
        if maximum_resources is not None:
            self._maximum_resources = maximum_resources
        if maximum_depth is not None:
            self._maximum_depth = maximum_depth
        self._healthy = False
        for path, previous in self._reads.items():
            read = replace(previous, active=previous.registered)
            if read.result == "normal_resource":
                # A writable or structural surprise follows normal discovery on
                # the next setup attempt, rather than reusing an old live value.
                read = replace(
                    read,
                    resource=None,
                    last_success=None,
                    result="pending",
                    next_attempt=0.0,
                )
            self._reads[path] = read

    def reserve_discovery(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        """Charge core discovery to the same distinct-path budget as recovery."""
        accepted = []
        for path in paths:
            if (
                path not in self._requested
                and len(self._requested) >= self._maximum_resources
            ):
                continue
            self._requested.add(path)
            accepted.append(path)
        return tuple(accepted)

    def schedule(
        self,
        path: str,
        depth: int,
        *,
        advertised: bool = False,
        registered: bool = False,
        referenced_child: bool = False,
    ) -> None:
        """Deduplicate reported or previously registered, reviewed paths."""
        if not is_startup_path(path) and not referenced_child:
            return
        read = self._reads.get(path)
        if read is None:
            if len(self._reads) >= MAX_STARTUP_PATHS:
                return
            read = StartupRead(path, depth)
        read = replace(
            read,
            depth=min(depth, read.depth),
            advertised=read.advertised or advertised,
            registered=read.registered or registered,
            active=True,
        )
        if read.depth > self._maximum_depth and read.attempts == 0:
            read = replace(read, result="depth_limit")
        elif read.result == "depth_limit":
            read = replace(read, result="pending")
        self._reads[path] = read

    def withdraw(self, unwanted: Callable[[str], bool]) -> tuple[str, ...]:
        """C8: drop queued reads not attempted yet that a gateway profile
        found later does not need; returns the dropped paths."""
        dropped = tuple(
            path
            for path, read in self._reads.items()
            if read.attempts == 0 and not read.registered and unwanted(path)
        )
        for path in dropped:
            del self._reads[path]
        return dropped

    def due(self, now: float, *, limit: int = 30) -> tuple[str, ...]:
        """Select the oldest due reads without enlarging discovery's path budget."""
        remaining = max(0, self._maximum_resources - len(self._requested))
        paths: list[str] = []
        for read in sorted(
            self._reads.values(),
            key=lambda item: (is_startup_path(item.path), item.next_attempt),
        ):
            if not read.pending or read.next_attempt > now:
                continue
            if read.path not in self._requested:
                if remaining == 0:
                    self._reads[read.path] = replace(read, result="resource_limit")
                    continue
                remaining -= 1
            paths.append(read.path)
            if len(paths) >= limit:
                break
        return tuple(paths)

    def start(self, paths: tuple[str, ...], timestamp: datetime) -> None:
        """Reserve actual requests only when a batch is about to be sent."""
        self._requested.update(paths)
        for path in paths:
            self._reads[path] = self._reads[path].start(timestamp)

    def fail(self, paths: tuple[str, ...], category: str, now: float) -> None:
        """Apply a completed failure only to paths in the attempted batch."""
        for path in paths:
            self._reads[path] = self._reads[path].failed(category, now)

    def receive(self, resource: Resource, now: float, timestamp: datetime) -> bool:
        """Store a requested resource and report whether its value was accepted."""
        read = self._reads[resource.path].received(resource, now, timestamp)
        self._reads[resource.path] = read
        return read.result in {"success", "normal_resource"}

    def budget_expired(self, now: float, *, in_flight: bool = False) -> None:
        """Leave unsent work pending without falsely counting a failed request."""
        deferred = False
        for path, read in self._reads.items():
            if read.pending and read.result != "reading" and read.next_attempt <= now:
                self._reads[path] = replace(
                    read,
                    result="budget_wait",
                    next_attempt=now + RECOVERY_INTERVAL_SECONDS,
                )
                deferred = True
        self._budget_exhaustions += in_flight or deferred

    def cached_reads(self) -> tuple[StartupRead, ...]:
        """Return confirmed values together with their actual read timestamps."""
        return tuple(
            read
            for read in self._reads.values()
            if read.active
            and read.resource is not None
            and read.result in _TERMINAL_RESULTS
        )

    def registered_paths(self) -> tuple[str, ...]:
        """Return exact paths whose existing sensor identities must be retained."""
        return tuple(path for path, read in self._reads.items() if read.registered)

    def tracks(self, path: str) -> bool:
        """Whether this resource is part of supplementary recovery."""
        return path in self._reads


class StartupSession:
    """Share recovery fairness and cadence within a single config entry."""

    def __init__(
        self,
        selected_ids: tuple[str, ...],
        *,
        options: Mapping[str, object] | None = None,
    ) -> None:
        self._selected_ids = tuple(sorted(selected_ids))
        self._options = dict(options or {})
        self._gateways: dict[str, StartupGatewayData] = {}
        self._last_round: dict[str, float] = {}
        self._setup_complete = False
        self._closed = False
        self._next_round = 0.0
        self._busy = False
        self._remove_state_listener: Callable[[], None] | None = None

    @property
    def selected_ids(self) -> tuple[str, ...]:
        """Return the gateway selection this cache belongs to."""
        return self._selected_ids

    @property
    def setup_complete(self) -> bool:
        """Whether setup has completed and periodic recovery may run."""
        return self._setup_complete

    @property
    def closed(self) -> bool:
        """Whether an explicit reset or authentication failure ended this session."""
        return self._closed

    @property
    def busy(self) -> bool:
        """Whether a gateway currently owns the entry's recovery slot."""
        return self._busy

    def can_resume(
        self, selected_ids: tuple[str, ...], options: Mapping[str, object]
    ) -> bool:
        """Reuse only an unfinished setup with the same selection and options."""
        return (
            not self._setup_complete
            and not self._closed
            and self._selected_ids == tuple(sorted(selected_ids))
            and self._options == options
        )

    def mark_setup_complete(self) -> None:
        """Enable normal recovery after platforms have been loaded."""
        self._setup_complete = True

    def close(self) -> None:
        """Prevent further recovery or reuse after reset or lost authentication."""
        self._closed = True

    def attach_state_listener(self, register: Callable[[], Callable[[], None]]) -> None:
        """Observe entry lifecycle once, including across automatic setup retries."""
        if self._remove_state_listener is None:
            self._remove_state_listener = register()

    def detach(self) -> None:
        """Remove lifecycle observation when replaced or successfully unloaded."""
        if self._remove_state_listener is not None:
            self._remove_state_listener()
            self._remove_state_listener = None

    def gateway(self, gateway_id: str) -> StartupGatewayData:
        """Never share startup results with another selected gateway."""
        return self._gateways.setdefault(gateway_id, StartupGatewayData())

    def claim(self, gateway_id: str, now: float) -> bool:
        """Allow one bounded recovery batch per entry cadence, rotating gateways."""
        if (
            not self._setup_complete
            or self._closed
            or self._busy
            or now < self._next_round
        ):
            return False
        eligible = [
            (self._last_round.get(key, -1.0), key)
            for key, state in self._gateways.items()
            if state.healthy and state.due(now, limit=1)
        ]
        if not eligible or min(eligible)[1] != gateway_id:
            return False
        self._busy = True
        self._last_round[gateway_id] = now
        self._next_round = now + RECOVERY_INTERVAL_SECONDS
        return True

    def release(self) -> None:
        """Always release the entry reservation, including cancellation."""
        self._busy = False
