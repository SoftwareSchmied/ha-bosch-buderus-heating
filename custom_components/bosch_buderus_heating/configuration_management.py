"""Atomic connection changes on current storage, without activating migration."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth_lifecycle import (
    connection_backoff,
    connection_resources,
    entry_refresh_in_progress,
    forget_connection_auth,
)
from .bacon.flow import discover
from .bacon.models import BaconError
from .configuration import (
    CLOUD_KIND,
    LOCAL_KIND,
    ConfigurationError,
    ConfigurationSnapshot,
    JsonObject,
    load_configuration,
    validate_cloud_credentials,
)
from .configuration_access import EntryConfiguration
from .const import PollingProfile
from .data import tokens_to_data
from .pointt import AuthTokens, PointTClient


def section(data: JsonObject, name: str) -> dict[str, JsonObject]:
    """Use a collection from an already validated snapshot."""
    return cast(dict[str, JsonObject], data[name])


@dataclass(frozen=True, slots=True)
class ConnectionEdit:
    """An intent keeps its selected connection and ignores ordinary rotation."""

    owner_id: str = field(repr=False)
    connection_id: str = field(repr=False)
    signature: str = field(repr=False)


class ConfigurationManagement:
    """Prepare validated changes and commit without an intervening await."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        self.hass = hass
        self.entry = entry
        self._listeners: list[Callable[[ConfigurationSnapshot], None]] = []

    def current(self) -> ConfigurationSnapshot:
        if (
            self.hass.config_entries.async_get_entry(self.entry.entry_id)
            is not self.entry
        ):
            raise ConfigurationError("entry_removed")
        return load_configuration(
            self.entry.entry_id,
            self.entry.data,
            self.entry.options,
            version=self.entry.version,
            minor_version=self.entry.minor_version,
        )

    @callback
    def subscribe(
        self, listener: Callable[[ConfigurationSnapshot], None]
    ) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def edit(self, connection_id: str) -> ConnectionEdit:
        snapshot = self.current()
        connection = snapshot.connections.get(connection_id)
        if connection is None:
            raise ConfigurationError("connection_removed")
        data, options = snapshot.to_data(), snapshot.to_options()
        parameters = {
            key: value
            for key, value in connection.to_data().items()
            if key != "credentials"
        }
        bindings: JsonObject = {
            key: {"binding": record, "options": section(options, "bindings")[key]}
            for key, record in section(data, "bindings").items()
            if record["connection_id"] == connection_id
        }
        return ConnectionEdit(
            snapshot.owner_id,
            connection_id,
            json.dumps(
                [
                    parameters,
                    section(options, "connections")[connection_id],
                    bindings,
                    {
                        binding.target_id: snapshot.targets[binding.target_id].to_data()
                        for binding in snapshot.bindings.values()
                        if binding.connection_id == connection_id
                    },
                ],
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def _checked(self, edit: ConnectionEdit) -> ConfigurationSnapshot:
        if edit != self.edit(edit.connection_id):
            raise ConfigurationError("configuration_changed")
        return self.current()

    @callback
    def _save(
        self, data: JsonObject, options: JsonObject, *, minor_version: int | None = None
    ) -> ConfigurationSnapshot:
        before = self.current()
        data["revision"] = before.revision + 1
        target_minor = before.minor_version if minor_version is None else minor_version
        if target_minor < before.minor_version:
            raise ConfigurationError("unsupported_minor_version")
        snapshot = load_configuration(
            before.owner_id, data, options, minor_version=target_minor
        )
        if target_minor != before.minor_version:
            self.hass.config_entries.async_update_entry(
                self.entry,
                data=snapshot.to_data(),
                options=snapshot.to_options(),
                minor_version=target_minor,
            )
        else:
            self.hass.config_entries.async_update_entry(
                self.entry, data=snapshot.to_data(), options=snapshot.to_options()
            )
        for listener in tuple(self._listeners):
            listener(snapshot)
        return snapshot

    @callback
    def check_local_access_enabled(self) -> None:
        """Reject an unfinished public setup before changing runtime or storage."""
        from .config_flow import BoschBuderusConfigFlow
        from .local_credentials import LOCAL_STORAGE_MINOR_VERSION

        if (
            BoschBuderusConfigFlow.VERSION != 4
            or BoschBuderusConfigFlow.MINOR_VERSION < LOCAL_STORAGE_MINOR_VERSION
        ):
            raise ConfigurationError("local_setup_not_enabled")

    @callback
    def commit_local_access(
        self, before: ConfigurationSnapshot, proposed: ConfigurationSnapshot
    ) -> ConfigurationSnapshot:
        """Accept verified local data only after the flow advertises its format.

        HA acknowledges an in-memory update, not a synchronous disk transaction.
        No old token is revoked. The full proposal is built immediately before
        this call without an intervening await.
        """
        from .local_credentials import LOCAL_STORAGE_MINOR_VERSION

        self.check_local_access_enabled()
        if self.current() != before or proposed.owner_id != before.owner_id:
            raise ConfigurationError("configuration_changed")
        if (
            proposed.version != before.version
            or proposed.minor_version != LOCAL_STORAGE_MINOR_VERSION
        ):
            raise ConfigurationError("unsupported_minor_version")
        if proposed.minor_version != before.minor_version:
            if self.entry.state not in (
                ConfigEntryState.NOT_LOADED,
                ConfigEntryState.SETUP_IN_PROGRESS,
                ConfigEntryState.MIGRATION_ERROR,
            ):
                raise ConfigurationError("local_storage_upgrade_requires_stop")
            if entry_refresh_in_progress(self.hass, self.entry.entry_id):
                raise ConfigurationError("authentication_in_progress")
        return self._save(
            proposed.to_data(),
            proposed.to_options(),
            minor_version=proposed.minor_version,
        )

    @callback
    def commit_selection(
        self, edit: ConnectionEdit, data: JsonObject, options: JsonObject
    ) -> ConfigurationSnapshot:
        """Save a selection built from this connection's current configuration."""
        self._checked(edit)
        return self._save(data, options)

    @callback
    def connect_cloud(
        self, before: ConfigurationSnapshot, proposed: ConfigurationSnapshot
    ) -> ConfigurationSnapshot:
        """Commit a new verified access without overwriting intervening changes."""
        if self.current() != before or proposed.owner_id != before.owner_id:
            raise ConfigurationError("configuration_changed")
        return self._save(proposed.to_data(), proposed.to_options())

    @callback
    def set_enabled(self, edit: ConnectionEdit, enabled: bool) -> ConfigurationSnapshot:
        before = self._checked(edit)
        data, options = before.to_data(), before.to_options()
        section(options, "connections")[edit.connection_id]["enabled"] = enabled
        return self._save(data, options)

    @callback
    def set_polling_profile(
        self, edit: ConnectionEdit, profile: PollingProfile
    ) -> ConfigurationSnapshot:
        before = self._checked(edit)
        if before.connections[edit.connection_id].kind != CLOUD_KIND:
            raise ConfigurationError("unsupported_connection")
        data, options = before.to_data(), before.to_options()
        section(options, "connections")[edit.connection_id]["polling_profile"] = (
            profile.value
        )
        return self._save(data, options)

    @callback
    def set_local_interval(
        self, edit: ConnectionEdit, seconds: int
    ) -> ConfigurationSnapshot:
        before = self._checked(edit)
        if before.connections[edit.connection_id].kind != LOCAL_KIND:
            raise ConfigurationError("unsupported_connection")
        if type(seconds) is not int or not 60 <= seconds <= 900 or seconds % 60:
            raise ConfigurationError("invalid_local_interval")
        data, options = before.to_data(), before.to_options()
        section(options, "connections")[edit.connection_id]["local_interval"] = seconds
        return self._save(data, options)

    @callback
    def set_local_values(
        self, edit: ConnectionEdit, enabled: bool
    ) -> ConfigurationSnapshot:
        """Let paired local readings supply existing entities of linked targets."""
        before = self._checked(edit)
        if before.connections[edit.connection_id].kind != LOCAL_KIND:
            raise ConfigurationError("unsupported_connection")
        if type(enabled) is not bool:
            raise ConfigurationError("invalid_local_values")
        data, options = before.to_data(), before.to_options()
        targets = {
            binding.target_id
            for binding in before.bindings.values()
            if binding.connection_id == edit.connection_id
        }
        if not targets:
            raise ConfigurationError("binding_changed")
        for target_id in targets:
            section(options, "targets")[target_id]["local_values"] = enabled
        return self._save(data, options)

    @callback
    def set_binding_enabled(
        self, edit: ConnectionEdit, binding_id: str, enabled: bool
    ) -> ConfigurationSnapshot:
        before = self._checked(edit)
        binding = before.bindings.get(binding_id)
        if binding is None or binding.connection_id != edit.connection_id:
            raise ConfigurationError("binding_changed")
        data, options = before.to_data(), before.to_options()
        section(options, "bindings")[binding_id]["enabled"] = enabled
        return self._save(data, options)

    @callback
    def set_targets_enabled(
        self, edit: ConnectionEdit, selected: set[str]
    ) -> ConfigurationSnapshot:
        before = self._checked(edit)
        bindings = {
            key
            for key, binding in before.bindings.items()
            if binding.connection_id == edit.connection_id
        }
        if not selected.issubset(bindings):
            raise ConfigurationError("binding_changed")
        data, options = before.to_data(), before.to_options()
        for key in bindings:
            section(options, "bindings")[key]["enabled"] = key in selected
        return self._save(data, options)

    @callback
    def forget_bindings(
        self, edit: ConnectionEdit, binding_ids: set[str]
    ) -> ConfigurationSnapshot:
        """Forget switched-off systems of a cloud access (P50, R1).

        Only deselected PointT systems of this access. Their targets, the
        Home Assistant devices and entities stay; a later sign-in no longer
        needs to see them.
        """
        before = self._checked(edit)
        options_before = section(before.to_options(), "bindings")
        if not binding_ids or any(
            (binding := before.bindings.get(key)) is None
            or binding.connection_id != edit.connection_id
            or binding.adapter != "pointt"
            or options_before[key]["enabled"]
            for key in binding_ids
        ):
            raise ConfigurationError("binding_changed")
        data, options = before.to_data(), before.to_options()
        for key in binding_ids:
            # A forgotten system stays hidden (R1, P49b): never a stale mark.
            section(options, "targets")[before.bindings[key].target_id].pop(
                "local_plant", None
            )
            del section(data, "bindings")[key]
            del section(options, "bindings")[key]
        for target in section(options, "targets").values():
            target["read_order"] = [
                key
                for key in cast(list[str], target["read_order"])
                if key not in binding_ids
            ]
        return self._save(data, options)

    @callback
    def exchange_gateway(
        self, binding_id: str, old: str, new: str
    ) -> ConfigurationSnapshot:
        """Let a confirmed new gateway continue the same system (P50).

        The target and the binding stay; the binding's locator, the target's
        gateway identity and its compatibility identity change together. The
        fault notification option moves along. A local access stays until
        :meth:`drop_local_identities` after its removal.
        """
        from .const import CONF_FAULT_NOTIFICATIONS
        from .notifications import gateway_notification_key

        before = self.current()
        binding = before.bindings.get(binding_id)
        if (
            binding is None
            or binding.adapter != "pointt"
            or cast(JsonObject, binding.to_data()["locator"]).get("value") != old
            or old == new
        ):
            raise ConfigurationError("binding_changed")
        data, options = before.to_data(), before.to_options()
        record = section(data, "bindings")[binding_id]
        record["locator"] = {**cast(JsonObject, record["locator"]), "value": new}
        target = section(data, "targets")[binding.target_id]
        target["identities"] = [
            {**item, "value": new, "evidence": "confirmed_exchange"}
            if item["namespace"] == "pointt_gateway" and item["value"] == old
            else item
            for item in cast(list[JsonObject], target["identities"])
        ]
        legacy = target.get("legacy_identity")
        if (
            isinstance(legacy, dict)
            and legacy.get("format") == "pointt_v3"
            and legacy.get("gateway_id") == old
        ):
            legacy["gateway_id"] = new
        notifications = options.get(CONF_FAULT_NOTIFICATIONS)
        old_key, new_key = gateway_notification_key(old), gateway_notification_key(new)
        if isinstance(notifications, dict) and old_key in notifications:
            moved = notifications.pop(old_key)
            notifications.setdefault(new_key, moved)
        return self._save(data, options)

    @callback
    def drop_local_identities(self, target_id: str) -> ConfigurationSnapshot | None:
        """Forget unused local identities of a target after an exchange (P50).

        The old gateway's local identity would block setting up a local access
        for the new gateway. Identities a binding still uses stay. None when
        nothing changed.
        """
        before = self.current()
        target_record = before.targets.get(target_id)
        if target_record is None or target_record.kind == "local_installation":
            return None
        used = {
            cast(str, cast(JsonObject, binding.to_data()["locator"])["value"])
            for binding in before.bindings.values()
            if binding.target_id == target_id and binding.adapter == "k40_local"
        }
        data, options = before.to_data(), before.to_options()
        target = section(data, "targets")[target_id]
        identities = cast(list[JsonObject], target["identities"])
        kept = [
            item
            for item in identities
            if item["namespace"] != "local_system_gateway" or item["value"] in used
        ]
        if len(kept) == len(identities):
            return None
        target["identities"] = cast(list[Any], kept)
        return self._save(data, options)

    @callback
    def remove(self, edit: ConnectionEdit) -> ConfigurationSnapshot:
        before = self._checked(edit)
        data, options = before.to_data(), before.to_options()
        del section(data, "connections")[edit.connection_id]
        del section(options, "connections")[edit.connection_id]
        removed = {
            key
            for key, binding in before.bindings.items()
            if binding.connection_id == edit.connection_id
        }
        binding_options = section(before.to_options(), "bindings")
        for key in removed:
            binding = before.bindings[key]
            if binding.adapter == "pointt":
                target = section(options, "targets")[binding.target_id]
                if binding_options[key]["enabled"] and any(
                    other.target_id == binding.target_id and other.adapter == LOCAL_KIND
                    for other in before.bindings.values()
                ):
                    # R1 (P49b): the system keeps showing its local readings.
                    target["local_plant"] = True
                else:
                    target.pop("local_plant", None)  # never a stale mark
            del section(data, "bindings")[key]
            del section(options, "bindings")[key]
        for target in section(options, "targets").values():
            target["read_order"] = [
                key
                for key in cast(list[str], target["read_order"])
                if key not in removed
            ]
        result = self._save(data, options)
        forget_connection_auth(self.hass, self.entry.entry_id, edit.connection_id)
        return result

    async def async_reauthenticate(
        self,
        edit: ConnectionEdit,
        tokens: AuthTokens,
        *,
        request_guard: Callable[[], None] = lambda: None,
    ) -> ConfigurationSnapshot:
        """Confirm every assigned target using the new authenticated session.

        A successful subset is not proof for the remaining targets. Unavailable
        services leave the old credentials untouched and surface a retryable error.
        """

        def ensure_current() -> None:
            request_guard()
            self._checked(edit)

        ensure_current()
        before = self._checked(edit)
        if before.connections[edit.connection_id].kind != CLOUD_KIND:
            raise ConfigurationError("unsupported_connection")
        validate_cloud_credentials(tokens_to_data(tokens))
        configuration = EntryConfiguration(self.entry, edit.connection_id)
        selected = configuration.cloud_data()
        gateways = set(selected["gateway_ids"])
        devices = set(selected["bacon_device_ids"])
        assigned = [
            item
            for item in before.bindings.values()
            if item.connection_id == edit.connection_id
        ]
        if not assigned or any(
            item.adapter not in ("pointt", "bacon") for item in assigned
        ):
            raise ConfigurationError("unverifiable_targets")
        session = async_get_clientsession(self.hass)
        backoff = connection_backoff(self.hass, before.owner_id, edit.connection_id)
        resources = connection_resources(self.hass, before.owner_id, edit.connection_id)
        backoff.raise_if_active()
        if gateways:
            visible = await PointTClient(
                session,
                tokens.access_token,
                backoff=backoff,
                metrics=resources.metrics,
                request_semaphore=resources.semaphore,
                request_guard=ensure_current,
            ).get_gateways()
            if not gateways.issubset(item.gateway_id for item in visible):
                raise ConfigurationError("reauth_targets_missing")
        if devices:
            ensure_current()
            backoff.raise_if_active()
            try:
                visible_devices, account = await discover(
                    session,
                    tokens,
                    backoff=backoff,
                    request_semaphore=resources.semaphore,
                    request_guard=ensure_current,
                )
            except BaconError as error:
                if error.category == "rate_limited":
                    backoff.activate(error.retry_after)
                raise
            if account != selected.get("bacon_account") or not devices.issubset(
                visible_devices
            ):
                raise ConfigurationError("reauth_targets_missing")
        ensure_current()
        latest = self._checked(edit)
        data, options = latest.to_data(), latest.to_options()
        connection = section(data, "connections")[edit.connection_id]
        connection["credentials"] = {
            **cast(JsonObject, connection["credentials"]),
            **tokens_to_data(tokens),
        }
        connection["auth_epoch"] = latest.connections[edit.connection_id].auth_epoch + 1
        return self._save(data, options)
