"""Compare fresh system evidence once and reuse a scoped retained relationship."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping
from typing import TYPE_CHECKING, Any, Protocol, cast

from homeassistant.core import HomeAssistant

from .configuration import LOCAL_KIND, ConfigurationError, ConfigurationSnapshot
from .gateway_identity import (
    GatewayNamespace,
    IdentityError,
    parse_pointt_gateway_identity,
)
from .local_api.models import SystemInfo
from .local_completion import ScopedIdentityContract
from .local_setup import VerifiedLocalSetup
from .local_state import LocalCondition
from .local_support import SUPPORT_KEY, LocalSetupSupport
from .pointt import AuthenticationError, PointTError, RateLimited

if TYPE_CHECKING:
    from .cloud_configuration import LocalFirstProof

CONTRACT = "pointt-local-system-v1"


def _module_key(row: Mapping[str, object]) -> tuple[str, str]:
    key = row.get("Tok"), row.get("Id")
    if not all(isinstance(v, str) and v and len(v) <= 1024 for v in key):
        raise ConfigurationError("identity_contract_required")
    return cast(tuple[str, str], key)


def compare_systems(local: SystemInfo, cloud: object, gateway: str) -> None:
    """Require exact gateway equality and at least one exact module identity.

    Floating-point serial numbers never establish identity. Extra cloud modules
    are allowed, but duplicate module keys or conflicting comparable identities
    reject the relationship. Firmware versions and measured values are not IDs.
    """
    if (
        not isinstance(cloud, Mapping)
        or cloud.get("id") != "/system/info"
        or cloud.get("type") != "systeminfo"
        or cloud.get("status") != "completed"
        or cloud.get("gatewayId") != gateway
        or local.gateway_id != gateway
        or local.status != "completed"
    ):
        raise ConfigurationError("local_identity_conflict")
    rows = cloud.get("values")
    if (
        not isinstance(rows, list)
        or not 0 < len(rows) <= 512
        or not 0 < len(local.modules) <= 512
    ):
        raise ConfigurationError("identity_contract_required")
    cloud_modules = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ConfigurationError("identity_contract_required")
        key = _module_key(row)
        if key in cloud_modules:
            raise ConfigurationError("local_identity_conflict")
        cloud_modules[key] = row
    seen = set()
    matched = 0
    for module in local.modules:
        key = module.token, module.id
        if key in seen:
            raise ConfigurationError("local_identity_conflict")
        seen.add(key)
        other = cloud_modules.get(key)
        if other is None:
            continue
        serial = other.get("ModuleSerialNumber")
        if (
            type(serial) not in (str, int)
            or not serial
            or not module.serial_number
            or not module.hardware
            or (type(serial) is int and serial <= 0)
            or (
                isinstance(serial, str)
                and (not serial.strip("0") or not serial.strip())
            )
        ):
            continue
        if (
            type(serial) is not type(module.serial_number)
            or serial != module.serial_number
            or other.get("ModuleHwIdentStr") != module.hardware
        ):
            raise ConfigurationError("local_identity_conflict")
        matched += 1
    if not matched:
        raise ConfigurationError("identity_contract_required")


def retained_contract(
    snapshot: ConfigurationSnapshot, target_id: str, gateway_id: str
) -> ScopedIdentityContract | None:
    target = snapshot.targets.get(target_id)
    if target is None or target.kind != "pointt_object":
        return None
    identities = cast(list[dict[str, object]], target.to_data()["identities"])
    if any(
        i.get("namespace") == GatewayNamespace.K40_LOCAL
        and i.get("value") == gateway_id
        and i.get("evidence") == "verified_local_setup"
        and i.get("comparison_contract") == CONTRACT
        for i in identities
    ):
        contract = ScopedIdentityContract(
            snapshot.owner_id, target_id, gateway_id, CONTRACT
        )
        contract.verified_pairs(snapshot, target_id, gateway_id)
        return contract
    return None


class LocalIdentityPolicy:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def verify(
        self, snapshot: ConfigurationSnapshot, target_id: str, proof: VerifiedLocalSetup
    ) -> ScopedIdentityContract | None:
        proof.check(proof.edit)
        proof.edit.check(snapshot)
        if (
            proof.edit.target_id != target_id
            or proof.edit.context.identity.owner_id != snapshot.owner_id
        ):
            raise ConfigurationError("identity_contract_scope_changed")
        gateway = proof.access.gateway_id
        if retained := retained_contract(snapshot, target_id, gateway):
            return retained
        from .gateway_identity import IdentityContext
        from .managed_cloud import CloudConnection
        from .managed_entry import ManagedEntry

        entry = self.hass.config_entries.async_get_entry(snapshot.owner_id)
        owner = getattr(entry, "runtime_data", None)
        if not isinstance(owner, ManagedEntry) or proof.system is None:
            return None
        candidates = [
            (key, binding)
            for key, binding in snapshot.bindings.items()
            if binding.target_id == target_id and binding.adapter == "pointt"
        ]
        if len(candidates) != 1:
            return None
        key, binding = candidates[0]
        context = owner.connections.source_context(key)
        connection = owner.connections.connection_driver(binding.connection_id)
        if context is None or not isinstance(connection, CloudConnection):
            return None
        contract = ScopedIdentityContract(
            snapshot.owner_id, target_id, gateway, CONTRACT
        )
        contract.verified_pairs(snapshot, target_id, gateway)
        try:
            async with asyncio.timeout(30):
                metadata, system = await connection.client.get_local_identity_evidence(
                    gateway
                )
        except AuthenticationError:
            raise ConfigurationError("local_cloud_auth_required") from None
        except RateLimited:
            raise ConfigurationError("local_cloud_rate_limited") from None
        except PointTError, TimeoutError:
            raise ConfigurationError("local_cloud_unavailable") from None
        if not context.current or owner.connections.source_context(key) is not context:
            raise ConfigurationError("configuration_changed")
        proof.check(proof.edit)
        current = owner.connections.management.current()
        proof.edit.check(current)
        try:
            parsed = parse_pointt_gateway_identity(
                metadata,
                context=IdentityContext(
                    GatewayNamespace.POINTT,
                    snapshot.owner_id,
                    binding.connection_id,
                    context.generation.connection_generation,
                ),
            )
        except IdentityError:
            raise ConfigurationError("local_identity_conflict") from None
        if parsed.value != gateway:
            raise ConfigurationError("local_identity_conflict")
        compare_systems(proof.system, system, gateway)
        return contract


class _IdentityEvidence(Protocol):
    async def get_local_identity_evidence(
        self, gateway_id: str
    ) -> tuple[object, object]: ...


def _running_local_source(
    owner: object, snapshot: ConfigurationSnapshot, target_id: str
) -> tuple[str, Any]:
    """The running, ready local access of one plant (8B-a), else an error."""
    from .managed_entry import ManagedEntry

    unavailable = ConfigurationError("cloud_local_first_local_unavailable")
    if not isinstance(owner, ManagedEntry):
        raise unavailable
    candidates = [
        (key, binding)
        for key, binding in snapshot.bindings.items()
        if binding.target_id == target_id and binding.adapter == LOCAL_KIND
    ]
    if len(candidates) != 1:
        raise unavailable
    key, binding = candidates[0]
    options = cast(dict[str, dict[str, dict[str, Any]]], snapshot.to_options())
    runtime = owner.connections
    state = runtime.local_state(key)
    source = runtime.source_driver(key)
    if (
        not options["connections"][binding.connection_id]["enabled"]
        or not options["bindings"][key]["enabled"]
        or state is None
        or state.condition is not LocalCondition.READY
        or not callable(getattr(source, "read_system", None))
    ):
        raise unavailable
    return key, source


async def verify_local_first_merges(
    hass: HomeAssistant,
    snapshot: ConfigurationSnapshot,
    gateways: Collection[str],
    client: Callable[[], _IdentityEvidence],
    connection_id: str,
) -> dict[str, LocalFirstProof]:
    """Compare the cloud's and the local system of each plant of this entry
    that joins the selected cloud (P49b step 8, 8A-a).

    Both sides are read fresh: the cloud's ``/system/info`` and the system
    information through the running local access. Only an exact gateway and
    module match yields a short-lived proof for the configuration builder.
    Cloud errors other than timeouts are left to the calling dialog.
    """
    from .cloud_configuration import (
        LocalFirstProof,
        local_first_targets,
        target_fingerprint,
    )
    from .gateway_identity import IdentityContext

    pending = local_first_targets(
        snapshot.to_data(), snapshot.to_options(), set(gateways)
    )
    entry = hass.config_entries.async_get_entry(snapshot.owner_id)
    owner = getattr(entry, "runtime_data", None)
    proofs = {}
    # The cloud client is created only for such a plant: saving any other
    # selection makes no request (and is not blocked by a cloud pause).
    cloud = client() if pending else None
    for gateway, target_id in sorted(pending.items()):
        assert cloud is not None
        binding_id, source = _running_local_source(owner, snapshot, target_id)
        fingerprint = target_fingerprint(snapshot.targets[target_id].to_data())
        try:
            async with asyncio.timeout(30):
                local = await source.read_system()
        except TimeoutError:
            local = None
        if local == "busy":
            raise ConfigurationError("cloud_local_first_local_busy")
        if not isinstance(local, SystemInfo):
            raise ConfigurationError("cloud_local_first_local_unavailable")
        try:
            async with asyncio.timeout(30):
                metadata, system = await cloud.get_local_identity_evidence(gateway)
        except TimeoutError:
            raise ConfigurationError("cannot_connect") from None
        current = cast(Any, owner).connections.management.current()
        target = current.targets.get(target_id)
        if (
            target is None
            or target_fingerprint(target.to_data()) != fingerprint
            or cast(Any, owner).connections.source_driver(binding_id) is not source
        ):
            raise ConfigurationError("configuration_changed")
        try:
            parsed = parse_pointt_gateway_identity(
                metadata,
                context=IdentityContext(
                    GatewayNamespace.POINTT, snapshot.owner_id, connection_id, 0
                ),
            )
            if parsed.value != gateway:
                raise ConfigurationError("local_identity_conflict")
            compare_systems(local, system, gateway)
        except IdentityError, ConfigurationError:
            raise ConfigurationError("cloud_local_first_conflict") from None
        proofs[gateway] = LocalFirstProof(
            snapshot.owner_id, target_id, gateway, binding_id, fingerprint
        )
    return proofs


def install_local_support(hass: HomeAssistant) -> None:
    policy = LocalIdentityPolicy(hass)
    hass.data[SUPPORT_KEY] = LocalSetupSupport(
        lambda: True, retained_contract, policy.verify
    )
