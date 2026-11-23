"""The plant model of a gateway: its inputs, its result and its stored row.

This adapter reads what the plant reports and hands it to the pure rules of
``plant_detection``:

* the component lists of the cloud (``/system/info``) and of the local
  access (``/system/basicInfo``), parsed once per answer
  (``component_rows``);
* the type values (``/system/type``, the heat source types, the heat pump
  types, ``/system/bus``, the outdoor status of a hybrid control) from the
  cloud's data or its resources, including the values kept in the
  discovery inventory, else from the local readings. A reading is used
  whether or not it is still current: an outage never changes an input;
* the hybrid evidence the integration used before
  (``local_only.hybrid_evidence``), so a hybrid system stays one;
* the gateway class and its firmware.

The result is kept per gateway in one row ``<gateway>:plant`` of the
saved calculated values (``derived_store``): the component lists of both
sources, the inputs, the gate of the hybrid manager rule, the result, and
the results of targeted reads (``probes``, filled later). The row is read
before the first result is worked out and written only when its content
changes. An input that is missing now is taken from the row; only a new
reading that says something else changes the result. Serial numbers are
never part of it; versions are kept in the row and shown in the
diagnostics only, never in attributes or the log.

The display source of the components follows the heat generator sensor:
one current answer alone is shown; when both agree (the gateway's own rows
are not compared) the source shown last stays, and the longer list wins a
change of source; when they disagree, the answer that matches the one shown
last stays, else the newer one.

Public functions for the other parts of the integration:
``track_plant``, ``current_plant``, ``components``, ``describe``,
``set_probe_results``, ``plant_diagnostics``. Without a result (``None``)
everything behaves as before the plant model.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from weakref import WeakKeyDictionary, ref

from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .component_rows import (
    ComponentRow,
    clean_version,
    parse_cloud_rows,
    parse_local_rows,
    rows_from_lists,
)
from .derived_store import DerivedStore, derived_store
from .plant_detection import (
    CONFLICT_CODES,
    EVIDENCE_CODES,
    FUELS,
    MODEL_VERSION,
    PLANT_KINDS,
    Gate,
    PlantInputs,
    PlantResult,
    platform_of,
    resolve,
)
from .system_components import (
    BEHAVIOUR_BASES,
    ComponentTable,
    is_gateway_row,
    load_table,
)

if TYPE_CHECKING:
    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .platform_scope import DiscoveryLifecycle

_LOGGER = logging.getLogger(__name__)

CLOUD_PATH: Final = "/system/info"
LOCAL_PATH: Final = "/system/basicInfo"
SYSTEM_TYPE_PATH: Final = "/system/type"
BUS_PATH: Final = "/system/bus"
OUTDOOR_STATUS_PATH: Final = "/heatSources/hybrid/outdoorStatus"
HEAT_SOURCES_TYPE_PATH: Final = "/heatSources/type"
FIRMWARE_PATH: Final = "/gateway/versionFirmware"
_HEAT_SOURCE_TYPE: Final = re.compile(r"/heatSources/(hs[0-9]{1,3})/type")
_HEAT_PUMP_TYPE: Final = re.compile(r"/heatSources(?:/(hs[0-9]{1,3}))?/heatPumpType")
_SAFE: Final = re.compile(r"[A-Za-z0-9_.-]{1,48}")
# The key of the heat pump type of ``/heatSources`` itself.
CENTRAL: Final = "heatSources"

ROW_SUFFIX: Final = ":plant"
ROW_VERSION: Final = 1
SOURCES: Final = ("cloud", "local")
REMOVED_KEPT: Final = timedelta(days=30)
MAX_ALLOWED_VALUES: Final = 16
MAX_PROBES: Final = 48
MAX_COMPONENT_ENTRIES: Final = 128
_MAX_HEAT_SOURCES: Final = 16
_PROBE_TEXT: Final = re.compile(r"[A-Za-z0-9_./{}%:-]{1,96}")

_TRACKERS: WeakKeyDictionary[object, PlantTracker] = WeakKeyDictionary()
# Results of targeted reads handed over before the gateway's model started
# (the first discovery runs before the platforms are set up).
_PENDING_PROBES: WeakKeyDictionary[object, tuple[Mapping[str, object], ...]] = (
    WeakKeyDictionary()
)


def safe_token(value: object) -> str | None:
    """A reported value as a safe token, ``other`` for anything else."""
    if value is None:
        return None
    if isinstance(value, str) and _SAFE.fullmatch(value):
        return value
    return "other"


def row_key(gateway_id: str) -> str:
    return f"{gateway_id}{ROW_SUFFIX}"


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt_util.parse_datetime(value)
    except ValueError, TypeError:  # a damaged stored time counts as missing
        return None


# ------------------------------------------------------------------ inputs


@dataclass(frozen=True, slots=True)
class SourceRows:
    """The component list of one source."""

    source: str
    rows: tuple[ComponentRow, ...]
    observed_at: datetime | None
    # Read in this run (``False``: taken from the stored row).
    current: bool


@dataclass(frozen=True, slots=True)
class ShownComponents:
    """The components shown for a gateway and where they come from."""

    source: str | None
    rows: tuple[ComponentRow, ...]
    restored: bool
    table_version: int | None


@dataclass(frozen=True, slots=True)
class ComponentDescription:
    """What the table says about one row; names are for display only."""

    place: str | None
    place_name: str | None
    type_name: str | None
    role: str | None
    basis: str | None
    gateway: bool


def describe(
    row: ComponentRow, table: ComponentTable | None = None
) -> ComponentDescription:
    """Place key, names, role and basis of a row from the component table.

    A role is given only for entries whose basis is not a name.
    """
    table = load_table() if table is None else table
    place = table.place(row.tok) if table is not None and row.tok is not None else None
    kind = (
        table.component(row.type_id)
        if table is not None and row.type_id is not None
        else None
    )
    role = (
        table.role(row.type_id, row.tok)
        if table is not None and row.type_id is not None
        else None
    )
    return ComponentDescription(
        place.key if place is not None else None,
        (place.name or None) if place is not None else None,
        (kind.name or None) if kind is not None else None,
        role.role if role is not None and role.basis in BEHAVIOUR_BASES else None,
        kind.basis if kind is not None else None,
        is_gateway_row(row.type_id, row.tok),
    )


@dataclass(frozen=True, slots=True)
class _Gathered:
    """What the plant reports now; ``None`` fields were not read."""

    cloud: SourceRows | None
    local: SourceRows | None
    system_type: str | None
    heat_source_types: tuple[tuple[str, str], ...]
    heat_sources_type: str | None
    heat_pump_types: tuple[tuple[str, str], ...]
    bus: str | None
    outdoor_status: str | None
    allowed_values: tuple[tuple[str, tuple[str, ...]], ...]
    hybrid_evidence: tuple[str, ...] | None
    gateway_class: str | None
    gateway_firmware: str | None
    platform: str | None


def gateway_class(gateway: Any) -> str:
    """The class of a gateway, as the diagnostics name it."""
    from .water_softener import is_water_softener

    if is_water_softener(gateway, {}):
        return "water_softener_gateway"
    combined = " ".join(
        value
        for value in (
            getattr(gateway, "device_type", None),
            getattr(gateway, "model", None),
        )
        if isinstance(value, str) and value
    ).upper()
    for known in ("MX300", "MX400", "K30RF", "K30", "K40RF", "K40"):
        if known in combined:
            return known.lower()
    if "HEATPUMP" in combined or "HEAT PUMP" in combined:
        return "heat_pump_gateway"
    return "heating_gateway"


def _platform(coordinator: Any, klass: str) -> str:
    from .device import is_rrc_plant
    from .icom_profile import is_icom_gateway

    if is_icom_gateway(getattr(coordinator, "gateway", None)):
        return "icom"
    try:
        if is_rrc_plant(coordinator):
            return "rrc"
    except Exception:  # an incomplete view is no room controller
        _LOGGER.debug("Room controller check not possible", exc_info=True)
    return platform_of(klass, None) or "other"


class _Readings:
    """The readings of one coordinator, kept by the identity of their
    sources so that an update pass works each answer out once."""

    def __init__(self) -> None:
        self._cloud: tuple[object, tuple[ComponentRow, ...]] | None = None
        self._local: tuple[object, tuple[ComponentRow, ...]] | None = None
        self._paths: tuple[tuple[int, int], tuple[str, ...]] | None = None
        self.parsed = 0

    def type_paths(self, resources: Mapping[str, Any]) -> tuple[str, ...]:
        key = (id(resources), len(resources))
        if self._paths is not None and self._paths[0] == key:
            return self._paths[1]
        found = tuple(
            sorted(
                path
                for path in resources
                if _HEAT_SOURCE_TYPE.fullmatch(path) or _HEAT_PUMP_TYPE.fullmatch(path)
            )
        )
        self._paths = (key, found)
        return found

    def cloud_rows(
        self, source: object, values: Sequence[object]
    ) -> tuple[ComponentRow, ...]:
        if self._cloud is not None and self._cloud[0] is source:
            return self._cloud[1]
        rows = parse_cloud_rows(values)
        self.parsed += 1
        self._cloud = (source, rows)
        return rows

    def local_rows(self, observation: Any) -> tuple[ComponentRow, ...]:
        if self._local is not None and self._local[0] is observation:
            return self._local[1]
        values = getattr(observation, "document", None)
        raw = values.get("values") if isinstance(values, Mapping) else None
        rows = parse_local_rows(
            raw
            if isinstance(raw, Sequence) and not isinstance(raw, str | bytes)
            else getattr(observation.data, "modules", ())
        )
        self.parsed += 1
        self._local = (observation, rows)
        return rows


def _local_observations(coordinator: Any) -> dict[str, Any]:
    """The newest local reading per path, current or retained."""
    selection = getattr(coordinator, "local_selection", None)
    if selection is None:
        return {}
    newest: dict[str, Any] = {}
    try:
        states = selection.local_states()
    except Exception:  # no local access is usable
        return {}
    for state in states:
        if state is None:
            continue
        for observation in (
            *(getattr(state, "last_observations", ()) or ()),
            *(getattr(state, "current_observations", ()) or ()),
        ):
            path = observation.request.path
            kept = newest.get(path)
            if kept is None or observation.observed_at > kept.observed_at:
                newest[path] = observation
    return newest


def _value(
    coordinator: Any, local: Mapping[str, Any], path: str
) -> tuple[object, tuple[object, ...]] | None:
    """A reading's value and allowed values: the cloud's data, its resources
    (the stored inventory included), else the newest local reading."""
    data = getattr(coordinator, "data", None) or {}
    resources = getattr(coordinator, "resources", None) or {}
    snapshot = data.get(path)
    for resource in (
        getattr(snapshot, "resource", None),
        resources.get(path),
    ):
        if resource is not None and resource.has_value and resource.value is not None:
            return resource.value, tuple(resource.metadata.allowed_values)
    reading = getattr(local.get(path), "data", None)
    value = getattr(reading, "value", None)
    if value is None:
        return None
    allowed = getattr(reading, "allowed_values", ()) or ()
    return value, tuple(allowed)


def _hybrid_evidence(
    coordinator: Any, local: Mapping[str, Any]
) -> tuple[str, ...] | None:
    """The hybrid evidence as the integration used it before, from the
    readings whatever their age; ``None`` without any readings."""
    from .derived_inputs import local_only_paths
    from .local_only import hybrid_evidence, hybrid_path_counts, hybrid_path_needs_value

    resources = getattr(coordinator, "resources", None) or {}
    data = getattr(coordinator, "data", None) or {}
    try:
        local_paths = local_only_paths(coordinator)
    except Exception:  # no local access is usable
        local_paths = frozenset()
    if not resources and not data and not local_paths:
        return None
    readings: list[tuple[str, object]] = []
    for path in (*resources, *(path for path in data if path not in resources)):
        if not hybrid_path_counts(path):
            continue
        if not hybrid_path_needs_value(path):
            readings.append((path, None))
            continue
        found = _value(coordinator, {}, path)
        readings.append((path, found[0] if found is not None else None))
    for path in local_paths:
        if path in resources or not hybrid_path_counts(path):
            continue
        if not hybrid_path_needs_value(path):
            readings.append((path, None))
            continue
        found = _value(coordinator, local, path)
        readings.append((path, found[0] if found is not None else None))
    return hybrid_evidence(readings)


def _allowed(values: tuple[object, ...]) -> tuple[str, ...]:
    tokens: list[str] = []
    for value in values:
        token = safe_token(value if isinstance(value, str) else str(value))
        if token is not None and token not in tokens:
            tokens.append(token)
        if len(tokens) >= MAX_ALLOWED_VALUES:
            break
    return tuple(tokens)


def _gather(coordinator: Any, readings: _Readings) -> _Gathered:
    local = _local_observations(coordinator)
    data = getattr(coordinator, "data", None) or {}
    resources = getattr(coordinator, "resources", None) or {}

    cloud: SourceRows | None = None
    snapshot = data.get(CLOUD_PATH)
    resource = getattr(snapshot, "resource", None) or resources.get(CLOUD_PATH)
    if resource is not None and resource.values:
        cloud = SourceRows(
            "cloud",
            readings.cloud_rows(resource, resource.values),
            getattr(snapshot, "last_success", None),
            True,
        )
    local_rows: SourceRows | None = None
    observation = local.get(LOCAL_PATH)
    if (
        observation is not None
        and getattr(observation.data, "status", None) == "completed"
    ):
        local_rows = SourceRows(
            "local", readings.local_rows(observation), observation.observed_at, True
        )

    allowed: list[tuple[str, tuple[str, ...]]] = []

    def value(path: str) -> str | None:
        found = _value(coordinator, local, path)
        if found is None:
            return None
        if found[1]:
            allowed.append((path, _allowed(found[1])))
        return safe_token(found[0] if isinstance(found[0], str) else str(found[0]))

    paths = set(readings.type_paths(resources)) | {
        path
        for path in (*data, *local)
        if _HEAT_SOURCE_TYPE.fullmatch(path) or _HEAT_PUMP_TYPE.fullmatch(path)
    }
    heat_source_types: list[tuple[str, str]] = []
    heat_pump_types: list[tuple[str, str]] = []
    for path in sorted(paths):
        token = value(path)
        if token is None:
            continue
        if match := _HEAT_SOURCE_TYPE.fullmatch(path):
            heat_source_types.append((match.group(1), token))
        elif match := _HEAT_PUMP_TYPE.fullmatch(path):
            heat_pump_types.append((match.group(1) or CENTRAL, token))
    system_type = value(SYSTEM_TYPE_PATH)
    heat_sources_type = value(HEAT_SOURCES_TYPE_PATH)
    bus = value(BUS_PATH)
    outdoor = value(OUTDOOR_STATUS_PATH)
    firmware = _value(coordinator, local, FIRMWARE_PATH)
    firmware_text = clean_version(firmware[0]) if firmware is not None else None
    if firmware_text is None:
        firmware_text = clean_version(
            getattr(getattr(coordinator, "gateway", None), "firmware_version", None)
        )
    klass = gateway_class(getattr(coordinator, "gateway", None))
    return _Gathered(
        cloud,
        local_rows,
        system_type,
        tuple(heat_source_types[:_MAX_HEAT_SOURCES]),
        heat_sources_type,
        tuple(heat_pump_types[:_MAX_HEAT_SOURCES]),
        bus,
        outdoor,
        tuple(sorted(allowed)),
        _hybrid_evidence(coordinator, local),
        klass,
        firmware_text,
        _platform(coordinator, klass),
    )


# ---------------------------------------------------------- source choice


def _compared(rows: Sequence[ComponentRow]) -> tuple[tuple[int, int], ...]:
    """The rows two sources are compared by: place and type, without the
    gateway's own rows."""
    return tuple(
        sorted(
            (
                row.tok if row.tok is not None else -1,
                row.type_id if row.type_id is not None else -1,
            )
            for row in rows
            if not is_gateway_row(row.type_id, row.tok)
        )
    )


def _newer(first: SourceRows, second: SourceRows) -> bool:
    if first.observed_at is None:
        return False
    return second.observed_at is None or first.observed_at > second.observed_at


def choose_rows(
    cloud: SourceRows | None,
    local: SourceRows | None,
    last: SourceRows | None,
) -> tuple[SourceRows | None, bool]:
    """The list to show and whether the two current lists disagree."""
    if cloud is None or local is None:
        return (cloud or local or last), False
    newer, older = (local, cloud) if _newer(local, cloud) else (cloud, local)
    if _compared(cloud.rows) == _compared(local.rows):
        if last is not None:
            kept = cloud if last.source == "cloud" else local
            other = local if kept is cloud else cloud
            # A change of source never shows fewer rows.
            return (other if len(other.rows) > len(kept.rows) else kept), False
        return (older if len(older.rows) > len(newer.rows) else newer), False
    if last is not None:
        for candidate in (newer, older):
            if _compared(candidate.rows) == _compared(last.rows):
                return candidate, True
    return newer, True


# ------------------------------------------------------------- stored row


@dataclass(frozen=True, slots=True)
class _Stored:
    """The checked content of a stored row."""

    sources: Mapping[str, SourceRows]
    shown: str | None
    inputs: Mapping[str, Any]
    kind: str | None
    fuel: str | None
    cascade: bool | None
    evidence: tuple[str, ...]
    conflicts: tuple[str, ...]
    model_version: int | None
    changes: int
    probes: tuple[Mapping[str, object], ...]
    components: Mapping[str, Mapping[str, str | None]]
    updated_at: datetime | None


def _codes(value: object, allowed: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        sorted({item for item in value if isinstance(item, str) and item in allowed})
    )


def _token_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for key, item in list(value.items())[:_MAX_HEAT_SOURCES]:
        token = safe_token(item) if isinstance(item, str) else None
        if isinstance(key, str) and _SAFE.fullmatch(key) and token is not None:
            result[key] = token
    return result


def _probe(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping) or not value or len(value) > 16:
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _SAFE.fullmatch(key):
            return None
        if not _probe_value(item):
            return None
        result[key] = item
    return result


def _probe_value(item: object) -> bool:
    """A flag, a bounded whole number or a safe text."""
    if item is None or isinstance(item, bool):
        return True
    if isinstance(item, int):
        return abs(item) < 10**12
    return isinstance(item, str) and _PROBE_TEXT.fullmatch(item) is not None


def _same_times(
    previous: Sequence[Mapping[str, object]], current: Sequence[Mapping[str, object]]
) -> tuple[Mapping[str, object], ...]:
    """The current results, each with the time (``at``) of an earlier result
    that is otherwise equal: an unchanged run changes nothing in the row."""

    def without_time(item: Mapping[str, object]) -> tuple[tuple[str, object], ...]:
        return tuple(sorted((key, value) for key, value in item.items() if key != "at"))

    earlier = {without_time(item): item.get("at") for item in previous}
    kept: list[Mapping[str, object]] = []
    for item in current:
        at = earlier.get(without_time(item))
        if at is not None and "at" in item:
            item = {**item, "at": at}
        kept.append(item)
    return tuple(kept)


def _probes(value: object) -> tuple[Mapping[str, object], ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return None
    probes = [_probe(item) for item in list(value)[: MAX_PROBES + 1]]
    if len(probes) > MAX_PROBES or any(item is None for item in probes):
        return None
    return tuple(item for item in probes if item is not None)


def _stored_inputs(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    inputs: dict[str, Any] = {}
    for key in (
        "system_type",
        "heat_sources_type",
        "bus",
        "outdoor_status",
        "gateway_class",
        "platform",
    ):
        item = value.get(key)
        if isinstance(item, str) and safe_token(item) == item:
            inputs[key] = item
    firmware = clean_version(value.get("gateway_firmware"))
    if firmware is not None:
        inputs["gateway_firmware"] = firmware
    for key in ("heat_source_types", "heat_pump_types"):
        inputs[key] = _token_map(value.get(key))
    evidence = value.get("hybrid_evidence")
    if isinstance(evidence, list):
        inputs["hybrid_evidence"] = tuple(
            sorted(
                {
                    item
                    for item in evidence
                    if isinstance(item, str) and safe_token(item) == item
                }
            )
        )
    allowed = value.get("allowed_values")
    if isinstance(allowed, Mapping):
        inputs["allowed_values"] = {
            path: tuple(
                item
                for item in items[:MAX_ALLOWED_VALUES]
                if isinstance(item, str) and safe_token(item) == item
            )
            for path, items in list(allowed.items())[:32]
            if isinstance(path, str) and isinstance(items, list)
        }
    return inputs


def parse_row(row: object) -> _Stored | None:
    """The stored row, checked; ``None`` for a missing or foreign row."""
    if not isinstance(row, Mapping) or row.get("v") != ROW_VERSION:
        return None
    sources: dict[str, SourceRows] = {}
    raw_sources = row.get("sources")
    if isinstance(raw_sources, Mapping):
        for name in SOURCES:
            item = raw_sources.get(name)
            if not isinstance(item, Mapping):
                continue
            rows = rows_from_lists(item.get("rows"))
            if rows is not None:
                sources[name] = SourceRows(
                    name, rows, _parse_time(item.get("changed_at")), False
                )
    shown = row.get("shown")
    kind = row.get("kind")
    fuel = row.get("fuel")
    cascade = row.get("cascade")
    model = row.get("model_version")
    changes = row.get("changes")
    components: dict[str, dict[str, str | None]] = {}
    raw_components = row.get("components")
    if isinstance(raw_components, Mapping):
        for key, item in list(raw_components.items())[:MAX_COMPONENT_ENTRIES]:
            if not isinstance(key, str) or not isinstance(item, Mapping):
                continue
            first = item.get("first_seen")
            removed = item.get("removed_at")
            if _parse_time(first) is None:
                continue
            components[key] = {
                "first_seen": first if isinstance(first, str) else None,
                "removed_at": removed if _parse_time(removed) is not None else None,
            }
    return _Stored(
        sources,
        shown if shown in sources else None,
        _stored_inputs(row.get("inputs")),
        kind if kind in PLANT_KINDS else None,
        fuel if fuel in FUELS else None,
        cascade if isinstance(cascade, bool) else None,
        _codes(row.get("evidence"), EVIDENCE_CODES),
        _codes(row.get("conflicts"), CONFLICT_CODES),
        model if isinstance(model, int) and not isinstance(model, bool) else None,
        changes
        if isinstance(changes, int) and not isinstance(changes, bool) and changes >= 0
        else 0,
        _probes(row.get("probes")) or (),
        components,
        _parse_time(row.get("updated_at")),
    )


# ---------------------------------------------------------------- tracker


def _component_key(row: ComponentRow) -> str:
    tok = "-" if row.tok is None else str(row.tok)
    type_id = "-" if row.type_id is None else str(row.type_id)
    return f"{tok}/{type_id}"


class PlantTracker:
    """The plant model of one gateway, following its coordinator."""

    def __init__(
        self,
        coordinator: BoschBuderusDataUpdateCoordinator,
        store: DerivedStore | None,
    ) -> None:
        # Weak: the tracker never keeps a coordinator alive (_TRACKERS).
        self._coordinator = ref(coordinator)
        self._store = store
        self._key = row_key(coordinator.gateway.gateway_id)
        self._readings = _Readings()
        self._loaded = store is None
        self._stored: _Stored | None = None
        self._row: dict[str, Any] | None = None
        self._inputs: PlantInputs | None = None
        self._table: ComponentTable | None = None
        self._raw: PlantResult | None = None
        self._failed = False
        self._state: tuple[object, ...] | None = None
        self._unsubscribe: Any = None
        self.result: PlantResult | None = None
        self.shown: SourceRows | None = None
        self.sources: dict[str, SourceRows] = {}
        self.inputs_restored: frozenset[str] = frozenset()
        self.allowed_values: dict[str, tuple[str, ...]] = {}
        self.sources_differ = False
        self.computations = 0
        self.writes = 0
        self.changes = 0
        self.probes: tuple[Mapping[str, object], ...] = ()
        # Results handed over before the stored row was read.
        self._fresh_probes: tuple[Mapping[str, object], ...] | None = None
        self._components: dict[str, dict[str, str | None]] = {}
        self._updated_at: datetime | None = None

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def stored(self) -> bool:
        return self._stored is not None or self._row is not None

    @property
    def restored(self) -> bool:
        """The result depends on a stored input."""
        return bool(self.inputs_restored)

    @property
    def stored_age_seconds(self) -> int | None:
        if self._updated_at is None:
            return None
        return max(0, int((dt_util.utcnow() - self._updated_at).total_seconds()))

    def subscribe(self, entry: DiscoveryLifecycle) -> None:
        """Follow the coordinator's updates for the lifetime of ``entry``."""
        if self._unsubscribe is not None:
            return
        coordinator = self._coordinator()
        if coordinator is None:
            return
        remove = coordinator.async_add_listener(self.update)

        def unsubscribe() -> None:
            remove()
            self._unsubscribe = None

        self._unsubscribe = unsubscribe
        entry.async_on_unload(unsubscribe)

    async def async_load(self) -> None:
        """Read the stored row once, then work the result out.

        Never fails: a row that cannot be read counts as missing, and the
        model starts without it."""
        if self._loaded or self._store is None:
            return
        try:
            await self._store.async_load()
            if self._loaded:
                return
            self._read_row(self._store.get(self._key))
        except Exception:  # the plant model never stops a setup
            self._note_failure("Stored plant row not read")
        if self._loaded:
            return
        if self._fresh_probes is not None:
            self.probes = _same_times(self.probes, self._fresh_probes)
            self._fresh_probes = None
        self._loaded = True
        try:
            self.refresh()
        except Exception:  # the plant model never stops a setup
            self._note_failure("Plant model not worked out")

    def _read_row(self, row: dict[str, Any] | None) -> None:
        stored = parse_row(row)
        if stored is None or row is None:
            return
        components = {key: dict(value) for key, value in stored.components.items()}
        self._stored = stored
        self._row = row
        self.changes = stored.changes
        self.probes = stored.probes
        self._components = components
        self._updated_at = stored.updated_at

    def _note_failure(self, message: str) -> None:
        if not self._failed:
            self._failed = True
            _LOGGER.debug(message, exc_info=True)

    @callback
    def update(self) -> None:
        """Follow an update of the coordinator; never disturbs it."""
        self.safe_current()

    def safe_current(self) -> PlantResult | None:
        """``current`` for every caller outside the model: an unexpected
        error gives no result (behave as before the plant model)."""
        try:
            return self.current()
        except Exception:  # the plant model must never stop its callers
            self._note_failure("Plant model not worked out")
            return None

    def current(self) -> PlantResult | None:
        """The result of this update pass: worked out once per pass, as the
        hybrid evidence is (every caller asks within the same pass)."""
        from .local_selection import LocalSelection

        selection = getattr(self._coordinator(), "local_selection", None)
        if isinstance(selection, LocalSelection):
            return selection._cached(("plant_model", id(self)), self.refresh)
        return self.refresh()

    def refresh(self) -> PlantResult | None:
        """Work the result out from the current readings; ``None`` before
        the stored row is read."""
        coordinator = self._coordinator()
        if not self._loaded or coordinator is None:
            return self.result if coordinator is None else None
        gathered = _gather(coordinator, self._readings)
        stored = self._stored
        restored: set[str] = set()
        sources: dict[str, SourceRows] = {}
        for name, current in (("cloud", gathered.cloud), ("local", gathered.local)):
            if current is not None:
                sources[name] = current
            elif stored is not None and name in stored.sources:
                sources[name] = stored.sources[name]
        last = self.shown
        if last is None and stored is not None and stored.shown is not None:
            last = stored.sources.get(stored.shown)
        current_cloud = gathered.cloud
        current_local = gathered.local
        if current_cloud is None and current_local is None:
            shown, differ = last, False
        else:
            shown, differ = choose_rows(current_cloud, current_local, last)
        if shown is not None and not shown.current:
            restored.add("components")
        self.shown = shown
        self.sources = sources
        self.sources_differ = differ

        kept = stored.inputs if stored is not None else {}

        def pick(name: str, value: Any) -> Any:
            if value is not None:
                return value
            if kept.get(name) is not None:
                restored.add(name)
                return kept[name]
            return None

        def pick_map(
            name: str, value: tuple[tuple[str, str], ...]
        ) -> tuple[tuple[str, str], ...]:
            merged = dict(kept.get(name) or {})
            if merged and set(merged) - {key for key, _ in value}:
                restored.add(name)
            merged.update(dict(value))
            return tuple(sorted(merged.items()))

        hybrid_evidence = gathered.hybrid_evidence
        if hybrid_evidence is None:
            hybrid_evidence = pick("hybrid_evidence", None) or ()
        inputs = PlantInputs(
            gateway_class=gathered.gateway_class,
            gateway_firmware=pick("gateway_firmware", gathered.gateway_firmware),
            system_type=pick("system_type", gathered.system_type),
            heat_source_types=pick_map("heat_source_types", gathered.heat_source_types),
            heat_sources_type=pick("heat_sources_type", gathered.heat_sources_type),
            heat_pump_types=pick_map("heat_pump_types", gathered.heat_pump_types),
            bus=pick("bus", gathered.bus),
            outdoor_status=pick("outdoor_status", gathered.outdoor_status),
            components=shown.rows if shown is not None else (),
            hybrid_evidence=tuple(hybrid_evidence),
            platform=gathered.platform,
        )
        allowed = dict(kept.get("allowed_values") or {})
        allowed.update(dict(gathered.allowed_values))
        self.allowed_values = allowed
        table = load_table()
        if inputs != self._inputs or table is not self._table or self._raw is None:
            self._inputs = inputs
            self._table = table
            self.computations += 1
            self._raw = resolve(inputs, table)
        state = (self._raw, differ, frozenset(restored))
        if state != self._state:
            self._state = state
            result = self._raw
            if differ:
                result = replace(
                    result,
                    conflicts=tuple(
                        sorted({*result.conflicts, "cloud_local_components_differ"})
                    ),
                )
            self._take(result, restored)
        self.inputs_restored = frozenset(restored)
        self._save()
        return self.result

    def _take(self, result: PlantResult, restored: set[str]) -> None:
        stored = self._stored
        previous = (
            self.result.plant_kind
            if self.result is not None
            else (stored.kind if stored is not None else None)
        )
        if result.transient and previous is not None:
            # A bus that is still detecting keeps what was known.
            base = self.result
            if base is None and stored is not None:
                base = PlantResult(
                    stored.kind,
                    stored.fuel,
                    stored.cascade,
                    stored.evidence,
                    stored.conflicts,
                )
            if base is not None:
                result = replace(
                    base,
                    evidence=tuple(sorted({*base.evidence, "stored"})),
                    gates=result.gates,
                )
        elif _uses_stored(result, restored):
            result = replace(
                result, evidence=tuple(sorted({*result.evidence, "stored"}))
            )
        if previous is not None and result.plant_kind != previous:
            self.changes += 1
            _LOGGER.info(
                "The detected plant kind changed from %s to %s",
                previous,
                result.plant_kind,
            )
        self.result = result

    def _bookkeeping(self, now: datetime) -> None:
        """First appearance and disappearance of each component."""
        current = [item for item in self.sources.values() if item.current]
        if not current:
            return
        present = {_component_key(row) for item in current for row in item.rows}
        stamp = _iso(now)
        for key in present:
            entry = self._components.get(key)
            if entry is None:
                self._components[key] = {"first_seen": stamp, "removed_at": None}
            elif entry.get("removed_at") is not None:
                entry["removed_at"] = None
        for key in list(self._components):
            if key in present:
                continue
            entry = self._components[key]
            removed = _parse_time(entry.get("removed_at"))
            if removed is None:
                entry["removed_at"] = stamp
            elif now - removed > REMOVED_KEPT:
                del self._components[key]
        while len(self._components) > MAX_COMPONENT_ENTRIES:
            oldest = min(
                self._components,
                key=lambda key: self._components[key]["first_seen"] or "",
            )
            del self._components[oldest]

    def _content(self) -> dict[str, Any]:
        result = self.result
        inputs = self._inputs
        assert result is not None and inputs is not None
        stored_sources = self._stored.sources if self._stored is not None else {}
        previous = (self._row or {}).get("sources")
        previous = previous if isinstance(previous, Mapping) else {}
        sources: dict[str, Any] = {}
        for name, item in self.sources.items():
            rows = [row.to_list() for row in item.rows]
            before = (
                previous.get(name) if isinstance(previous.get(name), Mapping) else None
            )
            changed_at = (
                before.get("changed_at")
                if before is not None and before.get("rows") == rows
                else None
            )
            if changed_at is None:
                kept = stored_sources.get(name)
                if (
                    kept is not None
                    and kept.rows == item.rows
                    and kept.observed_at is not None
                ):
                    changed_at = _iso(kept.observed_at)
                else:
                    changed_at = _iso(dt_util.utcnow())
            sources[name] = {"rows": rows, "changed_at": changed_at}
        gate = result.gates.get("hybrid_manager_place")
        return {
            "v": ROW_VERSION,
            "model_version": MODEL_VERSION,
            "table_version": self._table.table_version
            if self._table is not None
            else None,
            "sources": sources,
            "shown": self.shown.source if self.shown is not None else None,
            "components": {
                key: dict(value) for key, value in sorted(self._components.items())
            },
            "inputs": {
                "system_type": inputs.system_type,
                "heat_source_types": dict(inputs.heat_source_types),
                "heat_sources_type": inputs.heat_sources_type,
                "heat_pump_types": dict(inputs.heat_pump_types),
                "bus": inputs.bus,
                "outdoor_status": inputs.outdoor_status,
                "hybrid_evidence": list(inputs.hybrid_evidence),
                "allowed_values": {
                    path: list(values)
                    for path, values in sorted(self.allowed_values.items())
                },
                "gateway_class": inputs.gateway_class,
                "gateway_firmware": inputs.gateway_firmware,
                "platform": inputs.platform,
            },
            "gates": {} if gate is None else {"hybrid_manager_place": gate.as_dict()},
            "kind": result.plant_kind,
            "fuel": result.fuel,
            "cascade": result.cascade,
            "evidence": list(result.evidence),
            "conflicts": list(result.conflicts),
            "changes": self.changes,
            "probes": [dict(item) for item in self.probes],
        }

    def _save(self) -> None:
        if self._store is None or self.result is None:
            return
        now = dt_util.utcnow()
        self._bookkeeping(now)
        content = self._content()
        before = dict(self._row or {})
        before.pop("updated_at", None)
        if content == before:
            return
        content["updated_at"] = _iso(now)
        self._updated_at = now
        self._row = content
        self._store.set(self._key, content)
        self.writes += 1

    def set_probes(self, results: Sequence[Mapping[str, object]]) -> bool:
        """Keep the results of targeted reads; False for an invalid list."""
        probes = _probes(results)
        if probes is None:
            return False
        if not self._loaded:
            self._fresh_probes = probes
            return True
        self.probes = _same_times(self.probes, probes)
        if self.result is not None:
            self._save()
        return True


# Which stored input each evidence code rests on.
_EVIDENCE_INPUTS: Final[Mapping[str, tuple[str, ...]]] = {
    "reported_system_type": ("system_type", "hybrid_evidence"),
    "reported_heat_source_type": (
        "heat_source_types",
        "heat_sources_type",
        "hybrid_evidence",
    ),
    "reported_heat_pump_type": ("heat_pump_types",),
    "reported_outdoor_status": ("outdoor_status",),
    "hybrid_manager_component": ("components", "gateway_firmware"),
    "heat_source_types_combined": (
        "heat_source_types",
        "heat_sources_type",
        "heat_pump_types",
    ),
    "hybrid_resources": ("hybrid_evidence",),
    "hybrid_manager_signals": ("hybrid_evidence",),
}


def _uses_stored(result: PlantResult, restored: set[str]) -> bool:
    return any(
        name in restored
        for code in result.evidence
        for name in _EVIDENCE_INPUTS.get(code, ())
    )


# ------------------------------------------------------------- public API


def _supported(coordinator: Any) -> bool:
    from .water_softener import is_water_softener

    gateway = getattr(coordinator, "gateway", None)
    if gateway is None:
        return False
    try:
        return not is_water_softener(
            gateway, getattr(coordinator, "resources", None) or {}
        )
    except Exception:  # an incomplete view
        return True


def track_plant(
    entry: DiscoveryLifecycle, coordinator: BoschBuderusDataUpdateCoordinator
) -> PlantTracker | None:
    """Start the plant model of a gateway for the lifetime of ``entry``.

    The stored row is read first; until then there is no result.
    """
    if not _supported(coordinator):
        return None
    tracker = existing_tracker(coordinator)
    if tracker is None:
        try:
            tracker = PlantTracker(coordinator, derived_store(coordinator.hass))
            _TRACKERS[coordinator] = tracker
        except Exception:  # the plant model never stops a setup
            _LOGGER.debug("Plant model not started", exc_info=True)
            return None
        pending = _PENDING_PROBES.pop(coordinator, None)
        if pending is not None:
            tracker.set_probes(pending)
        coordinator.hass.async_create_task(tracker.async_load(), eager_start=True)
    tracker.subscribe(entry)
    return tracker


def existing_tracker(coordinator: object) -> PlantTracker | None:
    try:
        return _TRACKERS.get(coordinator)
    except TypeError:  # not weakly referable
        return None


def current_plant(coordinator: object) -> PlantResult | None:
    """The plant kind of a gateway; ``None`` while unknown (behave as
    before the plant model)."""
    tracker = existing_tracker(coordinator)
    if tracker is None or not tracker.loaded:
        return None
    return tracker.safe_current()


def components(coordinator: object) -> ShownComponents | None:
    """The components shown for a gateway and their source."""
    tracker = existing_tracker(coordinator)
    if tracker is None or not tracker.loaded:
        return None
    try:
        tracker.current()
        shown = tracker.shown
        table = load_table()
        return ShownComponents(
            shown.source if shown is not None else None,
            shown.rows if shown is not None else (),
            shown is not None and not shown.current,
            table.table_version if table is not None else None,
        )
    except Exception:  # the plant model must never stop its callers
        tracker._note_failure("Plant components not worked out")
        return None


def set_probe_results(
    coordinator: object, results: Sequence[Mapping[str, object]]
) -> bool:
    """Keep the results of targeted reads in the stored row (at most 48
    entries of safe texts, numbers and flags); False if not kept.

    Results handed over before the gateway's model started are kept until
    it starts. An entry equal to a stored one keeps the stored time.
    """
    tracker = existing_tracker(coordinator)
    if tracker is not None:
        return tracker.set_probes(results)
    probes = _probes(results)
    if probes is None:
        return False
    try:
        _PENDING_PROBES[coordinator] = probes
    except TypeError:  # not weakly referable
        return False
    return True


# ------------------------------------------------------------ diagnostics

MAX_UNMAPPED: Final = 32


def _component_report(
    row: ComponentRow, table: ComponentTable | None
) -> dict[str, object]:
    description = describe(row, table)
    return {
        "id": row.type_id,
        "token": row.tok,
        "place": description.place,
        "place_name": description.place_name,
        "name": description.type_name,
        "role": description.role,
        "basis": description.basis,
        "gateway": description.gateway,
        "version": row.version,
        "hardware_type": row.hardware_type,
        "product_name": row.product_name,
        "has_product_name": row.product_name is not None,
        "sw_update_type": row.sw_update_type,
        "sw_ident": row.sw_ident,
        "article_number": row.article_number,
    }


def _unmapped(
    rows: Sequence[ComponentRow], table: ComponentTable | None
) -> list[dict[str, object]]:
    counts: dict[tuple[int | None, int | None], int] = {}
    for row in rows:
        if (
            table is not None
            and row.type_id is not None
            and table.component(row.type_id)
        ):
            continue
        counts[(row.type_id, row.tok)] = counts.get((row.type_id, row.tok), 0) + 1
    ordered = sorted(
        counts.items(), key=lambda item: (item[0][0] or -1, item[0][1] or -1)
    )
    return [
        {"id": type_id, "token": tok, "count": count}
        for (type_id, tok), count in ordered[:MAX_UNMAPPED]
    ]


def plant_has_values(report: object) -> bool:
    """Whether a plant block holds reported texts (components, versions,
    type values), which makes the report contain raw values."""
    if not isinstance(report, Mapping):
        return False
    inputs = report.get("inputs")
    reported = isinstance(inputs, Mapping) and any(
        inputs.get(key)
        for key in (
            "gateway_firmware",
            "system_type",
            "heat_source_types",
            "heat_sources_type",
            "heat_pump_types",
            "bus",
            "outdoor_status",
        )
    )
    return bool(report.get("components") or report.get("sources") or reported)


def plant_diagnostics(coordinator: Any) -> dict[str, Any] | None:
    """The diagnostics block ``plant``; ``None`` for a water softener.

    Never fails: an unexpected error gives a fixed note.
    """
    try:
        if not _supported(coordinator):
            return None
        tracker = existing_tracker(coordinator)
        if tracker is None or not tracker.loaded:
            # Worked out for the report only, without the stored row.
            tracker = PlantTracker(coordinator, None)
        tracker.refresh()
        return _report(tracker)
    except Exception:  # the report must stay available
        _LOGGER.debug("Plant model not reportable", exc_info=True)
        return {"error": "unavailable"}


def _report(tracker: PlantTracker) -> dict[str, Any]:
    table = load_table()
    result = tracker.result
    inputs = tracker._inputs or PlantInputs()
    shown = tracker.shown
    gates: Mapping[str, Gate] = result.gates if result is not None else {}
    return {
        "model_version": MODEL_VERSION,
        "table_version": table.table_version if table is not None else None,
        "table_available": table is not None,
        "plant_kind": result.plant_kind if result is not None else None,
        "fuel": result.fuel if result is not None else None,
        "cascade": result.cascade if result is not None else None,
        "evidence": list(result.evidence) if result is not None else [],
        "conflicts": list(result.conflicts) if result is not None else [],
        "inputs": {
            "platform": inputs.platform,
            "gateway_class": inputs.gateway_class,
            "gateway_firmware": inputs.gateway_firmware,
            "system_type": inputs.system_type,
            "heat_source_types": dict(inputs.heat_source_types),
            "heat_sources_type": inputs.heat_sources_type,
            "heat_pump_types": dict(inputs.heat_pump_types),
            "bus": inputs.bus,
            "outdoor_status": inputs.outdoor_status,
            "hybrid_evidence": list(inputs.hybrid_evidence),
            "allowed_values": {
                path: list(values)
                for path, values in sorted(tracker.allowed_values.items())
            },
            "restored": sorted(tracker.inputs_restored),
        },
        "gates": {name: gate.as_dict() for name, gate in gates.items()},
        "components_source": shown.source if shown is not None else None,
        "components": [
            _component_report(row, table)
            for row in (shown.rows if shown is not None else ())
        ],
        "unmapped_components": _unmapped(
            shown.rows if shown is not None else (), table
        ),
        "sources": {
            name: {
                "current": item.current,
                "rows": len(item.rows),
                "components": [_component_report(row, table) for row in item.rows],
            }
            for name, item in sorted(tracker.sources.items())
        },
        "stored": tracker.stored,
        "restored": tracker.restored,
        "stored_age_seconds": tracker.stored_age_seconds,
        "changes": tracker.changes,
        "probes": [dict(item) for item in tracker.probes],
    }
