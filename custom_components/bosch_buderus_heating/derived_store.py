"""Saved state of the calculated counters, HA-wide, by key.

Keys start with the gateway ID (``<gateway>:...``), so a removed gateway's
rows can be dropped. Rows are small JSON objects; numbers are
validated by their readers. Saved at most once a minute, at once on request
(a finished defrost) and when Home Assistant stops, so a counter never falls
back after a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .log_helpers import log_failure

_LOGGER = logging.getLogger(__name__)
STORAGE_KEY = f"{DOMAIN}.calculated_values"
STORAGE_VERSION = 1
SAVE_DELAY_SECONDS = 60
MAX_ROWS = 512
_STORE: HassKey[DerivedStore] = HassKey(f"{DOMAIN}_calculated_values")


class DerivedStore:
    """Rows of the calculated counters, loaded once."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._rows: dict[str, dict[str, Any]] = {}
        self._loaded = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    async def async_load(self) -> None:
        """Read the file once; a damaged file starts empty."""
        if self._loaded:
            return
        try:
            stored = await self._store.async_load()
        except Exception as err:
            log_failure(
                _LOGGER,
                logging.WARNING,
                "Saved calculated values could not be read",
                error=err,
            )
            stored = None
        if self._loaded:
            return
        rows = stored.get("rows") if isinstance(stored, dict) else None
        if isinstance(rows, dict):
            for key, value in list(rows.items())[:MAX_ROWS]:
                if isinstance(key, str) and isinstance(value, dict):
                    self._rows[key] = value
        self._loaded = True

    def get(self, key: str) -> dict[str, Any] | None:
        row = self._rows.get(key)
        return dict(row) if row is not None else None

    def set(self, key: str, row: dict[str, Any], *, now: bool = False) -> None:
        if key not in self._rows and len(self._rows) >= MAX_ROWS:
            return
        self._rows[key] = dict(row)
        self._store.async_delay_save(self._data, 0 if now else SAVE_DELAY_SECONDS)

    async def async_forget_gateways(self, gateway_ids: Collection[str]) -> None:
        """Drop the rows of gateways no entry uses any more."""
        await self.async_load()
        prefixes = tuple(f"{gateway_id}:" for gateway_id in gateway_ids)
        forgotten = [key for key in self._rows if prefixes and key.startswith(prefixes)]
        if not forgotten:
            return
        for key in forgotten:
            del self._rows[key]
        await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        return {"rows": dict(self._rows)}


def derived_store(hass: HomeAssistant) -> DerivedStore:
    store = hass.data.get(_STORE)
    if store is None:
        store = hass.data[_STORE] = DerivedStore(hass)
    return store


def stored_number(row: dict[str, Any] | None, key: str) -> float | None:
    """A finite, non-negative number of a row, or ``None``."""
    value = row.get(key) if row is not None else None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if number == number and 0.0 <= number < float("inf") else None
