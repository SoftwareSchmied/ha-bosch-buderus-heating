"""Keep rotating credentials alive across Home Assistant entry unloads."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .auth_diagnostics import auth_diagnostics_for
from .const import DOMAIN
from .data import tokens_from_data, tokens_to_data
from .pointt import AuthTokens, OAuthClient, TokenManager
from .pointt.transport import RateLimitBackoff
from .runtime import BoschBuderusRuntimeData

ACCOUNT_BACKOFFS: HassKey[dict[str, RateLimitBackoff]] = HassKey(
    f"{DOMAIN}.account_backoffs"
)
_TOKEN_MANAGERS: HassKey[dict[str, TokenManager]] = HassKey(f"{DOMAIN}.token_managers")


@callback
def entry_auth(
    hass: HomeAssistant, entry: ConfigEntry[Any], oauth_client: OAuthClient
) -> tuple[TokenManager, RateLimitBackoff]:
    """Reuse account authentication even after HA discards runtime_data."""
    tokens = tokens_from_data(entry.data)
    runtime = getattr(entry, "runtime_data", None)
    backoff = hass.data.setdefault(ACCOUNT_BACKOFFS, {}).setdefault(
        entry.entry_id,
        runtime.client.backoff
        if isinstance(runtime, BoschBuderusRuntimeData)
        else RateLimitBackoff(),
    )
    managers = hass.data.setdefault(_TOKEN_MANAGERS, {})
    previous = managers.get(entry.entry_id)
    recorder = auth_diagnostics_for(hass, entry.entry_id)
    if previous is not None and previous.tokens == tokens:
        if recorder is not None:
            previous.set_observer(recorder.refresh_finished)
        return previous, backoff

    persisted_tokens = tokens

    @callback
    def record_persist(outcome: str) -> None:
        if (current_recorder := auth_diagnostics_for(hass, entry.entry_id)) is not None:
            current_recorder.persist_result(outcome)

    @callback
    def persist_tokens(updated_tokens: AuthTokens) -> None:
        nonlocal persisted_tokens
        # A refresh from the previous login must not overwrite replacement
        # credentials or restore an entry that the user has removed.
        if hass.config_entries.async_get_entry(entry.entry_id) is not entry:
            record_persist("skipped_entry_removed")
            return
        try:
            current = tokens_from_data(entry.data)
        except ValueError:
            record_persist("skipped_invalid_entry_data")
            return
        if current != persisted_tokens:
            record_persist("skipped_entry_credentials_changed")
            return
        persisted_tokens = updated_tokens
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, **tokens_to_data(updated_tokens)}
        )
        record_persist("saved")

    manager = TokenManager(
        oauth_client,
        tokens,
        persist_tokens,
        backoff=backoff,
        observer=recorder.refresh_finished if recorder is not None else None,
    )
    managers[entry.entry_id] = manager
    if recorder is not None:
        recorder.credentials_loaded(
            tokens, reason="start" if previous is None else "credentials_changed"
        )
    return manager, backoff


@callback
def entry_token_manager(hass: HomeAssistant, entry_id: str) -> TokenManager | None:
    """Return the retained token manager of an entry, if any."""
    return hass.data.get(_TOKEN_MANAGERS, {}).get(entry_id)


@callback
def forget_entry_auth(hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
    """Drop account memory on removal; an outstanding refresh cannot restore it."""
    hass.data.get(ACCOUNT_BACKOFFS, {}).pop(entry.entry_id, None)
    hass.data.get(_TOKEN_MANAGERS, {}).pop(entry.entry_id, None)
