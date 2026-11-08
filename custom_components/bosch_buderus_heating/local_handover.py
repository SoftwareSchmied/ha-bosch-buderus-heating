"""Coordinate one explicit local handover through public HA entry operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from homeassistant.config_entries import ConfigEntryState

from .auth_lifecycle import async_wait_for_entry_refresh
from .configuration import ConfigurationError, ConfigurationSnapshot
from .const import DOMAIN
from .local_credentials import LOCAL_STORAGE_MINOR_VERSION

if TYPE_CHECKING:
    from .configuration_management import ConfigurationManagement
    from .local_completion import ScopedIdentityContract
    from .local_enrollment import LocalEnrollmentJournal
    from .local_setup import VerifiedLocalSetup

_LOCKS = f"{DOMAIN}.local_handover_locks"


async def _settled[T](task: asyncio.Task[T]) -> T:
    """Do not interrupt a native entry operation halfway through its lifecycle."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


async def complete_local_setup(
    management: ConfigurationManagement,
    journal: LocalEnrollmentJournal,
    attempt_id: str,
    verification: VerifiedLocalSetup,
    *,
    contract: ScopedIdentityContract,
) -> ConfigurationSnapshot:
    """Stop only for the first format transition; restore an interrupted runtime.

    This remains behind the public flow's minor-version gate. Native HA accepts
    the update in memory; the journal retains its token until a later cold start.
    """
    management.check_local_access_enabled()
    hass, entry = management.hass, management.entry
    locks = cast(dict[str, asyncio.Lock], hass.data.setdefault(_LOCKS, {}))
    async with locks.setdefault(entry.entry_id, asyncio.Lock()):
        before = management.current()
        upgrade = before.minor_version < LOCAL_STORAGE_MINOR_VERSION
        stopped = False
        try:
            if upgrade:
                verification.edit.check(before)
                verification.check(verification.edit)
                contract.verified_pairs(
                    before, verification.edit.target_id, verification.access.gateway_id
                )
                await async_wait_for_entry_refresh(hass, entry.entry_id)
                verification.edit.check(management.current())
                verification.check(verification.edit)
                if entry.state == ConfigEntryState.LOADED:
                    stopped = True
                    if not await _settled(
                        hass.async_create_task(
                            hass.config_entries.async_unload(entry.entry_id),
                            "unload entry for local handover",
                        )
                    ):
                        raise ConfigurationError("local_storage_upgrade_requires_stop")
                elif entry.state not in (
                    ConfigEntryState.NOT_LOADED,
                    ConfigEntryState.MIGRATION_ERROR,
                ):
                    raise ConfigurationError("local_storage_upgrade_requires_stop")
            return await journal.commit_verified(
                attempt_id, verification, management, contract=contract
            )
        finally:
            if stopped:

                async def restore() -> None:
                    if (
                        hass.config_entries.async_get_entry(entry.entry_id) is entry
                        and entry.disabled_by is None
                        and entry.state == ConfigEntryState.NOT_LOADED
                        and not await hass.config_entries.async_setup(entry.entry_id)
                    ):
                        raise ConfigurationError("local_runtime_restart_failed")

                await _settled(
                    hass.async_create_task(
                        restore(), "restore entry after local handover"
                    )
                )
