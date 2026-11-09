"""Which resource paths only disabled entities show (G3, O51).

Every entity is noted with the paths it reads when the integration hands it
to Home Assistant, also one the entity registry keeps disabled (Home
Assistant then never adds it). A path counts as unused only when at least one
noted entity reads it and the registry keeps every one of them disabled. A
path without a noted entity, or with an entity not registered (yet), counts
as used: reading it as usual is always the safe answer.

Entities are never left out because of this: it only lets the readers of a
path read it less often while nobody shows it. Enabling an entity in the
registry makes its paths used again within ``REFRESH_DELAY`` seconds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HassJob, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
# Registry changes are bursty while entities are registered: worked out once
# after the burst instead of once per registered entity.
REFRESH_DELAY = 1.0
_USES: HassKey[dict[str, EntityUse]] = HassKey(f"{DOMAIN}.entity_use")
_NONE: frozenset[str] = frozenset()


def entity_paths(entity: Entity) -> frozenset[str]:
    """The resource paths an entity reads: its own and the ones it combines."""
    description = getattr(entity, "entity_description", None)
    paths = {
        value
        for value in (
            getattr(description, "resource_path", None),
            getattr(description, "secondary_resource_path", None),
            getattr(entity, "resource_path", None),
            *getattr(entity, "extra_resource_paths", ()),
        )
        if isinstance(value, str) and value.startswith("/")
    }
    return frozenset(paths)


def _gateway(entity: Entity) -> str | None:
    gateway = getattr(getattr(entity, "coordinator", None), "gateway", None)
    value = getattr(gateway, "gateway_id", None)
    return value if isinstance(value, str) and value else None


class EntityUse:
    """Per config entry: the paths whose noted entities are all disabled."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # (platform domain, unique ID) -> (gateway ID, paths read).
        self._entities: dict[tuple[str, str], tuple[str, frozenset[str]]] = {}
        # Gateway ID -> paths only disabled entities read; replaced, never
        # changed, so its identity tells a change.
        self._disabled: dict[str, frozenset[str]] = {}
        # Gateway ID -> paths any noted entity reads (A3); replaced likewise.
        self._noted: dict[str, frozenset[str]] = {}
        self._listeners: list[Callable[[], None]] = []
        # Listeners whose failure was already reported as a warning.
        self._failed: list[Callable[[], None]] = []
        self._pending: CALLBACK_TYPE | None = None
        self._closed = False
        self._job = HassJob(self._refresh_later, cancel_on_shutdown=True)
        self._unsubscribe = hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_updated
        )

    @callback
    def note(self, domain: str, entity: Entity) -> None:
        """Note an entity handed to Home Assistant, enabled or not."""
        unique_id = entity.unique_id
        gateway = _gateway(entity)
        paths = entity_paths(entity)
        if self._closed or not unique_id or gateway is None or not paths:
            return
        key, value = (str(domain), unique_id), (gateway, paths)
        if self._entities.get(key) != value:
            self._entities[key] = value
            self._schedule()

    def disabled_paths(self, gateway_id: str) -> frozenset[str]:
        """Paths of this gateway only disabled entities read (same object
        until the answer changes)."""
        return self._disabled.get(gateway_id, _NONE)

    def noted_paths(self, gateway_id: str) -> frozenset[str]:
        """Paths of this gateway any noted entity reads, enabled or not, as
        of the last refresh (same object until the answer changes)."""
        return self._noted.get(gateway_id, _NONE)

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener`` whenever the disabled or noted paths change."""
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)
            if listener in self._failed:
                self._failed.remove(listener)

        return remove

    @callback
    def close(self) -> None:
        self._closed = True
        self._unsubscribe()
        if self._pending is not None:
            self._pending()
            self._pending = None
        self._listeners.clear()
        self._failed.clear()

    @callback
    def _registry_updated(
        self, event: Event[er.EventEntityRegistryUpdatedData]
    ) -> None:
        data: dict[str, Any] = dict(event.data)
        if data.get("action") == "update" and not (
            {"disabled_by", "unique_id", "platform"} & set(data.get("changes", {}))
        ):
            return  # a new name, icon or area changes nothing here
        self._schedule()

    def _schedule(self) -> None:
        if self._pending is None and not self._closed:
            self._pending = async_call_later(self._hass, REFRESH_DELAY, self._job)

    @callback
    def _refresh_later(self, _now: object) -> None:
        self._pending = None
        self.refresh()

    @callback
    def refresh(self) -> None:
        """Work the disabled paths out now; tell the listeners of a change."""
        if self._closed:
            return
        registry = er.async_get(self._hass)
        used: dict[str, set[str]] = {}
        disabled: dict[str, set[str]] = {}
        for (domain, unique_id), (gateway, paths) in self._entities.items():
            entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
            entry = registry.async_get(entity_id) if entity_id is not None else None
            target = disabled if entry is not None and entry.disabled else used
            target.setdefault(gateway, set()).update(paths)
        result = {
            gateway: frozenset(paths - used.get(gateway, set()))
            for gateway, paths in disabled.items()
        }
        result = {gateway: paths for gateway, paths in result.items() if paths}
        noted = {
            gateway: frozenset(used.get(gateway, set()) | disabled.get(gateway, set()))
            for gateway in used.keys() | disabled.keys()
        }
        if result == self._disabled and noted == self._noted:
            return
        if result != self._disabled:
            self._disabled = result
        if noted != self._noted:
            self._noted = noted
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception as err:
                if listener in self._failed:
                    _LOGGER.debug(
                        "A listener of disabled entities failed", exc_info=True
                    )
                    continue
                # Once per listener, with the error kind only: a lasting fault
                # in the reading pace must not stay invisible.
                self._failed.append(listener)
                _LOGGER.warning(
                    "Adjusting the reading pace to enabled or disabled entities "
                    "failed (%s); further failures are logged at debug level",
                    type(err).__name__,
                )
                _LOGGER.debug("A listener of disabled entities failed", exc_info=True)


@callback
def entity_use(hass: HomeAssistant, entry: ConfigEntry[Any]) -> EntityUse:
    """The entity use of a config entry, created on first use until unload."""
    uses = hass.data.setdefault(_USES, {})
    if (use := uses.get(entry.entry_id)) is not None:
        return use
    created = uses[entry.entry_id] = EntityUse(hass)

    @callback
    def drop() -> None:
        created.close()
        if uses.get(entry.entry_id) is created:
            del uses[entry.entry_id]

    entry.async_on_unload(drop)
    return created
