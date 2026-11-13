"""Verify and propose local access on an explicitly selected retained target."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from time import monotonic, time
from typing import cast
from uuid import uuid4

from .configuration import (
    LOCAL_KIND,
    ConfigurationError,
    ConfigurationSnapshot,
    JsonObject,
    json_object,
    load_configuration,
    require_identifier,
)
from .gateway_identity import (
    GatewayNamespace,
    GatewayReference,
    IdentityContext,
    IdentityMatchStatus,
    NamespacePair,
    match_gateway_identity,
)
from .local_api.catalog import request_for_path
from .local_api.decoder import decode_resource
from .local_api.models import SystemInfo
from .local_budget import LocalReadBudget
from .local_credentials import (
    LOCAL_STORAGE_MINOR_VERSION,
    LocalCredentials,
    StoredLocalAccess,
    TokenOrigin,
)
from .local_probe import (
    LocalEndpoint,
    ProbeContext,
    ProbePath,
    ProbeStatus,
    assess_local_probe,
)
from .local_transport import (
    CertificateBinding,
    PortRole,
    StaleLocalRequest,
    read_local_probe,
)
from .plant_model import PlantKey

# An in-process receipt expires; this is not a token lifetime or vendor limit.
VERIFICATION_VALID_SECONDS = 60
_attempts = count()


class LocalSetupProbeError(ConfigurationError):
    """Retain a server's pause without exposing its response text."""

    def __init__(self, code: str, retry_after_seconds: float | None) -> None:
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds


# P49b step 7: the reserved ID of a plant set up from its local access alone.
NEW_PLANT_PREFIX = "target_local_"
_CONTROLLER = re.compile(r"(?<![A-Z0-9])MX(?:300|400)(?![A-Z0-9])", re.IGNORECASE)


def new_local_plant_id() -> str:
    """A reserved target ID for a plant without any cloud (local first)."""
    return NEW_PLANT_PREFIX + uuid4().hex


def is_new_local_plant(snapshot: ConfigurationSnapshot, target_id: str) -> bool:
    return target_id not in snapshot.targets and target_id.startswith(NEW_PLANT_PREFIX)


def _section(data: JsonObject, key: str) -> dict[str, JsonObject]:
    return cast(dict[str, JsonObject], data[key])


def _basis(snapshot: ConfigurationSnapshot, connection_id: str, target_id: str) -> str:
    """Ignore unrelated tokens and settings, but include this access's credentials."""
    target = snapshot.targets.get(target_id)
    if not is_new_local_plant(snapshot, target_id) and (
        target is None or target.kind not in ("pointt_object", "local_installation")
    ):
        raise ConfigurationError("local_target_unavailable")
    connection = snapshot.connections.get(connection_id)
    if connection is not None and connection.kind != LOCAL_KIND:
        raise ConfigurationError("unsupported_connection")
    data, options = snapshot.to_data(), snapshot.to_options()
    selected = {}
    for key, binding in snapshot.bindings.items():
        if binding.connection_id == connection_id:
            if binding.target_id != target_id or binding.adapter != LOCAL_KIND:
                raise ConfigurationError("local_target_changed")
        elif binding.target_id != target_id or binding.adapter != LOCAL_KIND:
            continue
        else:
            raise ConfigurationError("local_already_connected")
        selected[key] = {
            "binding": _section(data, "bindings")[key],
            "options": _section(options, "bindings")[key],
        }
    return json.dumps(
        [
            snapshot.owner_id,
            snapshot.version,
            snapshot.minor_version,
            connection.to_data() if connection else None,
            _section(options, "connections").get(connection_id),
            target.to_data() if target is not None else None,
            _section(options, "targets").get(target_id),
            selected,
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class LocalSetupEdit:
    """One selected connection and target, retaining their exact existing keys."""

    context: ProbeContext = field(repr=False)
    target_id: str = field(repr=False)
    binding_id: str = field(repr=False)
    _signature: str = field(repr=False)

    def recovery_fingerprint(self) -> str:
        """Bind a pending enrollment to this basis without copying old secrets."""
        return hashlib.sha256(self._signature.encode()).hexdigest()

    def check(self, current: ConfigurationSnapshot) -> None:
        if self._signature != _basis(
            current, self.context.identity.connection_id, self.target_id
        ):
            raise ConfigurationError("configuration_changed")


def begin_local_setup(
    snapshot: ConfigurationSnapshot,
    target_id: str,
    endpoint: LocalEndpoint,
    *,
    connection_id: str | None = None,
    reserved_ids: tuple[str, str] | None = None,
) -> LocalSetupEdit:
    """Select a retained target; discovery and local-first setup are separate."""
    target_id = require_identifier(target_id)
    if reserved_ids is not None:
        if connection_id is not None:
            raise ConfigurationError("invalid_reserved_local_ids")
        for value in reserved_ids:
            require_identifier(value)
        if (
            reserved_ids[0] in snapshot.connections
            or reserved_ids[1] in snapshot.bindings
        ):
            raise ConfigurationError("reserved_local_ids_in_use")
    if connection_id is None:
        connection_id = reserved_ids[0] if reserved_ids else "connection_" + uuid4().hex
    else:
        require_identifier(connection_id)
        if connection_id not in snapshot.connections:
            raise ConfigurationError("connection_removed")
    signature = _basis(snapshot, connection_id, target_id)
    previous = [
        b for b, v in snapshot.bindings.items() if v.connection_id == connection_id
    ]
    binding_id = (
        previous[0]
        if previous
        else (reserved_ids[1] if reserved_ids else "binding_" + uuid4().hex)
    )
    return LocalSetupEdit(
        ProbeContext(
            IdentityContext(
                GatewayNamespace.K40_LOCAL,
                snapshot.owner_id,
                connection_id,
                next(_attempts),
            ),
            endpoint,
        ),
        target_id,
        binding_id,
        signature,
    )


@dataclass(frozen=True, slots=True)
class VerifiedLocalSetup:
    """Short-lived transport evidence tied to the exact setup attempt."""

    edit: LocalSetupEdit = field(repr=False)
    access: StoredLocalAccess = field(repr=False)
    _accepted_at: float = field(repr=False)
    _is_active: Callable[[], bool] = field(repr=False)
    system: SystemInfo | None = field(default=None, repr=False)
    # Shown as manufacturer and model of a plant without a cloud; never an
    # identifier or serial number.
    brand: str | None = field(default=None, repr=False)
    model: str | None = field(default=None, repr=False)

    def check(self, edit: LocalSetupEdit) -> None:
        age = monotonic() - self._accepted_at
        if (
            self.edit is not edit
            or not self._is_active()
            or not 0 <= age <= VERIFICATION_VALID_SECONDS
        ):
            raise ConfigurationError("local_verification_expired")


async def verify_local_setup(
    edit: LocalSetupEdit,
    credentials: LocalCredentials,
    resource_binding: CertificateBinding,
    *,
    current: Callable[[], ConfigurationSnapshot],
    is_active: Callable[[], bool],
    authentication_binding: CertificateBinding | None = None,
    budget: LocalReadBudget | None = None,
) -> VerifiedLocalSetup:
    """Use the actual read transport and publish no proof after cancellation.

    Authentication transport supplies credentials from a successful response
    only after checking its own port binding. It does not infer token ownership.
    """
    edit.check(current())
    if (
        resource_binding.context != edit.context
        or resource_binding.role != PortRole.RESOURCES
    ):
        raise ConfigurationError("certificate_context_changed")
    if authentication_binding is not None and (
        authentication_binding.context != edit.context
        or authentication_binding.role != PortRole.AUTHENTICATION
    ):
        raise ConfigurationError("certificate_context_changed")
    if credentials.origin == TokenOrigin.CREATED and authentication_binding is None:
        raise ConfigurationError("authentication_binding_required")

    def accepts() -> bool:
        if not is_active():
            return False
        try:
            edit.check(current())
        except ConfigurationError:
            return False
        return True

    if budget is None:
        exchange = await read_local_probe(
            edit.context, credentials.token, resource_binding, is_current=accepts
        )
    else:
        exchange = await read_local_probe(
            edit.context,
            credentials.token,
            resource_binding,
            is_current=accepts,
            budget=budget,
        )
    if not accepts():
        raise StaleLocalRequest()
    assessment = assess_local_probe(exchange.responses, current_context=edit.context)
    if assessment.status != ProbeStatus.READY_FOR_IDENTITY_COMPARISON:
        raise LocalSetupProbeError(
            assessment.issues[0].problem.value, exchange.retry_after_seconds
        )
    assert assessment.identity is not None
    system_response = next(r for r in exchange.responses if r.path is ProbePath.SYSTEM)
    decoded = decode_resource(
        request_for_path(ProbePath.SYSTEM.value),
        system_response.body,
        context=edit.context.identity,
        observed_at=datetime.now(UTC),
        observed_monotonic=monotonic(),
    )
    assert isinstance(decoded.data, SystemInfo)
    model = next(
        (
            match.group(0).upper()
            for module in decoded.data.modules
            if module.product_name
            and (match := _CONTROLLER.search(module.product_name)) is not None
        ),
        None,
    )
    if budget is not None:
        await budget.bind_gateway(assessment.identity.value)
    return VerifiedLocalSetup(
        edit,
        StoredLocalAccess(
            credentials,
            edit.context.endpoint,
            resource_binding.sha256.hex(),
            authentication_binding.sha256.hex() if authentication_binding else None,
            assessment.identity.value,
            time(),
        ),
        monotonic(),
        is_active,
        decoded.data,
        assessment.brand.value if assessment.brand is not None else None,
        model,
    )


def gateway_references(
    snapshots: Collection[ConfigurationSnapshot],
) -> tuple[GatewayReference, ...]:
    """Keep disabled entries and disconnected retained targets in the comparison."""
    references = []
    for snapshot in snapshots:
        for target_id, target in snapshot.targets.items():
            for item in cast(list[JsonObject], target.to_data()["identities"]):
                if item["namespace"] not in tuple(GatewayNamespace):
                    continue
                references.append(
                    GatewayReference(
                        PlantKey(snapshot.owner_id, target_id),
                        GatewayNamespace(item["namespace"]),
                        cast(str, item["value"]),
                    )
                )
    return tuple(references)


def prepare_local_configuration(
    edit: LocalSetupEdit,
    verification: VerifiedLocalSetup,
    snapshots: Collection[ConfigurationSnapshot],
    *,
    verified_namespace_pairs: Collection[NamespacePair] = (),
    contract_id: str | None = None,
) -> ConfigurationSnapshot:
    """Build v4.2 data without mutation, preserving current unrelated changes.

    The caller supplies the complete current inventory and only namespace
    contracts validated for this selected device. No global cloud/local match
    is enabled here. The HA owner must recheck and save without an await.
    """
    verification.check(edit)
    owners = [s for s in snapshots if s.owner_id == edit.context.identity.owner_id]
    if len(owners) != 1:
        raise ConfigurationError("owner_changed")
    current = owners[0]
    edit.check(current)
    if verified_namespace_pairs and contract_id is None:
        raise ConfigurationError("identity_contract_required")
    if contract_id is not None:
        require_identifier(contract_id)
    from .gateway_identity import GatewayIdentity

    identity = GatewayIdentity(edit.context.identity, verification.access.gateway_id)
    new_plant = is_new_local_plant(current, edit.target_id)
    if new_plant:
        # P49b step 7: no cloud to compare with. The gateway must not belong
        # to any known plant under any identity, in this or another entry.
        if verified_namespace_pairs or any(
            reference.value == identity.value
            for reference in gateway_references(snapshots)
        ):
            raise ConfigurationError("local_plant_known")
        contract_id = None
    else:
        match = match_gateway_identity(
            identity,
            current_context=edit.context.identity,
            expected_plant=PlantKey(current.owner_id, edit.target_id),
            references=gateway_references(snapshots),
            verified_namespace_pairs=verified_namespace_pairs,
        )
        if match.status != IdentityMatchStatus.MATCHED:
            raise ConfigurationError("local_" + match.reason)
    connection_id = edit.context.identity.connection_id
    data, options = current.to_data(), current.to_options()
    connections = _section(data, "connections")
    previous = connections.get(connection_id)
    connections[connection_id] = {
        **(previous or {}),
        "kind": LOCAL_KIND,
        # Even reusing an identical token is an explicit new verification.
        "auth_epoch": cast(int, previous["auth_epoch"]) + 1 if previous else 0,
        "config_revision": cast(int, previous["config_revision"]) + 1
        if previous
        else 1,
        **json_object(verification.access.to_data()),
    }
    locator: JsonObject = {
        "namespace": GatewayNamespace.K40_LOCAL.value,
        "value": identity.value,
    }
    if new_plant:
        details: JsonObject = {}
        if verification.brand:
            details["brand"] = verification.brand
        if verification.model:
            details["model"] = verification.model
        _section(data, "targets")[edit.target_id] = {
            "kind": "local_installation",
            "identities": [],
            # Shown as the device's manufacturer and model; no identifiers.
            "local_details": details,
        }
        # Shown read-only from its local readings (P49b, R1).
        _section(options, "targets")[edit.target_id] = {
            "read_order": [],
            "local_plant": True,
        }
    target = _section(data, "targets")[edit.target_id]
    identities = cast(list[JsonObject], target["identities"])
    if not any(item["namespace"] == GatewayNamespace.K40_LOCAL for item in identities):
        identities.append({**locator, "evidence": "verified_local_setup"})
    if contract_id is not None:
        for item in identities:
            if (
                item["namespace"] == GatewayNamespace.K40_LOCAL
                and item["value"] == identity.value
            ):
                item["comparison_contract"] = contract_id
                item["evidence"] = "verified_local_setup"
    bindings = _section(data, "bindings")
    if edit.binding_id not in bindings and not new_plant:
        # A system's first local access shows its local readings in the
        # existing entities by default. A stored choice stays; a changed
        # local access and older entries without the option are unchanged.
        _section(options, "targets")[edit.target_id].setdefault("local_values", True)
    bindings[edit.binding_id] = {
        **bindings.get(edit.binding_id, {}),
        "connection_id": connection_id,
        "target_id": edit.target_id,
        "adapter": LOCAL_KIND,
        "locator": locator,
        "link_evidence": {"basis": "verified_local_setup", "contract": contract_id},
    }
    _section(options, "connections").setdefault(connection_id, {"enabled": True})
    _section(options, "bindings").setdefault(edit.binding_id, {"enabled": True})
    order = cast(list[str], _section(options, "targets")[edit.target_id]["read_order"])
    if edit.binding_id not in order:
        order.append(edit.binding_id)
    data["revision"] = current.revision + 1
    return load_configuration(
        current.owner_id,
        data,
        options,
        # A newer minor version stays; it is never lowered.
        minor_version=max(LOCAL_STORAGE_MINOR_VERSION, current.minor_version),
    )


def commit_local_setup(
    management: object,
    edit: LocalSetupEdit,
    verification: VerifiedLocalSetup,
    *,
    verified_namespace_pairs: Collection[NamespacePair] = (),
    contract_id: str | None = None,
) -> ConfigurationSnapshot:
    """Recheck all owners and hand one proposal to HA's guarded update callback."""
    from .cloud_configuration import cloud_snapshots
    from .configuration_management import ConfigurationManagement

    if not isinstance(management, ConfigurationManagement):
        raise ConfigurationError("invalid_configuration_owner")
    before = management.current()
    if before.owner_id != edit.context.identity.owner_id:
        raise ConfigurationError("owner_changed")
    proposed = prepare_local_configuration(
        edit,
        verification,
        cloud_snapshots(management.hass),
        verified_namespace_pairs=verified_namespace_pairs,
        contract_id=contract_id,
    )
    try:
        return management.commit_local_access(before, proposed)
    except Exception as error:
        # A listener may fail after HA already accepted the new configuration.
        # Do not overwrite subsequent state or pretend the update was rolled back.
        try:
            unchanged = management.current() == before
        except ConfigurationError:
            unchanged = False
        if unchanged and isinstance(error, ConfigurationError):
            raise
        raise ConfigurationError(
            "local_save_failed" if unchanged else "local_save_outcome_uncertain"
        ) from None
