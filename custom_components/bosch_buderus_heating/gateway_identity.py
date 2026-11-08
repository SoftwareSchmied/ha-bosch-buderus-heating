"""Parse gateway identity evidence without networking or registry mutations."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .plant_model import PlantKey

# A defensive input limit, not a vendor-defined identifier length.
MAX_IDENTIFIER_LENGTH = 1024
_POINTT_ID_FIELDS = ("deviceId", "id", "gatewayId", "gateway_id")


class GatewayNamespace(StrEnum):
    """Keep the known identity sources distinct until a contract connects them."""

    POINTT = "pointt_gateway"
    K40_LOCAL = "local_system_gateway"


class IdentityProblem(StrEnum):
    """Static parser errors safe to expose without the original payload."""

    WRONG_SOURCE = "wrong_source"
    INVALID_PAYLOAD = "invalid_payload"
    MISSING_IDENTITY = "identity_missing"
    INVALID_IDENTITY = "identity_invalid"
    CONFLICTING_ALIASES = "identity_alias_conflict"
    WRONG_RESOURCE = "wrong_resource"
    WRONG_RESOURCE_TYPE = "wrong_resource_type"
    INCOMPLETE_RESPONSE = "incomplete_response"
    INVALID_ENVELOPE = "invalid_envelope"


class IdentityError(ValueError):
    """Reject unusable evidence without retaining any source values."""

    def __init__(self, problem: IdentityProblem) -> None:
        self.problem = problem
        super().__init__(problem.value)


def _identifier(value: object) -> str:
    """Preserve opaque strings; reject coercion, whitespace and control text."""
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_IDENTIFIER_LENGTH
        or not value.isprintable()
        or any(character.isspace() for character in value)
    ):
        raise IdentityError(IdentityProblem.INVALID_IDENTITY)
    return value


@dataclass(frozen=True, slots=True)
class IdentityContext:
    """Capture the owner and connection generation before starting a request.

    The caller advances the generation when connection settings change.
    Retagging an old response with the current generation is not permitted.
    """

    namespace: GatewayNamespace
    owner_id: str = field(repr=False)
    connection_id: str = field(repr=False)
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, GatewayNamespace):
            raise ValueError("Invalid identity namespace")
        _identifier(self.owner_id)
        _identifier(self.connection_id)
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("Invalid connection generation")


@dataclass(frozen=True, slots=True)
class GatewayIdentity:
    """A parsed identifier with its request provenance, never a device factory."""

    context: IdentityContext
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        _identifier(self.value)


def _payload(
    payload: object, context: IdentityContext, namespace: GatewayNamespace
) -> Mapping[str, object]:
    if context.namespace != namespace:
        raise IdentityError(IdentityProblem.WRONG_SOURCE)
    if not isinstance(payload, Mapping):
        raise IdentityError(IdentityProblem.INVALID_PAYLOAD)
    return payload


def parse_pointt_gateway_identity(
    payload: object, *, context: IdentityContext
) -> GatewayIdentity:
    """Read one raw PointT gateway object and reject contradictory ID aliases.

    Run before the existing tolerant Gateway parser discards alias information.
    Unknown fields, including model names and product serials, are not retained.
    """
    data = _payload(payload, context, GatewayNamespace.POINTT)
    identifiers = {_identifier(data[key]) for key in _POINTT_ID_FIELDS if key in data}
    if not identifiers:
        raise IdentityError(IdentityProblem.MISSING_IDENTITY)
    if len(identifiers) != 1:
        raise IdentityError(IdentityProblem.CONFLICTING_ALIASES)
    return GatewayIdentity(context, identifiers.pop())


def parse_local_gateway_identity(
    payload: object, *, context: IdentityContext
) -> GatewayIdentity:
    """Read the identity envelope of a completed K40/MX400 system response.

    The timestamp is checked for its documented shape, not used as a freshness
    clock. Module contents inside values are deliberately outside this parser.
    No module identifiers, measurements or unknown fields are retained.
    """
    data = _payload(payload, context, GatewayNamespace.K40_LOCAL)
    if data.get("id") != "/system/basicInfo":
        raise IdentityError(IdentityProblem.WRONG_RESOURCE)
    if data.get("type") != "systeminfo":
        raise IdentityError(IdentityProblem.WRONG_RESOURCE_TYPE)
    if data.get("status") != "completed":
        raise IdentityError(IdentityProblem.INCOMPLETE_RESPONSE)
    timestamp = data.get("timeStamp")
    writable = data.get("writeable")
    if (
        not isinstance(timestamp, str)
        or re.fullmatch(r"(?:[0-9]{10})?", timestamp) is None
        or type(writable) is not int
        or writable != 0
        or not isinstance(data.get("values"), list)
    ):
        raise IdentityError(IdentityProblem.INVALID_ENVELOPE)
    if "gatewayId" not in data:
        raise IdentityError(IdentityProblem.MISSING_IDENTITY)
    return GatewayIdentity(context, _identifier(data["gatewayId"]))


@dataclass(frozen=True, slots=True)
class GatewayReference:
    """An existing owner-managed identity, not an unprocessed response.

    The owner supplies retained, previously accepted references independently of
    cloud availability. Admitting and persisting references is a separate layer.
    An incoming response must first be checked against its current context.
    """

    plant: PlantKey = field(repr=False)
    namespace: GatewayNamespace
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, GatewayNamespace):
            raise ValueError("Invalid identity namespace")
        _identifier(self.plant.owner_id)
        _identifier(self.plant.target_id)
        _identifier(self.value)


class IdentityMatchStatus(StrEnum):
    """A decision about a selected installation, without applying it."""

    MATCHED = "matched"
    UNVERIFIED = "unverified"
    CONFLICT = "conflict"
    OWNER_CONFLICT = "owner_conflict"
    AMBIGUOUS = "ambiguous"


type MatchReason = Literal[
    "matched",
    "stale_observation",
    "target_missing",
    "unverified_namespace",
    "identity_mismatch",
    "owner_conflict",
    "ambiguous",
]
type NamespacePair = tuple[GatewayNamespace, GatewayNamespace]


@dataclass(frozen=True, slots=True)
class IdentityMatch:
    """Expose safe decision codes; retain the existing key only on success."""

    status: IdentityMatchStatus
    reason: MatchReason
    plant: PlantKey | None = field(default=None, repr=False)


def match_gateway_identity(
    identity: GatewayIdentity,
    *,
    current_context: IdentityContext,
    expected_plant: PlantKey,
    references: Collection[GatewayReference],
    verified_namespace_pairs: Collection[NamespacePair] = (),
) -> IdentityMatch:
    """Compare a new observation with an explicitly selected existing plant.

    Cross-source equality alone is insufficient. The caller must supply only
    namespace pairs verified for the device scope of this particular comparison.
    No cross-source contract is enabled globally by this module.

    References must cover the relevant configured inventory, including disabled
    entries, so another owner or installation cannot be selected by accident.
    This function neither registers a device nor grants connection/write access.
    """
    if identity.context != current_context:
        return IdentityMatch(IdentityMatchStatus.UNVERIFIED, "stale_observation")
    if current_context.owner_id != expected_plant.owner_id:
        return IdentityMatch(IdentityMatchStatus.OWNER_CONFLICT, "owner_conflict")

    def comparable(reference: GatewayReference) -> bool:
        source = identity.context.namespace
        target = reference.namespace
        return (
            source == target
            or (source, target) in verified_namespace_pairs
            or (target, source) in verified_namespace_pairs
        )

    comparable_references = tuple(
        reference for reference in references if comparable(reference)
    )
    matching_plants = {
        reference.plant
        for reference in comparable_references
        if reference.value == identity.value
    }
    if any(plant.owner_id != expected_plant.owner_id for plant in matching_plants):
        return IdentityMatch(IdentityMatchStatus.OWNER_CONFLICT, "owner_conflict")
    if len(matching_plants) > 1:
        return IdentityMatch(IdentityMatchStatus.AMBIGUOUS, "ambiguous")
    if not any(reference.plant == expected_plant for reference in references):
        return IdentityMatch(IdentityMatchStatus.UNVERIFIED, "target_missing")
    selected = tuple(
        reference
        for reference in comparable_references
        if reference.plant == expected_plant
    )
    if not selected:
        return IdentityMatch(IdentityMatchStatus.UNVERIFIED, "unverified_namespace")
    if any(reference.value != identity.value for reference in selected):
        return IdentityMatch(IdentityMatchStatus.CONFLICT, "identity_mismatch")
    return IdentityMatch(IdentityMatchStatus.MATCHED, "matched", expected_plant)
