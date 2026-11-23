"""Verify and save the local access of an iCom gateway.

The gateway must already be known by the cloud (a plant of this entry): the
local access is proved to be the same gateway by its own ID, which must equal
the cloud's gateway ID. A gateway that reports no usable ID is recognised by
values it shares with the cloud instead (icom_identity). A different usable ID
or a clear contradiction, or too little agreement, saves nothing. The check
reads a few resources and sends the passwords nowhere else.

The private password is used exactly as typed. Only when the first answer
cannot be decrypted with it is the same answer tried offline with the typed
password without spaces at its start and end, then with an empty one (a
gateway whose private password was reset): the first is saved instead, the
second is named and saves nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    IDENTITY_UNVERIFIED,
    IDENTITY_UUID,
    IDENTITY_VALUES,
    MAX_ID_LENGTH,
    comparable_id,
    describe_reported_id,
    gateway_ids_match,
    icom_connection_data,
    is_icom_connection,
)
from .icom_client import (
    EXACT,
    PATH_FAILURES,
    PATH_PROBLEMS,
    RESET,
    IcomClient,
    IcomError,
    IcomProblem,
    IcomReply,
    other_private_password,
)
from .icom_crypto import IcomCryptoError, derive_key
from .icom_identity import (
    FALLBACK_PATHS,
    CloudReading,
    Verdict,
    compare,
    fingerprint,
)
from .icom_plan import UUID_PATH
from .local_credentials import LOCAL_STORAGE_MINOR_VERSION
from .local_probe import LocalProbeError, normalize_local_address

_LOGGER = logging.getLogger(__name__)

# What a failed check reports; each code has its own text in the dialog.
CANNOT_CONNECT = "icom_cannot_connect"
INVALID_AUTH = "icom_invalid_auth"
BUSY = "icom_busy"
NO_IDENTITY = "icom_no_identity"
OTHER_GATEWAY = "icom_other_gateway"
INVALID_HOST = "icom_invalid_host"
NO_PRIVATE_PASSWORD = "icom_no_private_password"
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
    # Set when the gateway was recognised by values: (path, text) to check later.
    fingerprint: tuple[str, str] | None = field(repr=False, default=None)
    # Which form of the typed private password fits: "exact" or "trimmed".
    private_password_variant: str = field(repr=False, default=EXACT)
    # How the gateway was recognised; empty: from the fingerprint (old proofs).
    identity_method: str = field(repr=False, default="")
    # Notes of the identity check for the diagnostics: kinds and counts only.
    check: Mapping[str, object] = field(repr=False, default_factory=dict)

    @property
    def method(self) -> str:
        if self.identity_method:
            return self.identity_method
        return IDENTITY_UUID if self.fingerprint is None else IDENTITY_VALUES


def _code(error: IcomError) -> str:
    return _PROBLEM_CODES.get(error.problem, CANNOT_CONNECT)


class _CheckedReads:
    """The reads of one check, with the offline look at the private password.

    Until one answer is decrypted, an undecryptable one is tried offline with
    the other forms of the private password (no further request). After that
    the key is proved, and an undecryptable answer is a failure like any other.
    """

    def __init__(
        self, client: IcomClient, gateway_password: str, private_password: str
    ) -> None:
        self.client = client
        self.gateway_password = gateway_password
        self.private_password = private_password
        self.variant = EXACT
        self._proved = False

    async def get(self, path: str) -> IcomReply:
        try:
            reply = await self.client.get(path)
        except IcomError as error:
            if (
                self._proved
                or error.problem is not IcomProblem.UNDECRYPTABLE
                or error.answer is None
            ):
                raise
            reply = self._other_form(error.answer)
        self._proved = True
        return reply

    def _other_form(self, answer: bytes) -> IcomReply:
        found = other_private_password(
            self.gateway_password, self.private_password, answer
        )
        if found is None:
            raise IcomSetupError(INVALID_AUTH)
        if found.variant == RESET:
            # Never set up with an empty private password: it must be set first.
            raise IcomSetupError(NO_PRIVATE_PASSWORD)
        self.client.use_key(found.key)
        self.private_password, self.variant = found.private_password, found.variant
        return found.reply


async def verify_icom_access(
    session: aiohttp.ClientSession,
    *,
    target_id: str,
    gateway_id: str,
    host: str,
    gateway_password: str,
    private_password: str,
    also_accepted: Collection[str] = (),
    cloud_readings: Mapping[str, CloudReading] | None = None,
    port: int = ICOM_DEFAULT_PORT,
) -> IcomVerification:
    """Recognise the gateway as the cloud's: by its own ID, else by values.

    The IDs in also_accepted are further IDs the cloud itself reports for the same
    gateway (its reading of the same resource); the first one that matches
    is kept as the ID to check later. cloud_readings are the cloud's last
    readings, compared when the gateway reports no usable ID of its own or one
    that is not the cloud's: how a gateway's own ID relates to the cloud's is
    not known for every gateway. Only a clear contradiction of the values
    stops the setup; too little agreement sets the access up unverified.
    """
    try:
        key = derive_key(gateway_password, private_password)
    except IcomCryptoError as error:
        raise IcomSetupError(str(error)) from None
    address = normalize_icom_host(host)
    reads = _CheckedReads(
        IcomClient(session, address, key, port=port),
        gateway_password,
        private_password,
    )
    reported: object = None
    offered = True
    try:
        reply = await reads.get(UUID_PATH)
    except IcomError as error:
        if error.problem not in PATH_PROBLEMS:
            raise IcomSetupError(_code(error)) from None
        offered = False
    else:
        reported = reply.document.get("value")
    check: dict[str, object] = {
        "uuid": describe_reported_id(reported, read=offered),
        "uuid_matches_cloud": None,
    }
    own_id = (
        reported
        if isinstance(reported, str) and comparable_id(reported) is not None
        else None
    )
    if own_id is not None:
        matching = next(
            (
                expected
                for expected in (gateway_id, *also_accepted)
                if gateway_ids_match(own_id, expected)
            ),
            None,
        )
        check["uuid_matches_cloud"] = matching is not None
        # Kept to check later: an ID with spaces, a line break or other
        # unreadable characters is kept as the matching ID of the cloud, or
        # as its comparable form when the cloud reports it the same way (all
        # compare the same).
        if matching is not None and not _storable_id(own_id):
            own_id = matching if _storable_id(matching) else comparable_id(own_id)
    if own_id is None or check["uuid_matches_cloud"] is not True:
        # No ID, an unusable one (empty, a number, "-1") or another one than
        # the cloud's: compare values.
        return await _verify_by_values(
            reads,
            cloud_readings or {},
            target_id,
            gateway_id,
            address,
            port,
            gateway_password,
            check,
        )
    check["method"] = IDENTITY_UUID
    _log_check(check)
    return IcomVerification(
        target_id,
        gateway_id,
        own_id,
        address,
        port,
        gateway_password,
        reads.private_password,
        time(),
        private_password_variant=reads.variant,
        identity_method=IDENTITY_UUID,
        check=check,
    )


def _storable_id(value: str) -> bool:
    """Whether a reported ID can be kept as it is (readable, no spaces)."""
    return (
        0 < len(value) <= MAX_ID_LENGTH
        and value.isprintable()
        and not any(character.isspace() for character in value)
    )


def _log_check(check: Mapping[str, object]) -> None:
    """The identity check on the debug log: kinds, counts and a verdict only."""
    _LOGGER.debug(
        "iCom setup identity check: own ID %s, the cloud's ID: %s, values: %s "
        "(%s of %s matching), method %s",
        check.get("uuid"),
        check.get("uuid_matches_cloud"),
        check.get("values_verdict", "not compared"),
        check.get("values_matching", 0),
        check.get("values_read", 0),
        check.get("method"),
    )


async def _verify_by_values(
    reads: _CheckedReads,
    cloud: Mapping[str, CloudReading],
    target_id: str,
    gateway_id: str,
    address: str,
    port: int,
    gateway_password: str,
    check: dict[str, object],
) -> IcomVerification:
    """Compare what the gateway answers with the cloud's last readings.

    A clear contradiction is another gateway. Enough agreement recognises the
    gateway by its values; too little sets the access up unverified (with the
    fingerprint when one was read), so that a gateway without a usable ID of
    its own can still be read.
    """
    local: dict[str, object] = {}
    # Only paths the cloud has a reading of are worth a request.
    for path in FALLBACK_PATHS:
        if path not in cloud:
            continue
        try:
            reply = await reads.get(path)
        except IcomError as error:
            if error.problem in PATH_PROBLEMS or error.problem in PATH_FAILURES:
                # Only this value is missing from the comparison.
                continue
            raise IcomSetupError(_code(error)) from None
        local[path] = reply.document.get("value")
    outcome = compare(local, cloud)
    check["values_verdict"] = outcome.verdict.value
    check["values_matching"] = outcome.matches
    check["values_read"] = len(local)
    if outcome.verdict is Verdict.CONTRADICTION:
        _log_check(check)
        raise IcomSetupError(OTHER_GATEWAY)
    remembered = fingerprint(local)
    method = (
        IDENTITY_VALUES
        if outcome.verdict is Verdict.CONFIRMED and remembered is not None
        else IDENTITY_UNVERIFIED
    )
    check["method"] = method
    _log_check(check)
    return IcomVerification(
        target_id,
        gateway_id,
        gateway_id,
        address,
        port,
        gateway_password,
        reads.private_password,
        time(),
        remembered,
        reads.variant,
        method,
        check,
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
                fingerprint=verification.fingerprint,
                private_password_variant=verification.private_password_variant,
                identity_method=verification.method,
                setup_check=verification.check,
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


def cloud_readings(
    owner: object,
    snapshot: ConfigurationSnapshot,
    target_id: str,
    paths: Collection[str] = FALLBACK_PATHS,
    *,
    now: datetime | None = None,
) -> dict[str, CloudReading]:
    """The cloud's last readings of some paths, with their age in seconds.

    Only what the cloud already read, never a new request. Empty when the
    cloud source does not run.
    """
    from .managed_entry import ManagedEntry

    if not isinstance(owner, ManagedEntry):
        return {}
    return readings_of(owner.connections, snapshot, target_id, paths, now=now)


def readings_of(
    connections: object,
    snapshot: ConfigurationSnapshot,
    target_id: str,
    paths: Collection[str] = FALLBACK_PATHS,
    *,
    now: datetime | None = None,
) -> dict[str, CloudReading]:
    """The cloud's last readings of some paths from the running connections.

    Used by the setup and by a running access that checks its gateway again.
    """
    source_data = getattr(connections, "source_data", None)
    if not callable(source_data):
        return {}
    moment = now or datetime.now(UTC)
    found: dict[str, CloudReading] = {}
    for key, binding in snapshot.bindings.items():
        if binding.target_id != target_id or binding.adapter != "pointt":
            continue
        data = getattr(source_data(key), "data", None)
        if not isinstance(data, dict):
            continue
        for path in paths:
            item = data.get(path)
            when = getattr(item, "last_success", None)
            if (
                item is None
                or not getattr(item, "available", False)
                or not isinstance(when, datetime)
                or not hasattr(getattr(item, "resource", None), "value")
            ):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            found.setdefault(
                path,
                CloudReading(item.resource.value, (moment - when).total_seconds()),
            )
    return found
