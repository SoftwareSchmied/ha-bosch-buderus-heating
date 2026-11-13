"""Atomic connection changes on current storage, without activating migration."""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth_lifecycle import (
    connection_backoff,
    connection_resources,
    entry_refresh_in_progress,
    forget_connection_auth,
)
from .bacon.flow import discover
from .bacon.models import BaconError, account_key, token_subject
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
from .const import DOMAIN, PollingProfile
from .data import tokens_to_data
from .icom_profile import ICOM_PROFILE_KEY, ICOM_PROFILE_REVISION
from .pointt import AuthTokens, PointTClient
from .repair_scope import repair_data, repair_id

# Switched-on systems a new sign-in did not find; they stay configured.
TARGETS_NOT_FOUND_PREFIX = "cloud_targets_not_found_"


def section(data: JsonObject, name: str) -> dict[str, JsonObject]:
    """Use a collection from an already validated snapshot."""
    return cast(dict[str, JsonObject], data[name])


def signed_in_account(tokens: AuthTokens) -> str | None:
    """Hash of the sign-in's account subject, or ``None`` if unreadable.

    Only the hash is ever stored, never the subject itself.
    """
    try:
        return account_key(token_subject(tokens.access_token))
    except BaconError:
        return None


def account_evidence(account: str | None) -> JsonObject | None:
    """The stored form of an account hash for a cloud connection."""
    return {"account_key": account} if account else None


def stored_account(connection: JsonObject) -> str | None:
    """The account hash a cloud access belongs to, if known.

    New accesses store it as account evidence; older ones still have the
    hash of the account their additional cloud devices were found with.
    """
    evidence = connection.get("account_evidence")
    if isinstance(evidence, dict):
        key = evidence.get("account_key")
        if isinstance(key, str) and key:
            return key
    legacy = connection.get("legacy_bacon_account")
    return legacy if isinstance(legacy, str) and legacy else None


def _not_found_issue_id(entry: ConfigEntry[Any], connection_id: str) -> str:
    return repair_id(TARGETS_NOT_FOUND_PREFIX, entry, connection_id)


@callback
def not_found_bindings(
    hass: HomeAssistant, entry: ConfigEntry[Any], connection_id: str
) -> tuple[str, ...]:
    """The bindings a repair note currently names as not found, if any."""
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, _not_found_issue_id(entry, connection_id)
    )
    value = (issue.data or {}).get("bindings") if issue is not None else None
    if not isinstance(value, str):
        return ()
    return tuple(key for key in value.split(",") if key)


@callback
def set_not_found_issue(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    snapshot: ConfigurationSnapshot,
    connection_id: str,
    binding_ids: Collection[str],
) -> None:
    """Name switched-on systems a sign-in did not find; remove the note otherwise.

    Only existing, switched-on bindings of this access are named. The texts
    show the last four characters of each identifier, as the selection does.
    """
    issue_id = _not_found_issue_id(entry, connection_id)
    options = section(snapshot.to_options(), "bindings")
    kept = sorted(
        key
        for key in set(binding_ids)
        if (binding := snapshot.bindings.get(key)) is not None
        and binding.connection_id == connection_id
        and options[key]["enabled"]
    )
    if not kept:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    if tuple(kept) == not_found_bindings(hass, entry, connection_id):
        return
    names = ", ".join(
        str(cast(JsonObject, snapshot.bindings[key].to_data()["locator"])["value"])[-4:]
        for key in kept
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="cloud_targets_not_found",
        translation_placeholders={
            "title": entry.title,
            "count": str(len(kept)),
            "systems": names,
        },
        data={**repair_data(entry, connection_id), "bindings": ",".join(kept)},
    )


@callback
def sync_not_found_issues(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    snapshot: ConfigurationSnapshot,
    *,
    found: Callable[[str], bool] = lambda _: False,
) -> None:
    """Keep the "not found" notes of an entry current.

    A system that delivers data again, was switched off, forgotten or whose
    access was removed is no longer named. Never sends a request.
    """
    registry = ir.async_get(hass)
    for (domain, issue_id), issue in tuple(registry.issues.items()):
        data = issue.data or {}
        if (
            domain != DOMAIN
            or not issue_id.startswith(TARGETS_NOT_FOUND_PREFIX)
            or data.get("entry_id") != entry.entry_id
        ):
            continue
        connection = data.get("connection_id")
        if not isinstance(connection, str) or connection not in snapshot.connections:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue
        listed = not_found_bindings(hass, entry, connection)
        set_not_found_issue(
            hass,
            entry,
            snapshot,
            connection,
            [key for key in listed if not found(key)],
        )


@callback
def clear_not_found_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Every "not found" note of a removed entry, loaded or not."""
    registry = ir.async_get(hass)
    for domain, issue_id in tuple(registry.issues):
        issue = registry.issues[(domain, issue_id)]
        if (
            domain == DOMAIN
            and issue_id.startswith(TARGETS_NOT_FOUND_PREFIX)
            and (issue.data or {}).get("entry_id") == entry_id
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


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
        # A switched-off, forgotten or removed system is no longer "not found".
        sync_not_found_issues(self.hass, self.entry, snapshot)
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
            or proposed.minor_version < LOCAL_STORAGE_MINOR_VERSION
            or proposed.minor_version < before.minor_version
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
        settings = section(options, "connections")[edit.connection_id]
        settings["polling_profile"] = profile.value
        # I1: the user's choice is never switched again.
        settings[ICOM_PROFILE_KEY] = max(
            ICOM_PROFILE_REVISION, _revision(settings.get(ICOM_PROFILE_KEY))
        )
        return self._save(data, options)

    @callback
    def switch_icom_profile(self, connection_id: str) -> bool:
        """I1: switch an existing connection with an iCom gateway once to the
        cloud-friendly profile; True if it was switched.

        The revision is saved with it, so a later choice of the user (or a
        connection already on the profile) is never switched again.
        """
        before = self.current()
        connection = before.connections.get(connection_id)
        if connection is None or connection.kind != CLOUD_KIND:
            return False
        data, options = before.to_data(), before.to_options()
        settings = section(options, "connections")[connection_id]
        if _revision(settings.get(ICOM_PROFILE_KEY)) >= ICOM_PROFILE_REVISION:
            return False
        if settings.get("polling_profile") == PollingProfile.CLOUD_FRIENDLY.value:
            return False  # nothing to switch; a later choice is marked then
        settings["polling_profile"] = PollingProfile.CLOUD_FRIENDLY.value
        settings[ICOM_PROFILE_KEY] = ICOM_PROFILE_REVISION
        self._save(data, options)
        return True

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
        """Forget switched-off systems and devices of a cloud access (P50, R1).

        Only deselected systems (PointT) and devices of this access. Their
        targets, the Home Assistant devices and entities stay; a later sign-in
        no longer needs to see them.
        """
        before = self._checked(edit)
        options_before = section(before.to_options(), "bindings")
        if not binding_ids or any(
            (binding := before.bindings.get(key)) is None
            or binding.connection_id != edit.connection_id
            or binding.adapter not in ("pointt", "bacon")
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
        """Confirm the account using the new authenticated session.

        A stored account (its hash) must match the sign-in; another account
        is refused. Without a stored account, at least one switched-on system
        or device must be listed (any assigned one while all are switched
        off), and an access without any system cannot be confirmed at all.
        Switched-off systems are not looked up and never block.
        Switched-on systems the listing lacks stay configured and are named in
        a repair note that explains how to forget them. Unavailable services
        leave the old credentials untouched and surface a retryable error.
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
        # A stored account must match; only without one do the systems the
        # sign-in can see confirm it.
        evidence = stored_account(before.connections[edit.connection_id].to_data())
        signed_in = signed_in_account(tokens)
        if evidence is not None and signed_in is not None and signed_in != evidence:
            raise ConfigurationError("reauth_wrong_account")
        confirmed = evidence is not None and signed_in == evidence
        assigned = {
            key: item
            for key, item in before.bindings.items()
            if item.connection_id == edit.connection_id
        }
        if not assigned:
            # Nothing left to look up: only the stored account can confirm it.
            if not confirmed:
                raise ConfigurationError("reauth_no_targets")
            ensure_current()
            return self._save_reauthentication(edit, tokens, signed_in, set())
        if any(item.adapter not in ("pointt", "bacon") for item in assigned.values()):
            raise ConfigurationError("unverifiable_targets")
        binding_options = section(before.to_options(), "bindings")
        enabled = {key for key in assigned if binding_options[key]["enabled"]}
        candidates = enabled or set(assigned)
        locators = {
            key: str(cast(JsonObject, item.to_data()["locator"])["value"])
            for key, item in assigned.items()
        }
        session = async_get_clientsession(self.hass)
        backoff = connection_backoff(self.hass, before.owner_id, edit.connection_id)
        resources = connection_resources(self.hass, before.owner_id, edit.connection_id)
        backoff.raise_if_active()
        visible: set[str] = set()
        if any(assigned[key].adapter == "pointt" for key in candidates):
            listed = await PointTClient(
                session,
                tokens.access_token,
                backoff=backoff,
                metrics=resources.metrics,
                request_semaphore=resources.semaphore,
                request_guard=ensure_current,
            ).get_gateways()
            gateway_ids = {item.gateway_id for item in listed}
            visible.update(
                key
                for key, item in assigned.items()
                if item.adapter == "pointt" and locators[key] in gateway_ids
            )
        # Switched-on devices are looked up to name missing ones; switched-off
        # ones only while no system has confirmed the account yet.
        if any(assigned[key].adapter == "bacon" for key in candidates) and (
            enabled or not visible
        ):
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
            if account != selected.get("bacon_account"):
                raise ConfigurationError("reauth_wrong_account")
            visible.update(
                key
                for key, item in assigned.items()
                if item.adapter == "bacon" and locators[key] in visible_devices
            )
        if not confirmed and not visible & candidates:
            raise ConfigurationError("reauth_targets_missing")
        ensure_current()
        return self._save_reauthentication(edit, tokens, signed_in, enabled - visible)

    def _save_reauthentication(
        self,
        edit: ConnectionEdit,
        tokens: AuthTokens,
        signed_in: str | None,
        missing: set[str],
    ) -> ConfigurationSnapshot:
        latest = self._checked(edit)
        data, options = latest.to_data(), latest.to_options()
        connection = section(data, "connections")[edit.connection_id]
        connection["credentials"] = {
            **cast(JsonObject, connection["credentials"]),
            **tokens_to_data(tokens),
        }
        connection["auth_epoch"] = latest.connections[edit.connection_id].auth_epoch + 1
        if connection.get("account_evidence") is None and signed_in is not None:
            # Accepted by the stored account or its systems: later sign-ins
            # must match this account (only its hash is stored).
            connection["account_evidence"] = account_evidence(signed_in)
        result = self._save(data, options)
        set_not_found_issue(self.hass, self.entry, result, edit.connection_id, missing)
        return result


def _revision(value: object) -> int:
    return value if type(value) is int else 0
