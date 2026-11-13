"""Prepare storage version 4 without changing a Home Assistant entry or registry."""

from __future__ import annotations

import json
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from .bacon.models import (
    BaconError,
    account_key,
    device_key,
    device_list,
    token_subject,
)
from .configuration import (
    CLOUD_KIND,
    POLLING_PROFILES,
    ConfigurationError,
    ConfigurationSnapshot,
    JsonObject,
    JsonValue,
    json_object,
    load_configuration,
    require_identifier,
    validate_cloud_credentials,
)

_NAMESPACE = uuid5(NAMESPACE_URL, "bosch_buderus_heating/configuration/v4")
_TOKEN_DEFAULTS: JsonObject = {
    "access_token": None,
    "refresh_token": None,
    "expires_at": None,
    "token_type": "Bearer",
    "scope": [],
}
_LEGACY_KEYS = {
    *_TOKEN_DEFAULTS,
    "brand",
    "gateway_ids",
    "bacon_device_ids",
    "bacon_account",
    "cloud_auth_epoch",
}
_OPTION_KEYS = {"connections", "targets", "bindings", "legacy_options"}


def _key(owner: str, kind: str, *parts: str) -> str:
    """Length-safe input encoding prevents delimiter-dependent identity collisions."""
    seed = json.dumps([owner, kind, *parts], ensure_ascii=True, separators=(",", ":"))
    return f"{kind}_{uuid5(_NAMESPACE, seed).hex}"


def _gateways(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        raise ConfigurationError("invalid_gateway_selection")
    identifiers = [require_identifier(item) for item in value]
    if len(set(identifiers)) != len(identifiers):
        raise ConfigurationError("duplicate_gateway_selection")
    return identifiers


def legacy_cloud_connection_id(owner_id: str) -> str:
    """Use the same connection identity before and after storage conversion."""
    require_identifier(owner_id)
    return _key(owner_id, "connection", "legacy_cloud")


def convert_legacy_configuration(
    owner_id: str,
    data: object,
    options: object,
    *,
    version: int = 3,
    minor_version: int = 1,
) -> ConfigurationSnapshot:
    """Return a detached proposal; retain the existing HA metadata outside it.

    Preview versions need the existing migration to version 3 first. This
    function does not call that migration, load a registry or start a client.
    Decoded token routing only reconstructs old names, never authenticates.
    """
    require_identifier(owner_id)
    if type(version) is not int or version != 3:
        raise ConfigurationError("unsupported_legacy_version")
    if type(minor_version) is not int or minor_version != 1:
        raise ConfigurationError("unsupported_minor_version")
    old, previous_options = json_object(data), json_object(options)
    if old.get("brand") not in ("bosch", "buderus"):
        raise ConfigurationError("invalid_brand")
    credentials = {
        key: old.get(key, default) for key, default in _TOKEN_DEFAULTS.items()
    }
    validate_cloud_credentials(credentials)
    gateways = _gateways(old.get("gateway_ids"))
    try:
        devices = device_list(old.get("bacon_device_ids", []))
    except BaconError:
        raise ConfigurationError("invalid_device_selection") from None
    if not gateways and not devices:
        raise ConfigurationError("empty_legacy_selection")

    account = old.get("bacon_account", "")
    if not isinstance(account, str):
        raise ConfigurationError("invalid_account_key")
    # A missing or mismatched routing claim leaves the old namespace unresolved.
    # Even a matching claim is not proof of authorization or a verified account.
    subject: str | None = None
    if devices:
        try:
            candidate = token_subject(cast(str, credentials["access_token"]))
            if account_key(candidate) == account:
                subject = candidate
        except BaconError, RecursionError:
            pass

    connection_id = legacy_cloud_connection_id(owner_id)
    connection: JsonObject = {
        "kind": CLOUD_KIND,
        "brand": old["brand"],
        "auth_epoch": old.get("cloud_auth_epoch", 0),
        "config_revision": 0,
        "credentials": credentials,
        "account_evidence": None,
        "legacy_bacon_account": account,
    }
    targets: JsonObject = {}
    bindings: JsonObject = {}
    binding_options: JsonObject = {}
    target_options: JsonObject = {}
    for adapter, namespace, identifier in [
        *[("pointt", "pointt_gateway", gateway) for gateway in gateways],
        *[("bacon", "bacon_device", serial) for serial in devices],
    ]:
        scope = account if adapter == "bacon" else ""
        target_id = _key(owner_id, "target", namespace, scope, identifier)
        binding_id = _key(owner_id, "binding", connection_id, target_id, adapter)
        locator: JsonObject = {"namespace": namespace, "value": identifier}
        legacy: JsonObject
        if adapter == "pointt":
            legacy = {"format": "pointt_v3", "gateway_id": identifier}
        else:
            locator["account_key"] = account
            legacy = {
                "format": "bacon_v3",
                "account_key": account,
                "serial": identifier,
                "entity_prefix": device_key(subject, identifier) if subject else None,
            }
        targets[target_id] = {
            "kind": "pointt_object" if adapter == "pointt" else "standalone_device",
            "identities": [{**locator, "evidence": "legacy_selection"}],
            "legacy_identity": legacy,
        }
        bindings[binding_id] = {
            "connection_id": connection_id,
            "target_id": target_id,
            "adapter": adapter,
            "locator": locator,
            "link_evidence": {"basis": "legacy_selection", "contract": None},
        }
        binding_options[binding_id] = {"enabled": True}
        target_options[target_id] = {"read_order": [binding_id]}

    profile = previous_options.get("polling_profile", "standard")
    preserved_options = {
        key: value for key, value in previous_options.items() if key in _OPTION_KEYS
    }
    if not isinstance(profile, str) or profile not in POLLING_PROFILES:
        # Preserve malformed older settings, while retaining their effective default.
        preserved_options["polling_profile"] = profile
        profile = "standard"
    new_options: JsonObject = {
        key: value
        for key, value in previous_options.items()
        if key not in _OPTION_KEYS and key != "polling_profile"
    }
    new_options.update(
        {
            "connections": {
                connection_id: {"enabled": True, "polling_profile": profile}
            },
            "targets": target_options,
            "bindings": binding_options,
        }
    )
    if preserved_options:
        new_options["legacy_options"] = preserved_options
    return load_configuration(
        owner_id,
        {
            "revision": 0,
            "connections": {connection_id: connection},
            "targets": targets,
            "bindings": bindings,
            "legacy_extra": {
                key: value for key, value in old.items() if key not in _LEGACY_KEYS
            },
        },
        new_options,
    )
