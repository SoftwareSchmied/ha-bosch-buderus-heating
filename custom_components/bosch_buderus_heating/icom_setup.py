"""Verify and save the local access of an iCom gateway.

The gateway must already be known by the cloud (a plant of this entry): the
local access is proved to be the same gateway by its own ID, which must equal
the cloud's gateway ID. Without a match nothing is saved. The check reads one
resource and sends the passwords nowhere else.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from time import time
from typing import cast
from uuid import uuid4

import aiohttp

from .configuration import (
    LOCAL_KIND,
    ConfigurationError,
    ConfigurationSnapshot,
    JsonObject,
    load_configuration,
)
from .configuration_management import ConfigurationManagement
from .gateway_identity import GatewayNamespace
from .icom_access import (
    ICOM_CONTRACT,
    ICOM_DEFAULT_PORT,
    gateway_ids_match,
    icom_connection_data,
    is_icom_connection,
)
from .icom_client import IcomClient, IcomError, IcomProblem
from .icom_crypto import IcomCryptoError, derive_key
from .icom_plan import UUID_PATH
from .local_credentials import LOCAL_STORAGE_MINOR_VERSION
from .local_probe import LocalProbeError, normalize_local_address

# What a failed check reports; each code has its own text in the dialog.
CANNOT_CONNECT = "icom_cannot_connect"
INVALID_AUTH = "icom_invalid_auth"
BUSY = "icom_busy"
NO_IDENTITY = "icom_no_identity"
OTHER_GATEWAY = "icom_other_gateway"
INVALID_HOST = "icom_invalid_host"
_PROBLEM_CODES = {
    IcomProblem.UNDECRYPTABLE: INVALID_AUTH,
    IcomProblem.BUSY: BUSY,
    IcomProblem.NOT_FOUND: NO_IDENTITY,
    IcomProblem.FORBIDDEN: NO_IDENTITY,
}


class IcomSetupError(ConfigurationError):
    """A fixed code for the dialog, never a password or an ID."""


def normalize_icom_host(value: object) -> str:
    """The address as entered, checked like every local address.

    IPv6 link-local addresses with a zone are not supported by this access.
    """
    try:
        host = normalize_local_address(value)
    except LocalProbeError:
        raise IcomSetupError(INVALID_HOST) from None
    if "%" in host:
        raise IcomSetupError(INVALID_HOST)
    return host


@dataclass(frozen=True, slots=True)
class IcomVerification:
    """The proof of one successful check; short-lived data of the dialog."""

    target_id: str = field(repr=False)
    gateway_id: str = field(repr=False)
    local_id: str = field(repr=False)
    host: str = field(repr=False)
    port: int = field(repr=False)
    gateway_password: str = field(repr=False)
    private_password: str = field(repr=False)
    verified_at: float = field(repr=False)


async def verify_icom_access(
    session: aiohttp.ClientSession,
    *,
    target_id: str,
    gateway_id: str,
    host: str,
    gateway_password: str,
    private_password: str,
    also_accepted: Collection[str] = (),
    port: int = ICOM_DEFAULT_PORT,
) -> IcomVerification:
    """Read the gateway's own ID and require it to be the cloud's gateway.

    The IDs in also_accepted are further IDs the cloud itself reports for the same
    gateway (its reading of the same resource); the first one that matches
    is kept as the ID to check later.
    """
    try:
        key = derive_key(gateway_password, private_password)
    except IcomCryptoError as error:
        raise IcomSetupError(str(error)) from None
    address = normalize_icom_host(host)
    client = IcomClient(session, address, key, port=port)
    try:
        reply = await client.get(UUID_PATH)
    except IcomError as error:
        raise IcomSetupError(
            _PROBLEM_CODES.get(error.problem, CANNOT_CONNECT)
        ) from None
    reported = reply.document.get("value")
    if not isinstance(reported, str) or not any(
        gateway_ids_match(reported, expected)
        for expected in (gateway_id, *also_accepted)
    ):
        raise IcomSetupError(OTHER_GATEWAY)
    return IcomVerification(
        target_id,
        gateway_id,
        reported,
        address,
        port,
        gateway_password,
        private_password,
        time(),
    )


def _section(data: JsonObject, key: str) -> dict[str, JsonObject]:
    return cast(dict[str, JsonObject], data[key])


def prepare_icom_configuration(
    current: ConfigurationSnapshot, verification: IcomVerification
) -> ConfigurationSnapshot:
    """Build the configuration with the access, without changing anything.

    A plant of the cloud gets its first local access, or a plant with an
    iCom access gets new data for it (new passwords, a new address). Another
    kind of local access of the plant is never replaced.
    """
    target = current.targets.get(verification.target_id)
    if target is None or target.kind != "pointt_object":
        raise IcomSetupError("local_target_unavailable")
    identities = cast(list[JsonObject], target.to_data()["identities"])
    cloud_ids = [
        item["value"]
        for item in identities
        if item["namespace"] == GatewayNamespace.POINTT
    ]
    if cloud_ids != [verification.gateway_id]:
        raise IcomSetupError("identity_contract_scope_changed")
    existing = [
        (key, binding)
        for key, binding in current.bindings.items()
        if binding.target_id == verification.target_id and binding.adapter == LOCAL_KIND
    ]
    if len(existing) > 1 or (
        existing
        and not is_icom_connection(
            current.connections[existing[0][1].connection_id].to_data()
        )
    ):
        raise IcomSetupError("local_already_connected")
    if existing:
        binding_id, binding = existing[0]
        connection_id = binding.connection_id
    else:
        binding_id, connection_id = (
            "binding_" + uuid4().hex,
            "connection_" + uuid4().hex,
        )
    data, options = current.to_data(), current.to_options()
    connections = _section(data, "connections")
    previous = connections.get(connection_id)
    connections[connection_id] = {
        **(previous or {}),
        "kind": LOCAL_KIND,
        "auth_epoch": cast(int, previous["auth_epoch"]) + 1 if previous else 0,
        "config_revision": cast(int, previous["config_revision"]) + 1
        if previous
        else 1,
        **cast(
            JsonObject,
            icom_connection_data(
                host=verification.host,
                port=verification.port,
                gateway_password=verification.gateway_password,
                private_password=verification.private_password,
                gateway_id=verification.gateway_id,
                local_id=verification.local_id,
                verified_at=verification.verified_at,
            ),
        ),
    }
    locator: JsonObject = {
        "namespace": GatewayNamespace.K40_LOCAL.value,
        "value": verification.gateway_id,
    }
    raw_target = _section(data, "targets")[verification.target_id]
    raw_identities = cast(list[JsonObject], raw_target["identities"])
    local = [
        item
        for item in raw_identities
        if item["namespace"] == GatewayNamespace.K40_LOCAL
    ]
    if any(item["value"] != verification.gateway_id for item in local):
        raise IcomSetupError("local_identity_conflict")
    if not local:
        raw_identities.append(
            {
                **locator,
                "evidence": "verified_local_setup",
                "comparison_contract": ICOM_CONTRACT,
            }
        )
    bindings = _section(data, "bindings")
    bindings[binding_id] = {
        **bindings.get(binding_id, {}),
        "connection_id": connection_id,
        "target_id": verification.target_id,
        "adapter": LOCAL_KIND,
        "locator": locator,
        "link_evidence": {"basis": "verified_local_setup", "contract": ICOM_CONTRACT},
    }
    _section(options, "connections").setdefault(connection_id, {"enabled": True})
    _section(options, "bindings").setdefault(binding_id, {"enabled": True})
    order = cast(
        list[str], _section(options, "targets")[verification.target_id]["read_order"]
    )
    if binding_id not in order:
        order.append(binding_id)
    data["revision"] = current.revision + 1
    return load_configuration(
        current.owner_id,
        data,
        options,
        minor_version=max(LOCAL_STORAGE_MINOR_VERSION, current.minor_version),
    )


def commit_icom_setup(
    management: object, verification: IcomVerification
) -> ConfigurationSnapshot:
    """Hand one proposal to the entry's guarded update, on the current state."""
    if not isinstance(management, ConfigurationManagement):
        raise ConfigurationError("invalid_configuration_owner")
    before = management.current()
    proposed = prepare_icom_configuration(before, verification)
    try:
        return management.commit_local_access(before, proposed)
    except ConfigurationError:
        raise
    except Exception:
        raise ConfigurationError("local_save_failed") from None


def cloud_gateway_ids(
    owner: object, snapshot: ConfigurationSnapshot, target_id: str
) -> tuple[str, ...]:
    """The IDs the running cloud source of a plant reports for its gateway.

    Only what the cloud already read (its reading of the gateway ID), never a
    new request. Empty when the cloud source does not run or has no reading.
    """
    from .managed_entry import ManagedEntry

    if not isinstance(owner, ManagedEntry):
        return ()
    found: list[str] = []
    for key, binding in snapshot.bindings.items():
        if binding.target_id != target_id or binding.adapter != "pointt":
            continue
        data = getattr(owner.connections.source_data(key), "data", None)
        item = data.get(UUID_PATH) if isinstance(data, dict) else None
        value = getattr(getattr(item, "resource", None), "value", None)
        if isinstance(value, str) and value not in found:
            found.append(value)
    return tuple(found)
