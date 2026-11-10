"""Remember discovered resources and search again for parts missing at start.

Discovery runs once per integration load. A start with a partial cloud answer
(single 4xx or unreadable items, a container without its usual references, a
timeout on one root) used to lose that part until the next restart. The
inventory keeps every resource discovered before, per gateway, so a gap is
noticed and searched again on a bounded schedule. Values are stored only to
rebuild the same entities later; they are never shown from here.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .log_helpers import log_failure
from .pointt import Gateway, Resource, ResourceMetadata, ResourceReference
from .pointt.redaction import resource_path_template

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY_SECONDS = 30
# O66: a pure "still seen" confirmation is written at most this often; content
# changes keep SAVE_DELAY_SECONDS. A later write includes held confirmations.
CONFIRM_SAVE_SECONDS = 3600
# A part not seen for this long is no longer expected after a start.
MAX_AGE = timedelta(days=30)
MAX_RESOURCES_PER_GATEWAY = 1024
# Seconds after the start-up discovery: 5, 15 and 60 minutes, then every six
# hours until one day has passed. Afterwards only the next load searches again.
RECOVERY_SCHEDULE = (300, 900, 3600, 21600, 43200, 64800, 86400)
MIN_RECOVERY_GAP_SECONDS = 300
# START-2: a search without an answer moves without using an attempt. After
# this many such postponements (two hours at the minimum gap) the series ends
# until the next start, which still expects the parts.
MAX_RECOVERY_POSTPONEMENTS = 24
_TEMPLATE_LIMIT = 20

_INVENTORIES: HassKey[dict[str, DiscoveryInventory]] = HassKey(
    f"{DOMAIN}.discovery_inventories"
)


def _storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.discovery_inventory.{entry_id}"


# ------------------------------------------------------------------ resources


def metadata_only(resource: Resource) -> Resource:
    """The description of a resource without any value (1A, P49b): type,
    unit, allowed values, limits, step and writable flag."""
    return Resource(path=resource.path, metadata=resource.metadata)


def resource_to_data(resource: Resource) -> dict[str, Any]:
    """Serialize a parsed resource losslessly as JSON."""
    metadata = resource.metadata
    return {
        "path": resource.path,
        "value": resource.value,
        "has_value": resource.has_value,
        "values": list(resource.values),
        "has_values": resource.has_values,
        "metadata": {
            "resource_type": metadata.resource_type,
            "unit": metadata.unit,
            "allowed_values": list(metadata.allowed_values),
            "minimum": metadata.minimum,
            "maximum": metadata.maximum,
            "writable": metadata.writable,
            "numeric_error_values": list(metadata.numeric_error_values),
            "numeric_error_states_valid": metadata.numeric_error_states_valid,
            "below_range_values": list(metadata.below_range_values),
            "step_size": metadata.step_size,
            "step_size_valid": metadata.step_size_valid,
            "used": metadata.used,
            "available": metadata.available,
            "availability_flags_valid": metadata.availability_flags_valid,
        },
        "references": [
            {"path": reference.path, "name": reference.name}
            for reference in resource.references
        ],
        "unknown_fields": sorted(resource.unknown_fields),
        "program_fields": dict(resource.program_fields),
    }


class _Invalid(ValueError):
    """A stored resource does not have the expected shape."""


def _json(value: object) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _Invalid
        return value
    if isinstance(value, list):
        return [_json(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json(item) for key, item in value.items()}
    raise _Invalid


def _optional(value: object, kind: type | tuple[type, ...]) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) and bool not in (
        kind if isinstance(kind, tuple) else (kind,)
    ):
        raise _Invalid
    if not isinstance(value, kind):
        raise _Invalid
    return value


def _number(value: object) -> float | None:
    number = _optional(value, (int, float))
    if number is None:
        return None
    if not math.isfinite(number):
        raise _Invalid
    return float(number)


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise _Invalid
    return value


def _scalars(value: object) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise _Invalid
    items = tuple(_json(item) for item in value)
    if any(isinstance(item, list | dict) for item in items):
        raise _Invalid
    return items


def resource_from_data(data: object) -> Resource:
    """Rebuild a resource; any unexpected shape raises ``ValueError``."""
    if not isinstance(data, Mapping):
        raise _Invalid
    path = data.get("path")
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 512:
        raise _Invalid
    metadata = data.get("metadata")
    if not isinstance(metadata, Mapping):
        raise _Invalid
    errors = _scalars(metadata.get("numeric_error_values"))
    # A4: stored before alpha.3 without it.
    below = _scalars(metadata.get("below_range_values", []))
    if any(
        isinstance(item, bool) or not isinstance(item, int | float)
        for item in (*errors, *below)
    ):
        raise _Invalid
    references = data.get("references")
    if not isinstance(references, list):
        raise _Invalid
    parsed_references = []
    for reference in references:
        if not isinstance(reference, Mapping):
            raise _Invalid
        reference_path = reference.get("path")
        if not isinstance(reference_path, str):
            raise _Invalid
        parsed_references.append(
            ResourceReference(reference_path, _optional(reference.get("name"), str))
        )
    values = data.get("values")
    unknown = data.get("unknown_fields")
    program = data.get("program_fields")
    if not isinstance(values, list) or not isinstance(unknown, list):
        raise _Invalid
    if not all(isinstance(item, str) for item in unknown):
        raise _Invalid
    if not isinstance(program, dict):
        raise _Invalid
    return Resource(
        path=path,
        value=_json(data.get("value")),
        has_value=_flag(data.get("has_value")),
        values=tuple(_json(item) for item in values),
        metadata=ResourceMetadata(
            resource_type=_optional(metadata.get("resource_type"), str),
            unit=_optional(metadata.get("unit"), str),
            allowed_values=_scalars(metadata.get("allowed_values")),
            minimum=_number(metadata.get("minimum")),
            maximum=_number(metadata.get("maximum")),
            writable=_flag(metadata.get("writable")),
            numeric_error_values=errors,
            numeric_error_states_valid=_flag(
                metadata.get("numeric_error_states_valid")
            ),
            below_range_values=below,
            step_size=_number(metadata.get("step_size")),
            step_size_valid=_flag(metadata.get("step_size_valid")),
            used=_optional(metadata.get("used"), bool),
            available=_optional(metadata.get("available"), bool),
            availability_flags_valid=_flag(metadata.get("availability_flags_valid")),
        ),
        references=tuple(parsed_references),
        has_values=_flag(data.get("has_values")),
        unknown_fields=frozenset(unknown),
        program_fields=_json(program),
    )


def _gateway_to_data(gateway: Gateway) -> dict[str, Any]:
    return {
        "device_type": gateway.device_type,
        "model": gateway.model,
        "firmware_version": gateway.firmware_version,
    }


def _gateway_from_data(gateway_id: str, data: object) -> Gateway:
    if not isinstance(data, Mapping):
        raise _Invalid
    return Gateway(
        gateway_id,
        device_type=_optional(data.get("device_type"), str),
        model=_optional(data.get("model"), str),
        firmware_version=_optional(data.get("firmware_version"), str),
    )


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise _Invalid
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise _Invalid from err
    if parsed.tzinfo is None:
        raise _Invalid
    return parsed


# ------------------------------------------------------------------ inventory


@dataclass(slots=True)
class _Seen:
    resource: Resource
    seen: datetime


@dataclass(slots=True)
class GatewayInventory:
    """Resources discovered before for one gateway."""

    gateway: Gateway
    saved_at: datetime
    resources: dict[str, _Seen] = field(default_factory=dict)
    # 1A (P49b): descriptions without values, kept beyond the age limit until
    # the gateway is deselected or forgotten; only templates, never present.
    templates: dict[str, Resource] = field(default_factory=dict)


class DiscoveryInventory:
    """Per config entry: the resources each selected gateway had before."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_VERSION,
            _storage_key(entry_id),
            private=True,
            atomic_writes=True,
        )
        self._gateways: dict[str, GatewayInventory] = {}
        self._loaded = False
        self._confirm_saved_at: float | None = None

    @property
    def store(self) -> Store[dict[str, Any]]:
        return self._store

    async def async_load(self) -> None:
        """Load once; a damaged file starts empty and is not fatal."""
        if self._loaded:
            return
        try:
            stored = await self._store.async_load()
        except Exception as err:
            log_failure(
                _LOGGER,
                logging.WARNING,
                "Saved discovery inventory could not be read",
                error=err,
            )
            stored = None
        if not self._loaded:
            self._gateways = dict(_gateways(stored, datetime.now(UTC)))
            self._loaded = True

    def gateway(self, gateway_id: str) -> GatewayInventory | None:
        return self._gateways.get(gateway_id)

    def resources(self, gateway_id: str) -> dict[str, Resource]:
        """Resources seen for this gateway within the age limit."""
        stored = self._gateways.get(gateway_id)
        if stored is None:
            return {}
        cutoff = datetime.now(UTC) - MAX_AGE
        return {
            path: item.resource
            for path, item in stored.resources.items()
            if item.seen >= cutoff
        }

    def templates(self, gateway_id: str) -> dict[str, Resource]:
        """Descriptions (no values) of every resource seen for this gateway,
        also beyond the age limit. Never a sign that a resource exists."""
        stored = self._gateways.get(gateway_id)
        return dict(stored.templates) if stored is not None else {}

    def keep_templates(self, gateway_ids: Iterable[str]) -> None:
        """Forget the descriptions of gateways that are no longer selected
        (1A); the resources keep their own age limit."""
        keep = set(gateway_ids)
        changed = False
        for gateway_id, stored in self._gateways.items():
            if gateway_id not in keep and stored.templates:
                stored.templates = {}
                changed = True
        if changed:
            self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    @property
    def loaded(self) -> bool:
        return self._loaded

    def remember_gateways(self, gateways: Iterable[Gateway]) -> None:
        """The cloud listed these gateways now (P47), also without resources.

        Listing confirms the gateway itself; its resources age separately by
        the time they were last read successfully.
        """
        now = datetime.now(UTC)
        for gateway in gateways:
            stored = self._gateways.get(gateway.gateway_id)
            if stored is None:
                self._gateways[gateway.gateway_id] = GatewayInventory(gateway, now)
            else:
                stored.gateway = gateway
                stored.saved_at = now
        self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    def confirm(self, gateway_id: str, paths: Iterable[str]) -> None:
        """Stored resources that a normal poll read successfully are current."""
        stored = self._gateways.get(gateway_id)
        if stored is None:
            return
        now = datetime.now(UTC)
        confirmed = False
        for path in paths:
            if (item := stored.resources.get(path)) is not None:
                item.seen = now
                confirmed = True
        if not confirmed:
            return
        stored.saved_at = now
        # O66: ages count in memory at once; the file follows at most hourly.
        moment = monotonic()
        if (
            self._confirm_saved_at is None
            or moment - self._confirm_saved_at >= CONFIRM_SAVE_SECONDS
        ):
            self._confirm_saved_at = moment
            self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    def seen(self, gateway_id: str) -> dict[str, datetime]:
        stored = self._gateways.get(gateway_id)
        return (
            {path: item.seen for path, item in stored.resources.items()}
            if stored is not None
            else {}
        )

    def startup_gateways(
        self, gateway_ids: Iterable[str]
    ) -> tuple[Gateway, ...] | None:
        """Every selected gateway as stored, or ``None`` if one is unknown or old."""
        cutoff = datetime.now(UTC) - MAX_AGE
        gateways = []
        for gateway_id in sorted(gateway_ids):
            stored = self._gateways.get(gateway_id)
            if stored is None or stored.saved_at < cutoff:
                return None
            gateways.append(stored.gateway)
        return tuple(gateways)

    def startup_expiry(self, gateway_id: str) -> datetime | None:
        """When ``startup_gateways`` stops accepting this gateway (P49b)."""
        stored = self._gateways.get(gateway_id)
        return stored.saved_at + MAX_AGE if stored is not None else None

    def age_seconds(self, gateway_id: str) -> int | None:
        stored = self._gateways.get(gateway_id)
        if stored is None:
            return None
        return max(0, int((datetime.now(UTC) - stored.saved_at).total_seconds()))

    def record(self, gateway: Gateway, resources: Mapping[str, Resource]) -> None:
        """Add or refresh discovered resources; keep parts missing right now."""
        if not resources:
            return
        now = datetime.now(UTC)
        cutoff = now - MAX_AGE
        previous = self._gateways.get(gateway.gateway_id)
        kept = {
            path: item
            for path, item in (previous.resources if previous else {}).items()
            if item.seen >= cutoff and path not in resources
        }
        current = {path: _Seen(resource, now) for path, resource in resources.items()}
        merged = {**kept, **current}
        if len(merged) > MAX_RESOURCES_PER_GATEWAY:
            # Keep the most recently seen parts.
            ordered = sorted(
                merged.items(), key=lambda item: item[1].seen, reverse=True
            )
            merged = dict(ordered[:MAX_RESOURCES_PER_GATEWAY])
        templates = _bounded_templates(
            previous.templates if previous else {},
            {path: metadata_only(resource) for path, resource in resources.items()},
        )
        self._gateways[gateway.gateway_id] = GatewayInventory(
            gateway, now, merged, templates
        )
        self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    def forget(self, gateway_id: str, paths: Iterable[str]) -> None:
        """Stop expecting parts that a full search series did not find."""
        stored = self._gateways.get(gateway_id)
        if stored is None:
            return
        paths = tuple(paths)
        removed = [path for path in paths if stored.resources.pop(path, None)]
        removed += [path for path in paths if stored.templates.pop(path, None)]
        if removed:
            self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    def retain(self, gateway_ids: Iterable[str]) -> None:
        """Forget gateways that are no longer selected."""
        keep = set(gateway_ids)
        removed = [key for key in self._gateways if key not in keep]
        for key in removed:
            del self._gateways[key]
        if removed:
            self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    def drop(self, gateway_id: str) -> None:
        """Forget one gateway, for example after it was exchanged (P50)."""
        if self._gateways.pop(gateway_id, None) is not None:
            self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    async def async_remove(self) -> None:
        self._gateways.clear()
        await self._store.async_remove()

    def _data(self) -> dict[str, Any]:
        return {
            "gateways": {
                gateway_id: {
                    "saved_at": stored.saved_at.isoformat(),
                    "gateway": _gateway_to_data(stored.gateway),
                    "resources": {
                        path: {
                            "seen": item.seen.isoformat(),
                            "resource": resource_to_data(item.resource),
                        }
                        for path, item in stored.resources.items()
                    },
                    # Ignored by versions before 1A (P49b).
                    "templates": {
                        path: resource_to_data(item)
                        for path, item in stored.templates.items()
                    },
                }
                for gateway_id, stored in self._gateways.items()
            }
        }


def _gateways(stored: object, now: datetime) -> Iterable[tuple[str, GatewayInventory]]:
    if not isinstance(stored, Mapping):
        return
    gateways = stored.get("gateways")
    if not isinstance(gateways, Mapping):
        return
    cutoff = now - MAX_AGE
    for gateway_id, data in gateways.items():
        if not isinstance(gateway_id, str) or not isinstance(data, Mapping):
            continue
        try:
            gateway = _gateway_from_data(gateway_id, data.get("gateway"))
            saved_at = _time(data.get("saved_at"))
        except ValueError:
            continue
        resources: dict[str, _Seen] = {}
        rows = data.get("resources")
        if not isinstance(rows, Mapping):
            continue
        described: dict[str, Resource] = {}
        for path, row in list(rows.items())[:MAX_RESOURCES_PER_GATEWAY]:
            if not isinstance(row, Mapping):
                continue
            try:
                resource = resource_from_data(row.get("resource"))
                seen = _time(row.get("seen"))
            except ValueError:
                continue  # one damaged row does not discard the others
            if resource.path != path:
                continue
            described[path] = metadata_only(resource)
            if seen >= cutoff:
                resources[path] = _Seen(resource, seen)
        stored_templates: dict[str, Resource] = {}
        raw_templates = data.get("templates")
        if isinstance(raw_templates, Mapping):
            for path, row in list(raw_templates.items())[:MAX_RESOURCES_PER_GATEWAY]:
                try:
                    resource = resource_from_data(row)
                except ValueError:
                    continue
                if resource.path == path:
                    stored_templates[path] = metadata_only(resource)
        yield (
            gateway_id,
            GatewayInventory(
                gateway,
                saved_at,
                resources,
                _bounded_templates(stored_templates, described),
            ),
        )


def _bounded_templates(
    older: Mapping[str, Resource], newer: Mapping[str, Resource]
) -> dict[str, Resource]:
    """Newer descriptions replace older ones; at most one per stored part."""
    merged = {path: item for path, item in older.items() if path not in newer}
    merged.update(newer)
    if len(merged) > MAX_RESOURCES_PER_GATEWAY:
        keep = list(newer)[:MAX_RESOURCES_PER_GATEWAY]
        rest = [path for path in merged if path not in newer]
        keep += rest[: MAX_RESOURCES_PER_GATEWAY - len(keep)]
        merged = {path: merged[path] for path in keep}
    return merged


def discovery_inventory(hass: HomeAssistant, entry_id: str) -> DiscoveryInventory:
    """Shared per entry, so a reload keeps pending writes."""
    inventories = hass.data.setdefault(_INVENTORIES, {})
    if (inventory := inventories.get(entry_id)) is None:
        inventory = inventories[entry_id] = DiscoveryInventory(hass, entry_id)
    return inventory


async def async_remove_discovery_inventory(hass: HomeAssistant, entry_id: str) -> None:
    inventory = hass.data.get(_INVENTORIES, {}).pop(entry_id, None)
    if inventory is None:
        inventory = DiscoveryInventory(hass, entry_id)
    await inventory.async_remove()


# ------------------------------------------------------------------- recovery

# Results that say nothing about whether a root exists.
_TRANSIENT = frozenset(
    {
        "timeout",
        "service_unavailable",
        "transport_error",
        "malformed",
        "unavailable",
        "request_failed",
    }
)


def is_transient_result(category: str | None) -> bool:
    if category is None:
        return False
    if category in _TRANSIENT:
        return True
    return category.startswith("http_5") and len(category) == 8


def recovery_roots(paths: Iterable[str]) -> tuple[str, ...]:
    """The shallowest missing paths: one search per missing subtree."""
    roots: list[str] = []
    for path in sorted(set(paths), key=lambda item: (item.count("/"), item)):
        if not any(path == root or path.startswith(f"{root}/") for root in roots):
            roots.append(path)
    return tuple(roots)


class DiscoveryRecovery:
    """Bounded search for parts that were discovered before but are missing."""

    def __init__(self) -> None:
        self.state = "none"
        self.missing: dict[str, str] = {}
        self.reference_resources = 0
        self.initially_missing = 0
        self.recovered = 0
        self.attempts = 0
        self.postponed = 0
        self.last_result: str | None = None
        self._started = 0.0
        self._next_due: float | None = None

    def plan(
        self,
        missing: Mapping[str, str],
        reference: int,
        now: float,
        *,
        announce: bool = True,
    ) -> None:
        """Start a new series after the start-up discovery.

        ``announce`` false: the caller keeps some of the parts first and then
        reports only the remaining ones (``announce``), as a requested or a
        firmware discovery does (START-5).
        """
        self.missing = dict(missing)
        self.reference_resources = reference
        self.initially_missing = len(missing)
        self.recovered = 0
        self.attempts = 0
        self.postponed = 0
        self.last_result = None
        self._started = now
        if not missing:
            self.state = "none" if reference == 0 else "complete"
            self._next_due = None
            return
        self.state = "pending"
        self._next_due = now + RECOVERY_SCHEDULE[0]
        if announce:
            self.announce()

    def announce(self) -> None:
        """Warn about the parts the planned search looks for, if any."""
        if self.state != "pending" or not self.missing:
            return
        if self.reference_resources:
            message = (
                "%d previously known PointT resources are missing after discovery "
                "(%s); searching again in %d minutes"
            )
        else:
            message = (
                "%d PointT root resources failed temporarily during discovery "
                "(%s); searching again in %d minutes"
            )
        _LOGGER.warning(
            message,
            len(self.missing),
            ", ".join(self.templates()),
            RECOVERY_SCHEDULE[0] // 60,
        )

    def due(self, now: float) -> bool:
        return self._next_due is not None and now >= self._next_due

    def roots(self) -> tuple[str, ...]:
        return recovery_roots(self.missing)

    def give_up(self, result: str) -> None:
        """End the series at once when a search cannot succeed."""
        self.attempts += 1
        self.last_result = result
        self.state = "given_up"
        self._next_due = None
        _LOGGER.warning(
            "%d PointT resources are missing and cannot be searched (%s): %s",
            len(self.missing),
            result,
            ", ".join(self.templates()),
        )

    def postpone(self, now: float | None = None) -> None:
        """A rate limit or no answer moves the search; no attempt is used.

        Without an own pause (rate limit: the coordinator's brake applies), the
        search is due again at once; after no answer it waits the minimum gap.
        After MAX_RECOVERY_POSTPONEMENTS the series ends (START-2): the parts
        stay expected, so the next start searches for them again.
        """
        self.postponed += 1
        if self.postponed >= MAX_RECOVERY_POSTPONEMENTS:
            self.last_result = "unanswered"
            self.state = "given_up"
            self._next_due = None
            _LOGGER.warning(
                "%d PointT resources are still missing and %d searches got no "
                "answer; searching again after the next start (%s)",
                len(self.missing),
                self.postponed,
                ", ".join(self.templates()),
            )
            return
        if now is not None and self._next_due is not None:
            self._next_due = max(self._next_due, now + MIN_RECOVERY_GAP_SECONDS)

    def finish(self, found: Iterable[str], now: float, *, result: str) -> None:
        """Record one attempt and plan the next one, or give up."""
        found_paths = set(found)
        recovered = [path for path in self.missing if path in found_paths]
        for path in recovered:
            del self.missing[path]
        self.recovered += len(recovered)
        self.attempts += 1
        self.last_result = result
        if not self.missing:
            self.state = "recovered"
            self._next_due = None
            _LOGGER.info(
                "All %d missing PointT resources were found again after %d search(es)",
                self.initially_missing,
                self.attempts,
            )
            return
        if self.attempts >= len(RECOVERY_SCHEDULE):
            self.state = "given_up"
            self._next_due = None
            _LOGGER.warning(
                "%d PointT resources are still missing after %d searches (%s); "
                "they are no longer expected at the next start",
                len(self.missing),
                self.attempts,
                ", ".join(self.templates()),
            )
            return
        self._next_due = max(
            self._started + RECOVERY_SCHEDULE[self.attempts],
            now + MIN_RECOVERY_GAP_SECONDS,
        )

    def templates(self) -> list[str]:
        templates = sorted({resource_path_template(path) for path in self.missing})
        if len(templates) > _TEMPLATE_LIMIT:
            return [*templates[:_TEMPLATE_LIMIT], "…"]
        return templates

    def diagnostics(self, now: float) -> dict[str, object]:
        reasons: dict[str, int] = {}
        for reason in self.missing.values():
            reasons[reason] = reasons.get(reason, 0) + 1
        return {
            "state": self.state,
            "reference_resources": self.reference_resources,
            "missing_at_start": self.initially_missing,
            "missing": len(self.missing),
            "missing_templates": self.templates(),
            "missing_reasons": dict(sorted(reasons.items())),
            "recovered": self.recovered,
            "attempts": self.attempts,
            "attempts_planned": len(RECOVERY_SCHEDULE),
            "postponed": self.postponed,
            "last_result": self.last_result,
            "next_attempt_in_seconds": (
                max(0, math.ceil(self._next_due - now))
                if self._next_due is not None
                else None
            ),
        }
