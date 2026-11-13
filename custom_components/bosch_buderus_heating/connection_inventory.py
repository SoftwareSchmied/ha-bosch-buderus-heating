"""Retain value-free entity identities independently of live connections."""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_SUPPORTED_FEATURES, STATE_UNAVAILABLE, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .configuration import (
    ConfigurationError,
    JsonObject,
    json_object,
    require_identifier,
)
from .configuration_management import ConfigurationManagement
from .connection_runtime import SourceContext
from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)
MAX_ENTITY_RECORDS = 4096
_FIELDS = frozenset(
    {
        "platform",
        "unique_id",
        "target_id",
        "binding_id",
        "adapter",
        "semantic_key",
        "resource_path",
    }
)
# O23: these repeat across thousands of records; one shared string each.
_INTERNED = ("platform", "target_id", "binding_id", "adapter")


@dataclass(frozen=True, slots=True)
class RetainedEntity:
    """No values, credentials, user names, areas or cached write permissions."""

    platform: str
    unique_id: str = field(repr=False)
    target_id: str = field(repr=False)
    binding_id: str = field(repr=False)
    adapter: str
    semantic_key: str = field(repr=False)
    resource_path: str | None = field(repr=False, default=None)

    def to_data(self) -> JsonObject:
        data = {key: getattr(self, key) for key in _FIELDS}
        if isinstance(self.platform, Platform):
            data["platform"] = self.platform.value
        return data

    @classmethod
    def from_data(cls, value: object) -> RetainedEntity:
        data = json_object(value)
        if set(data) != _FIELDS:
            raise ConfigurationError("invalid_inventory_fields")
        for name in _FIELDS - {"resource_path"}:
            require_identifier(data[name])
        if data["platform"] not in PLATFORMS or data["adapter"] not in (
            "pointt",
            "bacon",
            "k40_local",
        ):
            raise ConfigurationError("unsupported_inventory_adapter")
        path = data["resource_path"]
        if path is not None and (
            not isinstance(path, str)
            or not path.startswith("/")
            or len(path) > 1024
            or any(char in path for char in "?#\\")
            or not path.isprintable()
        ):
            raise ConfigurationError("invalid_inventory_path")
        for name in _INTERNED:
            value = data[name]
            if type(value) is str:
                data[name] = sys.intern(value)
        return cls(**cast(dict[str, Any], data))


class ConnectionInventory:
    """Serialize only reviewed identity descriptions, with ordered disk writes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        self.hass = hass
        self.entry = entry
        self._store = Store[dict[str, Any]](
            hass,
            1,
            f"{DOMAIN}.inventory.{entry.entry_id}",
            private=True,
            atomic_writes=True,
        )
        self._entities: dict[tuple[str, str], RetainedEntity] = {}
        self._lock = asyncio.Lock()
        self._removed = False
        self._writable = True
        self.healthy = True

    @property
    def entities(self) -> tuple[RetainedEntity, ...]:
        return tuple(self._entities.values())

    async def load(self) -> None:
        try:
            raw = await self._store.async_load()
            if raw is None:
                return
            data = json_object(raw)
            records = data.get("entities")
            if (
                data.get("owner_id") != self.entry.entry_id
                or not isinstance(records, list)
                or len(records) > MAX_ENTITY_RECORDS
            ):
                raise ConfigurationError("invalid_inventory")
            items = [RetainedEntity.from_data(item) for item in records]
            snapshot = ConfigurationManagement(self.hass, self.entry).current()
            if any(item.target_id not in snapshot.targets for item in items):
                raise ConfigurationError("unknown_inventory_target")
            entities = {(item.platform, item.unique_id): item for item in items}
            if len(entities) != len(items):
                raise ConfigurationError("duplicate_inventory_entity")
            self._entities = entities
        except Exception as error:
            # STORE-3: HA's Store raises HomeAssistantError (read failure),
            # UnsupportedStorageVersionError (newer file), KeyError or
            # TypeError (no envelope). The registry fallback works without it.
            _LOGGER.warning(
                "The entity inventory could not be loaded (%s); the registry "
                "entries are used instead",
                str(error)
                if isinstance(error, ConfigurationError)
                else type(error).__name__,
            )
            _LOGGER.debug("Inventory load failure", exc_info=True)
            self.healthy = False
            self._writable = False

    @callback
    def record(self, item: RetainedEntity, context: SourceContext) -> bool:
        """Only a current, correctly bound source can extend the known inventory."""
        if (
            self._removed
            or not context.current
            or context.runtime.entry is not self.entry
        ):
            return False
        item = RetainedEntity.from_data(item.to_data())
        binding = context.runtime.snapshot.bindings.get(
            context.generation.binding_id or ""
        )
        if binding is None or (item.binding_id, item.target_id, item.adapter) != (
            context.generation.binding_id,
            binding.target_id,
            binding.adapter,
        ):
            raise ConfigurationError("inventory_binding_conflict")
        key = item.platform, item.unique_id
        old = self._entities.get(key)
        if old is not None and (
            old.target_id != item.target_id or old.adapter != item.adapter
        ):
            raise ConfigurationError("inventory_identity_conflict")
        if old is None and len(self._entities) >= MAX_ENTITY_RECORDS:
            raise ConfigurationError("inventory_limit")
        self._entities[key] = item
        self._writable = True
        return True

    async def save(self) -> None:
        async with self._lock:
            if self._removed or not self._writable:
                return
            data = {
                "owner_id": self.entry.entry_id,
                "entities": [item.to_data() for item in self.entities],
            }
            try:
                await self._store.async_save(data)
                self.healthy = True
            except OSError, ValueError:
                self.healthy = False

    @callback
    def restore_unavailable(self, *, bindings: set[str] | None = None) -> None:
        """Keep owned registry entries visible even when inventory is unavailable.

        The HA registry remains authoritative for entity IDs, device assignments
        and user customizations. No new device or control is created from a cache.
        """
        ConfigurationManagement(self.hass, self.entry).current()
        registry = er.async_get(self.hass)
        selected = {
            (item.platform, item.unique_id)
            for item in self.entities
            if bindings is not None and item.binding_id in bindings
        }
        for registered in er.async_entries_for_config_entry(
            registry, self.entry.entry_id
        ):
            if registered.platform != DOMAIN or registered.disabled:
                continue
            if (
                bindings is not None
                and (registered.domain, registered.unique_id) not in selected
            ):
                continue
            registered.write_unavailable_state(self.hass)
            state = self.hass.states.get(registered.entity_id)
            if state is not None:
                attributes = {
                    key: value
                    for key, value in state.attributes.items()
                    if key
                    in (
                        "restored",
                        "friendly_name",
                        "icon",
                        "device_class",
                        "unit_of_measurement",
                    )
                }
                attributes[ATTR_SUPPORTED_FEATURES] = 0
                self.hass.states.async_set(
                    registered.entity_id, STATE_UNAVAILABLE, attributes
                )

    async def rekey(self, binding_id: str, old_prefix: str, new_prefix: str) -> bool:
        """Move one binding's records to new unique IDs (P50, gateway exchange).

        Only while the owner is stopped: the stored file is rewritten. A record
        whose new key already exists is dropped. True when something changed.
        """
        await self.load()
        if not self.healthy:
            return False
        changed = False
        entities: dict[tuple[str, str], RetainedEntity] = {}
        for item in self._entities.values():
            if item.binding_id == binding_id and item.unique_id.startswith(old_prefix):
                item = RetainedEntity.from_data(
                    {
                        **item.to_data(),
                        "unique_id": new_prefix + item.unique_id[len(old_prefix) :],
                    }
                )
                changed = True
            entities.setdefault((item.platform, item.unique_id), item)
        if changed:
            self._entities = entities
            await self.save()
        return changed

    async def remove_owner(self) -> None:
        """Only deletion of the complete HA entry removes the inventory file."""
        async with self._lock:
            self._removed = True
            self._entities.clear()
            await self._store.async_remove()
