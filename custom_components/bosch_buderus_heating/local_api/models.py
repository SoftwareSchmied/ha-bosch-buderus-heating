"""Immutable local observations, separate from cloud resources and HA entities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from ..gateway_identity import IdentityContext
from .catalog import ReadRequest

type Number = int | Decimal
type Scalar = str | bool | Number
type JsonValue = Scalar | tuple[JsonValue, ...] | Mapping[str, JsonValue] | None
type JsonObject = Mapping[str, JsonValue]


class DataIssue(StrEnum):
    UNKNOWN_UNIT = "unknown_unit"
    MISSING_UNIT = "missing_unit"
    UNSUPPORTED_TEXT_ENCODING = "unsupported_text_encoding"


@dataclass(frozen=True, slots=True, repr=False)
class NamedNumber:
    name: str
    value: Number


@dataclass(frozen=True, slots=True, repr=False)
class ScalarValue:
    value: Scalar
    unit: str | None
    allowed_values: tuple[Scalar, ...]
    minimum: Number | None
    maximum: Number | None
    step: Number | None
    states: tuple[NamedNumber, ...]
    matched_states: tuple[str, ...]
    decoded_text: str | None
    issues: tuple[DataIssue, ...]

    @property
    def measurement(self) -> Number | None:
        if (
            (type(self.value) is int or isinstance(self.value, Decimal))
            and not self.matched_states
            and DataIssue.UNKNOWN_UNIT not in self.issues
        ):
            return self.value
        return None


@dataclass(frozen=True, slots=True, repr=False)
class ValueArray:
    values: tuple[str, ...]
    allowed_values: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class NamedValues:
    values: tuple[NamedNumber, ...]
    unit: str | None
    issues: tuple[DataIssue, ...]


@dataclass(frozen=True, slots=True, repr=False)
class ResourceReference:
    id: str
    uri: str


@dataclass(frozen=True, slots=True, repr=False)
class ReferenceList:
    references: tuple[ResourceReference, ...]

    @property
    def signal_ids(self) -> frozenset[str]:
        return frozenset(
            ref.id[len("/signals/") :]
            for ref in self.references
            if ref.id.startswith("/signals/") and "/" not in ref.id[len("/signals/") :]
        )


@dataclass(frozen=True, slots=True, repr=False)
class ConfigurationValue:
    values: JsonObject


@dataclass(frozen=True, slots=True, repr=False)
class SystemModule:
    token: str
    id: str
    version: str
    serial_number: str | int
    hardware: str
    product_serial: str | None
    product_name: str | None


@dataclass(frozen=True, slots=True, repr=False)
class SystemInfo:
    status: str
    timestamp: str
    gateway_id: str
    modules: tuple[SystemModule, ...]


@dataclass(frozen=True, slots=True, repr=False)
class Fault:
    display_code: str
    cause_code: Number
    origin: str
    level: str
    fault_class: str


@dataclass(frozen=True, slots=True, repr=False)
class FaultList:
    faults: tuple[Fault, ...]


@dataclass(frozen=True, slots=True, repr=False)
class DeviceFault:
    type: str
    occurrence_id: Number
    value: str
    allowed_values: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class DeviceFaultList:
    faults: tuple[DeviceFault, ...]


@dataclass(frozen=True, slots=True, repr=False)
class AccessPoint:
    encoded_ssid: str
    strength: str
    security_type: str


@dataclass(frozen=True, slots=True, repr=False)
class AccessPointList:
    access_points: tuple[AccessPoint, ...]


@dataclass(frozen=True, slots=True, repr=False)
class DevicePattern:
    type: str
    pattern: str


@dataclass(frozen=True, slots=True, repr=False)
class InclusionEntry:
    assigned_circuit: str
    actual_devices: int
    max_supported_devices: int
    patterns: tuple[DevicePattern, ...]


@dataclass(frozen=True, slots=True, repr=False)
class InclusionWhitelist:
    entries: tuple[InclusionEntry, ...]


@dataclass(frozen=True, slots=True, repr=False)
class UpdateStatus:
    """Schema-checked immutable status/progress fields, including legacy names."""

    value: JsonObject


@dataclass(frozen=True, slots=True, repr=False)
class UpdateModule:
    module_id: str | int
    status: str
    timestamp: str
    fields: JsonObject


@dataclass(frozen=True, slots=True, repr=False)
class UpdateReport:
    timestamp: str
    recipe_id: str
    status: str
    gateway_id: str
    modules: tuple[UpdateModule, ...]
    logs: tuple[JsonObject, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RecordingSample:
    value: Number
    count: Number


@dataclass(frozen=True, slots=True, repr=False)
class RecordingGroup:
    sample_rate: str
    start: datetime
    end: datetime
    samples: tuple[RecordingSample, ...]


@dataclass(frozen=True, slots=True, repr=False)
class Recording:
    resource: ResourceReference
    recording_type: str
    unit: str | None
    groups: tuple[RecordingGroup, ...]
    issues: tuple[DataIssue, ...]


type ResourceData = (
    ScalarValue
    | ValueArray
    | NamedValues
    | ReferenceList
    | ConfigurationValue
    | SystemInfo
    | FaultList
    | DeviceFaultList
    | AccessPointList
    | InclusionWhitelist
    | UpdateStatus
    | UpdateReport
    | Recording
)


@dataclass(frozen=True, slots=True, repr=False)
class LocalObservation:
    request: ReadRequest
    context: IdentityContext
    observed_at: datetime
    observed_monotonic: float
    type: str
    reported_writeable: bool
    recordable: bool | None
    data: ResourceData
    document: JsonObject

    @property
    def can_write(self) -> Literal[False]:
        return False
