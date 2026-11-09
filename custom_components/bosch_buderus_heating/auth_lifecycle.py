"""Keep rotating credentials alive across Home Assistant entry unloads."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .auth_diagnostics import auth_diagnostics_for, forget_auth_history
from .cloud_pauses import cloud_pause_store
from .configuration_access import CredentialContext, EntryConfiguration
from .const import DOMAIN
from .pointt import AuthTokens, OAuthClient, PointTError, TokenManager
from .pointt.metrics import RequestMetrics
from .pointt.transport import RateLimitBackoff
from .runtime import BoschBuderusRuntimeData

type ConnectionKey = tuple[str, str]
REFRESH_MIGRATION_TIMEOUT_SECONDS = 30

ACCOUNT_BACKOFFS: HassKey[dict[ConnectionKey, RateLimitBackoff]] = HassKey(
    f"{DOMAIN}.account_backoffs"
)
_TOKEN_MANAGERS: HassKey[dict[ConnectionKey, TokenManager]] = HassKey(
    f"{DOMAIN}.token_managers"
)
_AUTH_CONTEXTS: HassKey[dict[ConnectionKey, CredentialContext]] = HassKey(
    f"{DOMAIN}.auth_contexts"
)


@dataclass(slots=True)
class CloudRequestResources:
    """Share resource HTTP slots and PointT accounting across one saved access.

    OAuth remains outside these slots and is serialized by TokenManager. Bacon
    shares the HTTP limit, but is not counted in the existing PointT metrics.
    """

    metrics: RequestMetrics = field(default_factory=RequestMetrics)
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(3))


_REQUEST_RESOURCES: HassKey[dict[ConnectionKey, CloudRequestResources]] = HassKey(
    f"{DOMAIN}.cloud_request_resources"
)


@callback
def connection_resources(
    hass: HomeAssistant, owner_id: str, connection_id: str
) -> CloudRequestResources:
    """Reuse the same budget during management, reauthentication and reloads."""
    return hass.data.setdefault(_REQUEST_RESOURCES, {}).setdefault(
        (owner_id, connection_id), CloudRequestResources()
    )


@callback
def entry_auth(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    oauth_client: OAuthClient,
    *,
    connection_id: str | None = None,
) -> tuple[TokenManager, RateLimitBackoff]:
    """Reuse account authentication even after HA discards runtime_data."""
    configuration = EntryConfiguration(entry, connection_id)
    tokens = configuration.tokens()
    context = configuration.context()
    key = (entry.entry_id, configuration.connection_id)
    runtime = getattr(entry, "runtime_data", None)
    backoff = _account_backoff(
        hass,
        key,
        lambda: (
            runtime.client.backoff
            if configuration.legacy and isinstance(runtime, BoschBuderusRuntimeData)
            else RateLimitBackoff()
        ),
    )
    managers = hass.data.setdefault(_TOKEN_MANAGERS, {})
    contexts = hass.data.setdefault(_AUTH_CONTEXTS, {})
    previous = managers.get(key)
    recorder = auth_diagnostics_for(hass, *key)
    if (
        previous is not None
        and previous.tokens == tokens
        and context.same_login(contexts.get(key))
        # A rejected manager never asks again. A new start or check of this
        # access may try the stored token once more, for example after a
        # temporary refusal; a genuinely invalid token is rejected again.
        and not previous.refresh_rejected
    ):
        if recorder is not None:
            previous.set_observer(recorder.refresh_finished)
        return previous, backoff

    persisted_tokens = tokens

    @callback
    def record_persist(outcome: str) -> None:
        if (current := auth_diagnostics_for(hass, *key)) is not None:
            current.persist_result(outcome)

    @callback
    def persist_tokens(updated_tokens: AuthTokens) -> None:
        nonlocal persisted_tokens
        # A refresh from the previous login must not overwrite replacement
        # credentials or restore an entry that the user has removed.
        if hass.config_entries.async_get_entry(entry.entry_id) is not entry:
            record_persist("skipped_entry_removed")
            return
        if managers.get(key) is not manager:
            record_persist("skipped_manager_replaced")
            return
        data = configuration.rotated_data(context, persisted_tokens, updated_tokens)
        if data is None:
            record_persist("skipped_entry_credentials_changed")
            return
        hass.config_entries.async_update_entry(entry, data=data)
        persisted_tokens = updated_tokens
        record_persist("saved")

    manager = TokenManager(
        oauth_client,
        tokens,
        persist_tokens,
        backoff=backoff,
        observer=recorder.refresh_finished if recorder is not None else None,
    )
    managers[key] = manager
    contexts[key] = context
    if recorder is not None:
        if previous is None:
            reason = "start"
        elif previous.refresh_rejected and previous.tokens == tokens:
            reason = "retry_after_rejection"
        else:
            reason = "credentials_changed"
        recorder.credentials_loaded(tokens, reason=reason)
    return manager, backoff


@callback
def connection_token_manager(
    hass: HomeAssistant, owner_id: str, connection_id: str
) -> TokenManager | None:
    """Return the retained token manager of one saved access, if any."""
    return hass.data.get(_TOKEN_MANAGERS, {}).get((owner_id, connection_id))


@callback
def entry_refresh_in_progress(hass: HomeAssistant, owner_id: str) -> bool:
    """Do not change storage while a retained credential rotation is pending."""
    return any(
        manager.refresh_in_progress
        for (owner, _), manager in hass.data.get(_TOKEN_MANAGERS, {}).items()
        if owner == owner_id
    )


async def async_wait_for_entry_refresh(hass: HomeAssistant, owner_id: str) -> None:
    """Allow retained rotations to persist before changing their storage format.

    Transport failures leave the previous credentials available for scoped reauth
    or retry. Unexpected persistence failures must stop migration. Timeout and
    caller cancellation never cancel the shielded credential rotation.
    """
    async with asyncio.timeout(REFRESH_MIGRATION_TIMEOUT_SECONDS):
        for (owner, _), manager in tuple(hass.data.get(_TOKEN_MANAGERS, {}).items()):
            if owner != owner_id:
                continue
            with suppress(PointTError):
                await manager.async_wait_for_refresh()


@callback
def connection_backoff(
    hass: HomeAssistant, owner_id: str, connection_id: str
) -> RateLimitBackoff:
    """Keep an existing account pause during verification of replacement credentials."""
    return _account_backoff(hass, (owner_id, connection_id), RateLimitBackoff)


@callback
def _account_backoff(
    hass: HomeAssistant,
    key: ConnectionKey,
    factory: Callable[[], RateLimitBackoff],
) -> RateLimitBackoff:
    """Share one pause per saved access and keep it across restarts."""
    backoffs = hass.data.setdefault(ACCOUNT_BACKOFFS, {})
    if (backoff := backoffs.get(key)) is None:
        backoff = backoffs[key] = factory()
        cloud_pause_store(hass).attach(key, backoff)
    return backoff


@callback
def forget_connection_auth(
    hass: HomeAssistant, owner_id: str, connection_id: str
) -> None:
    """Remove one saved access without touching another connection's session."""
    cloud_pause_store(hass).discard((owner_id, connection_id))
    forget_auth_history(hass, owner_id, connection_id)
    for registry in (
        ACCOUNT_BACKOFFS,
        _TOKEN_MANAGERS,
        _AUTH_CONTEXTS,
        _REQUEST_RESOURCES,
    ):
        hass.data.get(registry, {}).pop((owner_id, connection_id), None)


@callback
def forget_entry_auth(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    """Drop account memory on removal; an outstanding refresh cannot restore it."""
    cloud_pause_store(hass).discard_owner(entry.entry_id)
    forget_auth_history(hass, entry.entry_id)
    for registry in (
        ACCOUNT_BACKOFFS,
        _TOKEN_MANAGERS,
        _AUTH_CONTEXTS,
        _REQUEST_RESOURCES,
    ):
        entries = hass.data.get(registry, {})
        for key in tuple(entries):
            if key[0] == entry.entry_id:
                entries.pop(key, None)
