"""The entities of a plant from its local readings alone (P49b).

The local access answers in the resource format of the cloud and describes
itself: type, unit, allowed values and limits. This module turns the current
local readings of one plant into the entity set the plant has without the
cloud:

- A reading of the pair catalog becomes a resource in cloud form. It yields
  the same entities with the same unique IDs as the cloud resource, so their
  history continues. It is always read-only: the local access never writes,
  so it never yields a control.
- Every other readable reading follows the rules of the local-only readings
  (P45): ``<gateway>:local_only:<path>``; reviewed readings keep their fixed
  meaning and default activation, all others are offered generically (a
  number with the unit of the reading, or a state with the allowed values of
  the reading). Everyday values are enabled by the integration's default
  policy, technical values and signals are disabled.

Identifiers, addresses, names, network data, update logs and free text never
become entities. A paired reading for which the cloud offers no entity (its
cloud form yields none) follows the local-only rules, as P45 does. A reading
of a path the cloud offers on this plant (``cloud_paths``, for example from the
saved inventory) is treated like a paired one, so its entity keeps the cloud's
ID.

A fresh cloud resource of the same path may serve as a template when type,
unit, shape and allowed values match; it is never required.

Nothing here creates entities, sends requests or changes availability. The
diagnostics compare the local entity set with a running cloud (shadow
comparison).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from .local_api.catalog import request_for_path
from .local_api.errors import LocalResourceError
from .local_api.models import DataIssue, NamedValues, ScalarValue, ValueArray
from .local_only import (
    ENUM_KINDS,
    GENERIC,
    LocalOnlyDefinition,
    definition_for,
    for_reading,
    identifier_name,
    state_class,
    unit_and_class,
)
from .local_only_sensor import NUMBER_TYPES, local_only_suffix
from .observations import (
    Observation,
    ValueStatus,
    canonical_unit,
    from_local_observation,
    pair_for_path,
)
from .pointt.exceptions import ProtocolError
from .pointt.models import Resource
from .pointt.parsers import parse_resource
from .pointt.redaction import diagnostic_resource_path
from .resource_catalog import (
    entity_enabled_by_default,
    is_private_resource,
    resource_name,
)

if TYPE_CHECKING:
    from .local_api.models import LocalObservation

# Value forms the cloud resource parser accepts as one entity resource.
_CLOUD_FORM_TYPES = frozenset(
    {"floatValue", "integerValue", "stringValue", "emonValue", "arrayData"}
)
# Path segments of network, update, licence and identity data: never entities.
_PRIVATE_SEGMENT = re.compile(
    r"ip|ipv[46]|eth|wifi|mac\w*|ssid|uuid|serial\w*|sgtin|token\w*|passw\w*"
    r"|cert\w*|update|tzinfo|inclusiontime|basicinfo|licen[cs]e\w*|tosaccepted",
    re.IGNORECASE,
)
_NAME_LEAF = re.compile(r"name(?:[A-Z0-9]\w*)?")
# Allowed values of a generic state: short, printable texts only.
_MAX_OPTIONS = 64
_MAX_OPTION_LENGTH = 64
# Bounded lists in the diagnostics; counts are always complete.
MAX_LISTED = 150
_LISTED_SEGMENT = re.compile(r"[A-Za-z0-9_.{}-]{1,64}")
_LOGICAL_ID = re.compile(r"(?:hc|dhw|hs|sc|zone|zn|pg|sw|device|input)\d{1,3}")
_LABEL = re.compile(r"[A-Za-z0-9%./°_ -]{1,32}")
_PER_SOURCE_SUFFIXES = (":source_local", ":source_cloud")


def private_path(path: str) -> bool:
    """Identifiers, addresses, names, network and update data of the plant."""
    segments = path.strip("/").split("/")
    return (
        is_private_resource(path)
        or any(_PRIVATE_SEGMENT.fullmatch(segment) for segment in segments)
        or identifier_name(segments[-1])
        or _NAME_LEAF.fullmatch(segments[-1]) is not None
    )


# ------------------------------------------------------------ cloud form


def _thaw(value: object) -> Any:
    """The decoded local document as plain JSON, as the cloud parser expects."""
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)  # a JSON number with a fraction, as in the cloud
    return value


def _document(observation: LocalObservation) -> dict[str, Any]:
    document: dict[str, Any] = _thaw(observation.document)
    document["writeable"] = False
    document.pop("writable", None)
    if isinstance(state := document.get("state"), dict):
        document["state"] = [state]  # the cloud lists the states
    if observation.type == "emonValue":
        # The cloud reports energy collections without a unit: established as
        # kWh. Any other local unit is kept, so it is scaled or rejected.
        unit = document.pop("unit", None)
        if unit not in (None, "") and canonical_unit(unit) != "kWh":
            document["unitOfMeasure"] = unit
    return document


def cloud_form(
    observation: LocalObservation, *, cloud_path: bool = False
) -> tuple[Resource | None, str | None]:
    """A paired local reading, or one of a path the cloud offers, as a
    read-only cloud resource, or the reason not."""
    path = observation.request.path
    if pair_for_path(path) is None and not cloud_path:
        return None, "not_paired"
    if private_path(path):
        return None, "private"
    data = observation.data
    if observation.type not in _CLOUD_FORM_TYPES or not isinstance(
        data, ScalarValue | NamedValues | ValueArray
    ):
        return None, "unsupported_shape"
    if DataIssue.UNKNOWN_UNIT in getattr(data, "issues", ()):
        return None, "unknown_unit"
    try:
        parsed = parse_resource(_document(observation), path=path)
    except ProtocolError, ValueError, TypeError:
        return None, "invalid_document"
    return replace(parsed, metadata=replace(parsed.metadata, writable=False)), None


def _unit(resource: Resource) -> str | None:
    unit = canonical_unit(resource.metadata.unit)
    if unit is None and resource.metadata.resource_type == "emonValue":
        return "kWh" if "/emon/" in resource.path else None
    return unit


def _part_names(resource: Resource) -> set[str] | None:
    names: set[str] = set()
    for item in resource.values:
        if isinstance(item, Mapping):
            names.update(item)
        else:
            return None
    return names


def template_compatible(local: Resource, template: Resource) -> bool:
    """A cloud resource can serve as the template of a local one."""
    ours, theirs = local.metadata, template.metadata
    if (
        template.path != local.path
        or theirs.resource_type != ours.resource_type
        or _unit(template) != _unit(local)
    ):
        return False
    # A stored description without values (1A) has no shape to compare; the
    # shape then comes from the local answer alone.
    described_only = not template.has_value and not template.has_values
    if not described_only and (
        bool(template.values) != bool(local.values)
        or isinstance(template.value, Mapping) != isinstance(local.value, Mapping)
        or _part_names(template) != _part_names(local)
    ):
        return False
    return not (
        theirs.allowed_values
        and ours.allowed_values
        and {str(item) for item in theirs.allowed_values}
        != {str(item) for item in ours.allowed_values}
    )


def from_template(local: Resource, template: Resource) -> Resource:
    """The local value with the cloud's metadata, still read-only."""
    value = local.value
    integer = (
        type(template.value) is int
        if template.has_value
        # A description without a value (1A): its type decides.
        else template.metadata.resource_type == "integerValue"
    )
    if integer and isinstance(value, float) and value.is_integer():
        value = int(value)  # same reading, same state text
    return replace(
        template,
        value=value,
        has_value=local.has_value,
        values=local.values,
        has_values=local.has_values,
        metadata=replace(template.metadata, writable=False),
    )


# ------------------------------------------------------- local-only sensors


@dataclass(frozen=True, slots=True)
class LocalSensor:
    """One local-only sensor of the view, following the P45 rules."""

    path: str
    definition: LocalOnlyDefinition
    unit: str | None
    device_class: SensorDeviceClass | None
    reviewed: bool  # a P45 catalog entry with its fixed meaning

    @property
    def unique_suffix(self) -> str:
        return local_only_suffix(self.path)

    @property
    def enabled(self) -> bool:
        return self.definition.enabled

    @property
    def state_class(self) -> SensorStateClass | None:
        return state_class(self.definition, self.unit)

    @property
    def options(self) -> tuple[str, ...] | None:
        return self.definition.options if self.definition.kind in ENUM_KINDS else None


def local_reading(observation: LocalObservation) -> Observation:
    """A local reading as interpreted for a local-only sensor."""
    reading = from_local_observation(
        observation, connection_id="local", target_id="plant", current=True
    )
    if getattr(observation.data, "matched_states", ()):
        # A declared device state (for example "lowflow" or "OFF_COOL"), not a
        # measurement; states and on/off forms use its name.
        return replace(reading, status=ValueStatus.STATE_MARKER, value=None)
    return reading


def _extended(
    path: str, kind: str, options: tuple[str, ...] = ()
) -> LocalOnlyDefinition:
    """A documented reading without a reviewed P45 entry."""
    return LocalOnlyDefinition(
        re.compile(re.escape(path)),
        "spec",
        kind,
        entity_enabled_by_default(path),
        (resource_name(path, language="de"), resource_name(path, language="en")),
        None,
        options,
    )


def local_sensor(
    observation: LocalObservation,
    *,
    registered: Callable[[str], str | None] | None = None,
) -> tuple[LocalSensor | None, str]:
    """A readable local reading as a local-only sensor, or the reason not.

    The same rules with and without the cloud (C7): a generic reading
    reported as a number is offered because of its type, also while its value
    is an error marker (shown as unknown).
    """
    path = observation.request.path
    if private_path(path):
        return None, "private"
    data = observation.data
    if isinstance(data, NamedValues):
        return None, "multipart"
    if not isinstance(data, ScalarValue):
        return None, "unsupported_shape"
    reading = local_reading(observation)
    catalog = definition_for(path)
    # C7: every reading becomes an entity (unreviewed ones disabled); an
    # unknown unit or a current marker shows no value but keeps the entity.
    resolved = for_reading(
        catalog or _extended(path, GENERIC),
        observation,
        registered=registered(path) if registered is not None else None,
    )
    if resolved is None:
        return None, "undecided" if catalog and catalog.alternate else "text"
    definition = resolved
    reviewed = catalog is not None and catalog.kind != GENERIC
    if definition.kind == GENERIC and observation.type not in NUMBER_TYPES:
        return None, "text"
    device_class: SensorDeviceClass | None
    if definition.kind in ENUM_KINDS:
        unit, device_class = None, SensorDeviceClass.ENUM
    elif reading.status is ValueStatus.UNKNOWN_UNIT:
        unit, device_class = None, None  # C7: the number without the unit
    else:
        # A reviewed reading in another unit stays an entity without a value.
        unit, device_class = unit_and_class(definition, reading.unit) or (None, None)
    return LocalSensor(path, definition, unit, device_class, reviewed), "ok"


# --------------------------------------------------------------- the view


@dataclass(frozen=True, slots=True)
class EntityShape:
    """What a comparison looks at; never a value."""

    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    options: tuple[str, ...] | None = None
    enabled: bool | None = None


type EntityKey = tuple[str, str]  # (platform domain, unique ID after gateway)


def _text(value: object) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


@dataclass(frozen=True, slots=True)
class LocalPlantView:
    """Read-only resources in cloud form and local-only sensors of one plant."""

    resources: Mapping[str, Resource]
    local_sensors: tuple[LocalSensor, ...]
    skipped: Mapping[str, int]
    readings: int
    templates_used: int = 0

    def entity_shapes(self) -> dict[EntityKey, EntityShape]:
        shapes = description_shapes(self.resources)
        for sensor in self.local_sensors:
            shapes[("sensor", sensor.unique_suffix)] = EntityShape(
                sensor.unit,
                _text(sensor.device_class),
                _text(sensor.state_class),
                sensor.options,
                sensor.enabled,
            )
        return shapes


def build_local_plant_view(
    observations: Iterable[LocalObservation],
    *,
    templates: Mapping[str, Resource] | None = None,
    cloud_paths: Collection[str] = (),
    local_only_paths: Collection[str] = (),
    registered: Callable[[str], str | None] | None = None,
) -> LocalPlantView:
    """The entity set of the current local readings; deterministic.

    ``templates`` are fresh cloud resources by path; one is used only when
    compatible. ``cloud_paths`` are the paths the cloud offers on this plant;
    their readings keep the cloud's IDs like paired ones. A reading whose
    cloud form yields no entity (the cloud does not offer one for it) follows
    the local-only rules, as with P45. A path in ``local_only_paths`` (for
    example one with a registered local-only entity) stays a local-only
    sensor, so its registered ID wins.
    """
    templates = templates or {}
    by_path: dict[str, LocalObservation] = {}
    for observation in observations:
        by_path.setdefault(observation.request.path, observation)
    resources: dict[str, Resource] = {}
    sensors: list[LocalSensor] = []
    skipped: Counter[str] = Counter()
    used = 0
    for path in sorted(by_path):
        observation = by_path[path]
        reason: str | None = None
        offered = path in cloud_paths
        if path not in local_only_paths and (
            offered or pair_for_path(path) is not None
        ):
            resource, reason = cloud_form(observation, cloud_path=offered)
            template = templates.get(path)
            if (
                resource is not None
                and template is not None
                and template_compatible(resource, template)
            ):
                resource = from_template(resource, template)
            else:
                template = None
            if resource is not None and _cloud_entity(resource, offered=offered):
                resources[path] = resource
                used += template is not None
                continue
        sensor, local_reason = local_sensor(observation, registered=registered)
        if sensor is None:
            skipped[reason or local_reason] += 1
        else:
            sensors.append(sensor)
    return LocalPlantView(
        resources, tuple(sensors), dict(sorted(skipped.items())), len(by_path), used
    )


def _cloud_entity(resource: Resource, *, offered: bool) -> bool:
    """The cloud form gives an entity of a known reading. A resource the
    integration does not know follows the local-only rules here, so its
    local-only entity keeps its ID (create-all). An unknown cloud path the
    cloud offered (a registered cloud entity or the stored inventory) keeps
    its cloud ID; never a local-only twin next to it.

    O10: each builder runs once per resource for both questions.
    """
    from .binary_sensor import build_binary_sensor_descriptions
    from .sensor import build_sensor_descriptions

    resources = {resource.path: resource}
    sensors = build_sensor_descriptions(resources)
    if any(not item.value_kind.startswith("unknown_") for item in sensors):
        return True
    binaries = build_binary_sensor_descriptions(resources)
    return bool(binaries) or (offered and bool(sensors))


def description_shapes(
    resources: Mapping[str, Resource],
) -> dict[EntityKey, EntityShape]:
    """Shapes of the resource sensors and binary sensors the cloud path builds."""
    from .binary_sensor import build_binary_sensor_descriptions
    from .sensor import build_sensor_descriptions, sensor_unique_suffix

    shapes: dict[EntityKey, EntityShape] = {}
    for item in build_sensor_descriptions(resources):
        shapes[("sensor", sensor_unique_suffix(item))] = EntityShape(
            item.native_unit_of_measurement,
            _text(item.device_class),
            _text(item.state_class),
            tuple(item.options) if item.options is not None else None,
            item.entity_registry_enabled_default,
        )
    for binary in build_binary_sensor_descriptions(resources):
        shapes[("binary_sensor", binary.key)] = EntityShape(
            device_class=_text(binary.device_class),
            enabled=binary.entity_registry_enabled_default,
        )
    return shapes


# ------------------------------------------------------ shadow comparison


def registry_shapes(
    entries: Iterable[Any], gateway_id: str
) -> tuple[dict[EntityKey, EntityShape], int]:
    """Registered entities of one gateway, without its optional per-source
    entities (counted separately)."""
    prefix = f"{gateway_id}:"
    shapes: dict[EntityKey, EntityShape] = {}
    per_source = 0
    for entry in entries:
        unique_id = getattr(entry, "unique_id", None)
        if not isinstance(unique_id, str) or not unique_id.startswith(prefix):
            continue
        suffix = unique_id.removeprefix(prefix)
        if suffix.endswith(_PER_SOURCE_SUFFIXES):
            per_source += 1
            continue
        capabilities = getattr(entry, "capabilities", None) or {}
        options = capabilities.get("options")
        shapes[(entry.domain, suffix)] = EntityShape(
            getattr(entry, "unit_of_measurement", None),
            _text(getattr(entry, "original_device_class", None)),
            _text(capabilities.get("state_class")),
            tuple(options) if isinstance(options, list | tuple) else None,
        )
    return shapes, per_source


@lru_cache(maxsize=4096)
def diagnostic_suffix(suffix: str) -> str:
    """A unique ID after the gateway, with unrecognized dynamic IDs hidden."""
    prefix = ""
    for marker in ("local_only:", "gateway:"):
        if suffix.startswith(marker):
            prefix, suffix = marker, suffix.removeprefix(marker)
            break
    path = "/" + suffix.replace(":", "/")
    try:
        template = request_for_path(
            path, signal_ids=(path.removeprefix("/signals/"),)
        ).definition.template
    except LocalResourceError:
        # Not a documented local path: the reviewed cloud path redaction.
        path = diagnostic_resource_path(path)
    else:
        # A documented local path: only its address parameters can carry an
        # installation ID; logical IDs such as hc1 and signal names stay.
        path = "/".join(
            segment
            if not part.startswith("{")
            or part == "{signalId}"
            or _LOGICAL_ID.fullmatch(segment)
            else part
            for segment, part in zip(path.split("/"), template.split("/"), strict=True)
        )
    return prefix + ":".join(
        segment if _LISTED_SEGMENT.fullmatch(segment) else "other"
        for segment in path.strip("/").split("/")
    )


def _listed(key: EntityKey) -> str:
    return f"{key[0]}:{diagnostic_suffix(key[1])}"


def _label(value: str | None) -> str:
    if value is None:
        return "none"
    return value if _LABEL.fullmatch(value) else "other"


def _differences(local: EntityShape, other: EntityShape) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("unit", "device_class", "state_class"):
        ours, theirs = getattr(local, name), getattr(other, name)
        if ours != theirs:
            result[name] = [_label(ours), _label(theirs)]
    if local.options != other.options and (
        local.options is None
        or other.options is None
        or set(local.options) != set(other.options)
    ):
        ours_set, theirs_set = set(local.options or ()), set(other.options or ())
        result["options"] = {
            "local": len(ours_set),
            "cloud": len(theirs_set),
            "shared": len(ours_set & theirs_set),
        }
    return result


def shadow_comparison(
    view: LocalPlantView,
    *,
    cloud: Mapping[EntityKey, EntityShape],
    registry: Mapping[EntityKey, EntityShape],
    cloud_paths: Collection[str] = (),
    templates_compatible: int = 0,
    per_source_entities: int = 0,
) -> dict[str, Any]:
    """Compare the local entity set with the cloud's descriptions and the
    registry: unique IDs after the gateway only, no values or identifiers."""
    local = view.entity_shapes()
    known = cloud.keys() | registry.keys()
    matching = local.keys() & known
    mismatches = []
    for key in sorted(matching):
        other = cloud.get(key)
        if other is None:
            other = registry[key]
            if other.device_class is not None and (
                other.device_class == local[key].device_class
            ):
                # The registry holds the displayed unit, which Home Assistant
                # may convert (for example to °F); the class is compared.
                other = replace(other, unit=local[key].unit)
        if differences := _differences(local[key], other):
            mismatches.append({"id": _listed(key), **differences})
    groups = {
        "matching": [_listed(key) for key in matching],
        "only_local": [_listed(key) for key in local.keys() - known],
        "needs_cloud": [_listed(key) for key in known - local.keys()],
        # Local-only entities of paths the cloud offers under its own ID.
        "local_only_with_cloud_path": [
            _listed(("sensor", sensor.unique_suffix))
            for sensor in view.local_sensors
            if sensor.path in cloud_paths
        ],
    }
    return {
        "local_readings": view.readings,
        "cloud_form_resources": len(view.resources),
        "templates_compatible": templates_compatible,
        "local_sensors": len(view.local_sensors),
        "local_sensors_reviewed": sum(item.reviewed for item in view.local_sensors),
        "local_sensors_enabled": sum(item.enabled for item in view.local_sensors),
        "skipped": dict(view.skipped),
        "counts": {
            "local": len(local),
            "cloud": len(known),
            "per_source_entities": per_source_entities,
            **{name: len(items) for name, items in groups.items()},
            "mismatches": len(mismatches),
        },
        "needs_cloud_domains": dict(
            sorted(Counter(domain for domain, _ in known - local.keys()).items())
        ),
        **{name: sorted(items)[:MAX_LISTED] for name, items in groups.items()},
        "mismatches": mismatches[:MAX_LISTED],
        "truncated": len(mismatches) > MAX_LISTED
        or any(len(items) > MAX_LISTED for items in groups.values()),
        # O94: each list shows at most this many entries; the rest is left out.
        "listed_at_most": MAX_LISTED,
        "omitted_entries": max(0, len(mismatches) - MAX_LISTED)
        + sum(max(0, len(items) - MAX_LISTED) for items in groups.values()),
    }


def compatible_templates(
    view: LocalPlantView, templates: Mapping[str, Resource]
) -> int:
    """How many cloud-form resources a fresh cloud resource could serve."""
    return sum(
        template_compatible(resource, templates[path])
        for path, resource in view.resources.items()
        if path in templates
    )
