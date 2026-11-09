"""Read stored cloud configuration and prepare narrow updates on current data.

Version 4 access requires an explicit connection; legacy readers support older
stored entries. These helpers neither migrate nor authorize a transport.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .configuration import (
    CLOUD_KIND,
    SUPPORTED_MINOR_VERSIONS,
    ConfigurationError,
    json_object,
    validate_cloud_credentials,
)
from .configuration_migration import legacy_cloud_connection_id
from .const import PollingProfile, polling_profile_from_options
from .data import tokens_from_data, tokens_to_data
from .pointt import AuthTokens

TOKEN_KEYS = frozenset(
    {"access_token", "refresh_token", "expires_at", "token_type", "scope"}
)
AUTH_EPOCH = "cloud_auth_epoch"


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError("invalid_object")
    return value


def _counter(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ConfigurationError("invalid_counter")
    return value


@dataclass(frozen=True, slots=True)
class CredentialContext:
    """Login identity, independent of polling state and ordinary token rotation."""

    owner_id: str = field(repr=False)
    connection_id: str = field(repr=False)
    version: int
    minor_version: int
    auth_epoch: int
    brand: object = field(repr=False)
    account: object = field(repr=False)

    def same_login(self, other: CredentialContext | None) -> bool:
        """Whether both describe the same sign-in, ignoring account metadata.

        A new sign-in raises the epoch, so the account fields add nothing for
        tokens. They can change on their own, for example when the first
        separate cloud device is chosen; that must not drop a rotated token.
        """
        return other is not None and (
            self.owner_id,
            self.connection_id,
            self.version,
            self.minor_version,
            self.auth_epoch,
            self.brand,
        ) == (
            other.owner_id,
            other.connection_id,
            other.version,
            other.minor_version,
            other.auth_epoch,
            other.brand,
        )


class EntryConfiguration:
    """Always read the entry's latest state, without retaining a stale full copy."""

    def __init__(
        self, entry: ConfigEntry[Any], connection_id: str | None = None
    ) -> None:
        self.entry = entry
        if entry.version in (1, 2, 3):
            legacy_id = legacy_cloud_connection_id(entry.entry_id)
            if connection_id is not None and connection_id != legacy_id:
                raise ConfigurationError("unknown_connection")
            self.connection_id = legacy_id
        elif entry.version == 4 and entry.minor_version in SUPPORTED_MINOR_VERSIONS:
            if connection_id is None:
                raise ConfigurationError("connection_required")
            self.connection_id = connection_id
        else:
            raise ConfigurationError("unsupported_version")
        self._version = entry.version
        self._minor_version = entry.minor_version

    @property
    def legacy(self) -> bool:
        """Reject a view whose storage version changed underneath it."""
        if (
            self.entry.version != self._version
            or self.entry.minor_version != self._minor_version
        ):
            raise ConfigurationError("configuration_changed")
        return self._version < 4

    def _connection(self) -> Mapping[str, Any]:
        connection = _mapping(
            _mapping(self.entry.data.get("connections")).get(self.connection_id)
        )
        if connection.get("kind") != CLOUD_KIND:
            raise ConfigurationError("unsupported_connection")
        return connection

    def cloud_data(self) -> dict[str, Any]:
        """Expose existing cloud fields without assuming all targets use PointT.

        Metadata readers can also inspect an entry awaiting reauthentication.
        Credential validation is deliberately separate from metadata access.
        """
        if self.legacy:
            return deepcopy(dict(self.entry.data))
        connection = self._connection()
        result = dict(_mapping(connection.get("credentials")))
        result.update(
            brand=connection.get("brand"),
            bacon_account=connection.get("legacy_bacon_account", ""),
        )
        gateways: list[str] = []
        devices: list[str] = []
        for value in _mapping(self.entry.data.get("bindings")).values():
            binding = _mapping(value)
            if binding.get("connection_id") != self.connection_id:
                continue
            adapter = binding.get("adapter")
            if adapter not in ("pointt", "bacon"):
                continue
            locator = _mapping(binding.get("locator"))
            identifier = locator.get("value")
            if not isinstance(identifier, str):
                raise ConfigurationError("invalid_locator")
            (gateways if adapter == "pointt" else devices).append(identifier)
        result.update(gateway_ids=gateways, bacon_device_ids=devices)
        return deepcopy(result)

    def tokens(self) -> AuthTokens:
        """Validate only this connection, even if another source needs repair."""
        data = (
            self.entry.data
            if self.legacy
            else _mapping(self._connection().get("credentials"))
        )
        credentials = {key: data.get(key) for key in TOKEN_KEYS}
        credentials["token_type"] = data.get("token_type", "Bearer")
        credentials["scope"] = data.get("scope", [])
        validate_cloud_credentials(json_object(credentials))
        return tokens_from_data(credentials)

    def context(self) -> CredentialContext:
        """Capture the login epoch without requiring usable credentials."""
        if self.legacy:
            data: Mapping[str, Any] = self.entry.data
            epoch = data.get(AUTH_EPOCH, 0)
            account = data.get("bacon_account", "")
        else:
            data = self._connection()
            epoch = data.get("auth_epoch")
            account = (
                deepcopy(data.get("account_evidence")),
                data.get("legacy_bacon_account", ""),
            )
        return CredentialContext(
            self.entry.entry_id,
            self.connection_id,
            self._version,
            self._minor_version,
            _counter(epoch),
            data.get("brand"),
            account,
        )

    def polling_profile(self) -> PollingProfile:
        """Resolve the existing profile for exactly one cloud connection."""
        options: Mapping[str, Any] = self.entry.options
        if not self.legacy:
            self._connection()
            options = _mapping(
                _mapping(options.get("connections")).get(self.connection_id)
            )
        return polling_profile_from_options(options)

    def polling_options(self, profile: PollingProfile) -> dict[str, Any]:
        """Prepare a profile patch, preserving other connections and preferences."""
        options = deepcopy(dict(self.entry.options))
        if self.legacy:
            options["polling_profile"] = profile.value
        else:
            self._connection()
            connections = dict(_mapping(options.get("connections")))
            selected = dict(_mapping(connections.get(self.connection_id)))
            connections[self.connection_id] = {
                **selected,
                "polling_profile": profile.value,
            }
            options["connections"] = connections
        return options

    def setup_parameters(self) -> dict[str, Any]:
        """Snapshot legacy reload inputs independently of rotating credentials."""
        return {
            key: value
            for key, value in self.cloud_data().items()
            if key not in TOKEN_KEYS
        }

    def dialog_context(
        self,
    ) -> tuple[CredentialContext, dict[str, Any], PollingProfile]:
        """Ignore token rotation and unrelated display options in open dialogs."""
        data = self.cloud_data()
        selection = {key: data.get(key) for key in ("gateway_ids", "bacon_device_ids")}
        return self.context(), selection, self.polling_profile()

    def legacy_update(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        """Apply a dialog's intent to current v3 data and mark a replacement login."""
        if not self.legacy:
            raise ConfigurationError("unsupported_legacy_version")
        data = {**deepcopy(dict(self.entry.data)), **deepcopy(dict(updates))}
        if TOKEN_KEYS.intersection(updates):
            data[AUTH_EPOCH] = self.context().auth_epoch + 1
        return data

    def rotated_data(
        self, expected: CredentialContext, previous: AuthTokens, updated: AuthTokens
    ) -> dict[str, Any] | None:
        """Compare and prepare a narrow token patch; caller saves without awaiting.

        Disabling a connection or changing its account metadata does not
        invalidate a pending rotation. Removal, a replacement login, or a newer
        saved token set does invalidate it.
        """
        try:
            if not self.context().same_login(expected) or self.tokens() != previous:
                return None
            credentials = tokens_to_data(updated)
            validate_cloud_credentials(json_object(credentials))
            if self.legacy:
                return {**deepcopy(dict(self.entry.data)), **credentials}
            connections = dict(_mapping(self.entry.data.get("connections")))
            connection = dict(self._connection())
            connection["credentials"] = {
                **_mapping(connection.get("credentials")),
                **credentials,
            }
            connections[self.connection_id] = connection
            return {
                **deepcopy(dict(self.entry.data)),
                "connections": deepcopy(connections),
                "revision": _counter(self.entry.data.get("revision")) + 1,
            }
        except ValueError, OverflowError:
            return None
