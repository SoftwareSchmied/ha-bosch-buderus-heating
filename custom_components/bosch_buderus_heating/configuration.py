"""Pure, versioned storage contracts; no setup, transport or write authorization."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast

from .local_credentials import LOCAL_STORAGE_MINOR_VERSION, load_local_access

type JsonValue = str | int | float | bool | list[JsonValue] | JsonObject | None
type JsonObject = dict[str, JsonValue]

STORAGE_VERSION = 4
STORAGE_MINOR_VERSION = 1
SUPPORTED_MINOR_VERSIONS = frozenset(
    {STORAGE_MINOR_VERSION, LOCAL_STORAGE_MINOR_VERSION}
)
# Defensive storage limits, not appliance or vendor topology limits.
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 65_536
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_IDENTIFIER_LENGTH = 1024
CLOUD_KIND = "singlekey_cloud"
LOCAL_KIND = "k40_local"
POLLING_PROFILES = frozenset({"standard", "cloud_friendly"})


class ConfigurationError(ValueError):
    """Static error codes never include credentials or object identifiers."""


def readable_minor_version(minor_version: object) -> bool:
    """Whether an entry of this minor version can be read.

    A minor version only adds optional data, so a newer one is read like the
    newest known one: its unknown fields are kept and ignored. An incompatible
    change needs a new major version. This keeps a return within the same
    major version possible without a backup.
    """
    return type(minor_version) is int and minor_version >= STORAGE_MINOR_VERSION


def json_object(value: object) -> JsonObject:
    """Copy JSON-compatible input, rejecting cycles, nonfinite numbers and size."""
    remaining = MAX_JSON_NODES

    def copy(item: object, depth: int) -> JsonValue:
        nonlocal remaining
        remaining -= 1
        if depth > MAX_JSON_DEPTH or remaining < 0:
            raise ConfigurationError("storage_limit")
        if item is None or type(item) in (str, bool, int):
            return cast(JsonValue, item)
        if type(item) is float:
            if not math.isfinite(item):
                raise ConfigurationError("invalid_json")
            return item
        if isinstance(item, Mapping):
            result: JsonObject = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ConfigurationError("invalid_json")
                result[key] = copy(child, depth + 1)
            return result
        if isinstance(item, list):
            return [copy(child, depth + 1) for child in item]
        raise ConfigurationError("invalid_json")

    result = copy(value, 0)
    if not isinstance(result, dict):
        raise ConfigurationError("invalid_object")
    try:
        encoded = json.dumps(result, ensure_ascii=True, allow_nan=False)
    except ValueError, OverflowError:
        raise ConfigurationError("invalid_json") from None
    if len(encoded) > MAX_JSON_BYTES:
        raise ConfigurationError("storage_limit")
    return result


def require_identifier(value: object) -> str:
    """Preserve exact opaque IDs, including leading zeros and Unicode."""
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_IDENTIFIER_LENGTH
        or not value.isprintable()
        or any(character.isspace() for character in value)
    ):
        raise ConfigurationError("invalid_identifier")
    return value


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise ConfigurationError("invalid_object")
    return value


def _counter(value: JsonValue) -> int:
    if type(value) is not int or value < 0:
        raise ConfigurationError("invalid_counter")
    return value


def _strings(value: JsonValue) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigurationError("invalid_string_list")
    return cast(list[str], value)


def _expiry(value: JsonValue) -> None:
    if type(value) not in (int, float):
        raise ConfigurationError("invalid_credentials")
    try:
        finite = math.isfinite(cast(int | float, value))
    except OverflowError:
        finite = False
    if not finite:
        raise ConfigurationError("invalid_credentials")


def validate_cloud_credentials(data: JsonObject) -> None:
    """Validate storage shape without treating an expired token as invalid data."""
    token = data.get("access_token")
    refresh = data.get("refresh_token")
    expiry = data.get("expires_at")
    if not isinstance(token, str) or not token:
        raise ConfigurationError("invalid_credentials")
    if refresh is not None and not isinstance(refresh, str):
        raise ConfigurationError("invalid_credentials")
    _expiry(expiry)
    if not isinstance(data.get("token_type"), str):
        raise ConfigurationError("invalid_credentials")
    _strings(data.get("scope"))


def _hash(value: JsonValue) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ConfigurationError("invalid_account_key")
    return value


def _identity(value: JsonValue) -> tuple[str, str, str]:
    identity = _object(value)
    namespace = require_identifier(identity.get("namespace"))
    identifier = require_identifier(identity.get("value"))
    account = identity.get("account_key")
    if namespace == "bacon_device":
        account = _hash(account)
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identifier) is None:
            raise ConfigurationError("invalid_identifier")
    elif (
        namespace in ("pointt_gateway", "local_system_gateway") and account is not None
    ):
        raise ConfigurationError("invalid_identity_scope")
    elif account is not None:
        account = require_identifier(account)
    return namespace, account or "", identifier


def _encoded(value: JsonObject) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class StoredConnection:
    """A typed view; explicit serialization contains private configuration."""

    kind: str = field(repr=False)
    auth_epoch: int
    config_revision: int
    _json: str = field(repr=False)

    def to_data(self) -> JsonObject:
        """Return a detached copy, never a public diagnostic representation."""
        return cast(JsonObject, json.loads(self._json))


@dataclass(frozen=True, slots=True)
class StoredTarget:
    """A durable target is not automatically a Home Assistant device."""

    kind: str = field(repr=False)
    _json: str = field(repr=False)

    def to_data(self) -> JsonObject:
        return cast(JsonObject, json.loads(self._json))


@dataclass(frozen=True, slots=True)
class StoredBinding:
    """A connection-to-target link, not a semantic resource or write grant."""

    connection_id: str = field(repr=False)
    target_id: str = field(repr=False)
    adapter: str = field(repr=False)
    _json: str = field(repr=False)

    def to_data(self) -> JsonObject:
        return cast(JsonObject, json.loads(self._json))


@dataclass(frozen=True, slots=True)
class ConfigurationSnapshot:
    """Detached immutable output of load_configuration or legacy conversion."""

    owner_id: str = field(repr=False)
    version: int
    minor_version: int
    revision: int
    connections: Mapping[str, StoredConnection] = field(repr=False)
    targets: Mapping[str, StoredTarget] = field(repr=False)
    bindings: Mapping[str, StoredBinding] = field(repr=False)
    _data_json: str = field(repr=False)
    _options_json: str = field(repr=False)

    def to_data(self) -> JsonObject:
        return cast(JsonObject, json.loads(self._data_json))

    def to_options(self) -> JsonObject:
        return cast(JsonObject, json.loads(self._options_json))


def _connection(raw: JsonObject, minor_version: int) -> StoredConnection:
    kind = require_identifier(raw.get("kind"))
    epoch = _counter(raw.get("auth_epoch"))
    revision = _counter(raw.get("config_revision"))
    if kind == CLOUD_KIND:
        if raw.get("brand") not in ("bosch", "buderus"):
            raise ConfigurationError("invalid_brand")
        validate_cloud_credentials(_object(raw.get("credentials")))
        if raw.get("account_evidence") is not None:
            _object(raw["account_evidence"])
    elif kind == LOCAL_KIND and "local_access" in raw:
        if minor_version < LOCAL_STORAGE_MINOR_VERSION:
            raise ConfigurationError("unsupported_local_storage_version")
        try:
            load_local_access(raw)
        except ValueError as error:
            raise ConfigurationError(str(error)) from None
    elif kind == LOCAL_KIND:
        credentials = _object(raw.get("credentials"))
        if not isinstance(credentials.get("token"), str) or not credentials["token"]:
            raise ConfigurationError("invalid_credentials")
        if "open_api.read" not in _strings(credentials.get("scope")):
            raise ConfigurationError("invalid_local_scope")
        expiry = credentials.get("expires_at")
        if expiry is not None:
            _expiry(expiry)
        endpoint = _object(raw.get("endpoint"))
        host = require_identifier(endpoint.get("host"))
        if any(character in host for character in "/\\?#@"):
            raise ConfigurationError("invalid_endpoint")
        port = endpoint.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ConfigurationError("invalid_endpoint")
        if endpoint.get("transport") != "https_local_api":
            raise ConfigurationError("invalid_endpoint")
    return StoredConnection(kind, epoch, revision, _encoded(raw))


def _target(raw: JsonObject) -> tuple[StoredTarget, set[tuple[str, str, str]]]:
    kind = require_identifier(raw.get("kind"))
    identities = raw.get("identities")
    if not isinstance(identities, list) or not identities:
        raise ConfigurationError("missing_identity")
    signatures = {_identity(identity) for identity in identities}
    if len(signatures) != len(identities):
        raise ConfigurationError("duplicate_identity")
    if len({(ns, account) for ns, account, _ in signatures}) != len(signatures):
        raise ConfigurationError("ambiguous_identity")
    for identity in identities:
        require_identifier(_object(identity).get("evidence"))
    required = {
        "pointt_object": "pointt_gateway",
        "standalone_device": "bacon_device",
        "local_installation": "local_system_gateway",
    }.get(kind)
    if required is not None and not any(ns == required for ns, _, _ in signatures):
        raise ConfigurationError("missing_identity")
    legacy = raw.get("legacy_identity")
    if legacy is not None:
        record = _object(legacy)
        form = require_identifier(record.get("format"))
        if form == "pointt_v3":
            old_id = require_identifier(record.get("gateway_id"))
            if ("pointt_gateway", "", old_id) not in signatures:
                raise ConfigurationError("legacy_identity_conflict")
        elif form == "bacon_v3":
            account = _hash(record.get("account_key"))
            serial = require_identifier(record.get("serial"))
            if ("bacon_device", account, serial) not in signatures:
                raise ConfigurationError("legacy_identity_conflict")
            prefix = record.get("entity_prefix")
            if prefix is not None and (
                not isinstance(prefix, str)
                or re.fullmatch(r"bacon:[0-9a-f]{64}", prefix) is None
            ):
                raise ConfigurationError("invalid_legacy_prefix")
    return StoredTarget(kind, _encoded(raw)), signatures


def _binding(
    raw: JsonObject,
    connections: Mapping[str, StoredConnection],
    targets: Mapping[str, StoredTarget],
    identities: Mapping[str, set[tuple[str, str, str]]],
) -> StoredBinding:
    connection_id = require_identifier(raw.get("connection_id"))
    target_id = require_identifier(raw.get("target_id"))
    adapter = require_identifier(raw.get("adapter"))
    if connection_id not in connections or target_id not in targets:
        raise ConfigurationError("dangling_binding")
    locator = _identity(raw.get("locator"))
    if locator not in identities[target_id]:
        raise ConfigurationError("binding_identity_conflict")
    expected = {
        "pointt": (CLOUD_KIND, "pointt_gateway"),
        "bacon": (CLOUD_KIND, "bacon_device"),
        "k40_local": (LOCAL_KIND, "local_system_gateway"),
    }.get(adapter)
    if expected and (connections[connection_id].kind, locator[0]) != expected:
        raise ConfigurationError("adapter_conflict")
    connection_data = connections[connection_id].to_data()
    if (
        adapter == "k40_local"
        and "local_access" in connection_data
        and load_local_access(connection_data).gateway_id != locator[2]
    ):
        raise ConfigurationError("local_identity_conflict")
    if (
        adapter in ("pointt", "k40_local")
        and targets[target_id].kind == "standalone_device"
    ):
        raise ConfigurationError("adapter_conflict")
    if adapter == "bacon" and targets[target_id].kind in (
        "pointt_object",
        "local_installation",
    ):
        raise ConfigurationError("adapter_conflict")
    evidence = _object(raw.get("link_evidence"))
    require_identifier(evidence.get("basis"))
    if evidence.get("contract") is not None:
        require_identifier(evidence["contract"])
    return StoredBinding(connection_id, target_id, adapter, _encoded(raw))


def _options(
    raw: JsonObject,
    connections: Mapping[str, StoredConnection],
    targets: Mapping[str, StoredTarget],
    bindings: Mapping[str, StoredBinding],
) -> None:
    for key, objects in (
        ("connections", connections),
        ("targets", targets),
        ("bindings", bindings),
    ):
        settings = _object(raw.get(key))
        if settings.keys() != objects.keys():
            raise ConfigurationError("option_reference_conflict")
        for identifier, value in settings.items():
            option = _object(value)
            if key != "targets" and type(option.get("enabled")) is not bool:
                raise ConfigurationError("invalid_enabled")
            if key == "connections" and connections[identifier].kind == CLOUD_KIND:
                profile = option.get("polling_profile")
                if not isinstance(profile, str) or profile not in POLLING_PROFILES:
                    raise ConfigurationError("invalid_polling_profile")
                # I1: the one-time switch of an iCom connection; ignored by
                # older versions.
                revision = option.get("icom_profile_revision", 0)
                if type(revision) is not int or revision < 0:
                    raise ConfigurationError("invalid_polling_profile")
            if key == "connections" and connections[identifier].kind == LOCAL_KIND:
                interval = option.get("local_interval", 60)
                if (
                    type(interval) is not int
                    or not 60 <= interval <= 900
                    or interval % 60
                ):
                    raise ConfigurationError("invalid_local_interval")
            if key == "targets":
                order = _strings(option.get("read_order"))
                assigned = {
                    bid
                    for bid, binding in bindings.items()
                    if binding.target_id == identifier
                }
                if len(set(order)) != len(order) or set(order) != assigned:
                    raise ConfigurationError("invalid_read_order")
                if type(option.get("local_values", False)) is not bool:
                    raise ConfigurationError("invalid_local_values")
                # R1 (P49b): shown from its local readings after the cloud
                # access was removed; ignored by older versions.
                if type(option.get("local_plant", False)) is not bool:
                    raise ConfigurationError("invalid_local_plant")


def load_configuration(
    owner_id: str,
    data: object,
    options: object,
    *,
    version: int = STORAGE_VERSION,
    minor_version: int = STORAGE_MINOR_VERSION,
) -> ConfigurationSnapshot:
    """Validate one owner's snapshot; never interpret it as a source activation."""
    require_identifier(owner_id)
    if type(version) is not int or version != STORAGE_VERSION:
        raise ConfigurationError("unsupported_version")
    if not readable_minor_version(minor_version):
        raise ConfigurationError("unsupported_minor_version")
    stored, settings = json_object(data), json_object(options)
    revision = _counter(stored.get("revision"))
    connections = {
        require_identifier(key): _connection(_object(value), minor_version)
        for key, value in _object(stored.get("connections")).items()
    }
    targets: dict[str, StoredTarget] = {}
    identities: dict[str, set[tuple[str, str, str]]] = {}
    seen_identities: set[tuple[str, str, str]] = set()
    for key, value in _object(stored.get("targets")).items():
        require_identifier(key)
        target, signatures = _target(_object(value))
        if signatures & seen_identities:
            raise ConfigurationError("competing_targets")
        seen_identities.update(signatures)
        targets[key], identities[key] = target, signatures
    bindings = {
        require_identifier(key): _binding(
            _object(value), connections, targets, identities
        )
        for key, value in _object(stored.get("bindings")).items()
    }
    seen_bindings: set[tuple[str, str, str]] = set()
    local_connections: set[str] = set()
    for binding in bindings.values():
        signature = binding.connection_id, binding.target_id, binding.adapter
        if signature in seen_bindings:
            raise ConfigurationError("duplicate_binding")
        seen_bindings.add(signature)
        if connections[binding.connection_id].kind == LOCAL_KIND:
            if binding.connection_id in local_connections:
                raise ConfigurationError("ambiguous_local_target")
            local_connections.add(binding.connection_id)
    _object(stored.get("legacy_extra"))
    _options(settings, connections, targets, bindings)
    return ConfigurationSnapshot(
        owner_id,
        version,
        minor_version,
        revision,
        MappingProxyType(connections),
        MappingProxyType(targets),
        MappingProxyType(bindings),
        _encoded(stored),
        _encoded(settings),
    )
