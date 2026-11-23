"""Optional devices for the components of a system (off by default).

A system lists its components with a type number and a place (see
``plant_inputs``). With the option ``component_devices`` on, every component
of a gateway's list gets a Home Assistant device of its own, connected
through the gateway's device. Such a device only informs: it holds no
entity, and no entity ever changes its device.

- Identifier: ``(DOMAIN, "<gateway>:component:<place>:<type>")``. It holds
  no running number: two components of the same type at the same place share
  one device, whatever order the list has. It begins with the gateway, so the
  cleanup of deselected gateways of the older entry format covers it, and its
  segment ``component`` is never one the merge of logical devices removes.
- No device for the gateway's own rows, for a row without a valid type
  (type 0) or for a row without a type or place number.
- Name (only when the device is created, in the language of the Home
  Assistant server): the product name the component reports, else the name
  of its place with the component name, else with the type number. Model:
  the component name of the table; model ID: the type number. Never a
  serial number.
- Software version: only from the list shown (one source), the whole text
  the component reports, checked as in the component list
  (``component_rows.clean_version``: letters, digits and separators with a
  digit, at most 64 characters, never with a control character and never
  equal to an identifier of its row); none when two components of the same
  type at the same place report different ones. It never appears in an
  entity attribute or in the log.
- The gateway's device comes first: without it nothing is created; a device
  without its link gets it at the start.
- At a start, every component listed gets its device. While running, only a
  component listed for the first time does: a device deleted by the user
  comes back at the next start. A component no longer listed keeps its
  device (it can be deleted, it has no entities).
- Switching the option off removes every component device of the entry,
  also those the user renamed or placed; Home Assistant keeps their names
  and areas for 30 days and restores them when the option is switched on
  again within that time.
- A view from local readings alone (``read_only_view``) never changes the
  registry.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .component_rows import ComponentRow, clean_version
from .const import DOMAIN
from .ha_compat import device_by_identifier, via_device
from .i18n import localize
from .plant_inputs import components, describe, existing_tracker
from .system_components import (
    INVALID_TYPE_ID,
    ComponentTable,
    is_gateway_row,
    load_table,
)

if TYPE_CHECKING:
    from .platform_scope import DiscoveryLifecycle

_LOGGER = logging.getLogger(__name__)

OPTION: Final = "component_devices"
SEGMENT: Final = "component"
_DATA: Final = f"{DOMAIN}_component_devices"
_IDENTIFIER: Final = re.compile(
    r"(?P<gateway>.+?):component:(?P<place>[0-9]{1,6}):(?P<type>[0-9]{1,6})"
)
# C8: device names never carry the brand (the manufacturer field does).
_BRAND_WORDS: Final = re.compile(r"\b(?:Bosch|Buderus)\b[\s/]*", re.IGNORECASE)
_REMOTE: Final = re.compile(r"remote_hc([1-4])(?:_legacy)?")

type ComponentKey = tuple[int, int]


@dataclass(frozen=True, slots=True)
class ComponentDevice:
    """What the device of one component shows (place and type number)."""

    place: int
    type_id: int
    count: int
    place_key: str | None
    type_names: tuple[str, ...]
    product_name: str | None
    version: str | None
    versions_differ: bool
    role: str | None
    basis: str | None

    @property
    def model(self) -> str | None:
        return " / ".join(self.type_names) or None


def enabled(entry: ConfigEntry[Any]) -> bool:
    """The option is on (off when missing)."""
    return entry.options.get(OPTION) is True


def identifier(gateway_identifier: str, place: int, type_id: int) -> str:
    return f"{gateway_identifier}:{SEGMENT}:{place}:{type_id}"


def parse_identifier(value: object) -> tuple[str, int, int] | None:
    """Gateway, place and type number of a component device's identifier."""
    if not isinstance(value, str):
        return None
    match = _IDENTIFIER.fullmatch(value)
    if match is None:
        return None
    return match["gateway"], int(match["place"]), int(match["type"])


def component_identifier(device: dr.DeviceEntry) -> tuple[str, int, int] | None:
    """Gateway, place and type number of a component device, else None.

    A device counts only when its one identifier of this integration is a
    component identifier, so a gateway's or any other device never does.
    """
    ours = [value for domain, value in device.identifiers if domain == DOMAIN]
    if len(ours) != 1:
        return None
    return parse_identifier(ours[0])


def software_version(value: str | None) -> str | None:
    """The whole software version a component reports, checked again as in
    the component list (``clean_version``), else None."""
    return clean_version(value)


def device_components(
    rows: Iterable[ComponentRow], table: ComponentTable | None = None
) -> dict[ComponentKey, ComponentDevice]:
    """The devices of a component list, by place and type number.

    Only place and type numbers decide which devices exist; the table only
    names them.
    """
    table = load_table() if table is None else table
    grouped: dict[ComponentKey, list[ComponentRow]] = {}
    for row in rows:
        if (
            row.tok is None
            or row.type_id is None
            or row.type_id == INVALID_TYPE_ID
            or is_gateway_row(row.type_id, row.tok)
        ):
            continue
        grouped.setdefault((row.tok, row.type_id), []).append(row)
    found: dict[ComponentKey, ComponentDevice] = {}
    for (place, type_id), items in sorted(grouped.items()):
        description = describe(items[0], table)
        kind = table.component(type_id) if table is not None else None
        names = (kind.name, *kind.aliases) if kind is not None and kind.name else ()
        versions = {software_version(item.version) for item in items}
        products = {item.product_name for item in items if item.product_name}
        found[(place, type_id)] = ComponentDevice(
            place=place,
            type_id=type_id,
            count=len(items),
            place_key=description.place,
            type_names=names,
            product_name=next(iter(products)) if len(products) == 1 else None,
            version=next(iter(versions)) if len(versions) == 1 else None,
            versions_differ=len(versions) > 1,
            role=description.role,
            basis=description.basis,
        )
    return found


def _place_labels(language: str) -> dict[str, str]:
    return {
        "heat_generator_control": localize(
            language, "Wärmeerzeuger-Steuerung", "Heat generator control unit"
        ),
        "basic_controller": localize(language, "Basisregler", "Basic controller"),
        "system_controller": localize(language, "Systemregler", "System controller"),
        "pool": localize(language, "Poolmodul", "Pool module"),
        "remote": localize(
            language, "Fernbedienung Heizkreis", "Remote control heating circuit"
        ),
        "fresh_water": localize(language, "Frischwasserstation", "Fresh water station"),
        "solar": localize(language, "Solarmodul", "Solar module"),
        "radio": localize(language, "Funkmodul", "Radio module"),
        "ventilation": localize(language, "Lüftungsgerät", "Ventilation unit"),
        "hybrid_manager": localize(language, "Hybrid-Manager", "Hybrid manager"),
    }


def place_label(place_key: str | None, language: str) -> str:
    """The name of a place in the server's language; "Component" for a
    place the table does not know."""
    labels = _place_labels(language)
    if place_key is not None and (match := _REMOTE.fullmatch(place_key)):
        return f"{labels['remote']} {match.group(1)}"
    label = labels.get(place_key or "")
    return label or localize(language, "Komponente", "Component")


def device_name(component: ComponentDevice, language: str) -> str:
    """The name of a new component device."""
    if component.product_name is not None:
        product = _BRAND_WORDS.sub("", component.product_name).strip(" /-")
        if product:
            return product
    shown = component.model or str(component.type_id)
    return f"{place_label(component.place_key, language)} {shown}"


# ------------------------------------------------------------- lifecycle


class ComponentDevices:
    """The component devices of one gateway of an entry."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry[Any], gateway: str
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.gateway = gateway
        self.coordinator: Any = None
        # Components that got their device (or had one) in this run.
        self._handled: set[ComponentKey] = set()
        self._linked = False
        self._last: tuple[object, str] | None = None
        self._failed = False

    def attach(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        self._last = None

    def reset(self) -> None:
        """As at a start: every listed component gets its device again."""
        self._handled.clear()
        self._linked = False
        self._last = None

    def listed(self) -> dict[ComponentKey, ComponentDevice]:
        """The components of the list shown now."""
        shown = components(self.coordinator) if self.coordinator is not None else None
        return device_components(shown.rows) if shown is not None else {}

    @callback
    def update(self) -> None:
        """Follow an update of the coordinator; never disturbs it."""
        try:
            self.sync()
        except Exception:  # the devices must never stop an update
            if not self._failed:
                self._failed = True
                _LOGGER.debug("Component devices not updated", exc_info=True)

    @callback
    def sync(self) -> int:
        """Create the devices of new components; the number created."""
        coordinator = self.coordinator
        if (
            coordinator is None
            or not enabled(self.entry)
            or getattr(coordinator, "read_only_view", False) is True
        ):
            return 0
        registry = dr.async_get(self.hass)
        entry_id = self.entry.entry_id
        parent = device_by_identifier(registry, (DOMAIN, self.gateway), entry_id)
        if parent is None:
            return 0  # the gateway's device comes first
        if not self._linked:
            self._link(registry, parent)
        shown = components(coordinator)
        if shown is None:
            return 0
        if self._last is not None and self._last == (shown.rows, parent.id):
            return 0
        language = self.hass.config.language
        created = 0
        complete = True
        for key, item in device_components(shown.rows).items():
            try:
                created += self._sync_one(registry, key, item, parent, language)
            except Exception:  # one component never stops the others
                complete = False
                _LOGGER.debug("Component device not updated", exc_info=True)
        # Done for this list only when every component was handled; else the
        # next update tries the failed ones again.
        self._last = (shown.rows, parent.id) if complete else None
        return created

    def _sync_one(
        self,
        registry: dr.DeviceRegistry,
        key: ComponentKey,
        item: ComponentDevice,
        parent: dr.DeviceEntry,
        language: str,
    ) -> int:
        entry_id = self.entry.entry_id
        wanted = (DOMAIN, identifier(self.gateway, *key))
        device = device_by_identifier(registry, wanted, entry_id)
        if device is not None:
            self._handled.add(key)
            self._refresh(registry, device, item, parent)
            return 0
        if key in self._handled:
            return 0  # deleted by the user: back at the next start
        registry.async_get_or_create(
            config_entry_id=entry_id,
            identifiers={wanted},
            manufacturer=parent.manufacturer,
            model=item.model,
            model_id=str(item.type_id),
            name=device_name(item, language),
            sw_version=item.version,
            **via_device(registry, (DOMAIN, self.gateway), entry_id),
        )
        self._handled.add(key)
        return 1

    def _link(self, registry: dr.DeviceRegistry, parent: dr.DeviceEntry) -> None:
        """Connect devices that were created without their gateway's device."""
        for device in tuple(
            dr.async_entries_for_config_entry(registry, self.entry.entry_id)
        ):
            parsed = component_identifier(device)
            if (
                parsed is not None
                and parsed[0] == self.gateway
                and device.via_device_id != parent.id
            ):
                registry.async_update_device(device.id, via_device_id=parent.id)
        self._linked = True

    def _refresh(
        self,
        registry: dr.DeviceRegistry,
        device: dr.DeviceEntry,
        item: ComponentDevice,
        parent: dr.DeviceEntry,
    ) -> None:
        changes: dict[str, Any] = {}
        if item.version is not None and device.sw_version != item.version:
            changes["sw_version"] = item.version
        elif item.versions_differ and device.sw_version is not None:
            changes["sw_version"] = None
        if device.model != item.model:
            changes["model"] = item.model
        if device.via_device_id != parent.id:
            changes["via_device_id"] = parent.id
        if changes:
            registry.async_update_device(device.id, **changes)


def _managers(hass: HomeAssistant) -> dict[tuple[str, str], ComponentDevices]:
    managers: dict[tuple[str, str], ComponentDevices] = hass.data.setdefault(_DATA, {})
    return managers


def managers_of(hass: HomeAssistant, entry_id: str) -> list[ComponentDevices]:
    return [
        manager
        for (owner, _), manager in sorted(_managers(hass).items())
        if owner == entry_id
    ]


@callback
def track_component_devices(
    scope: DiscoveryLifecycle, coordinator: Any
) -> ComponentDevices | None:
    """Follow the component devices of a gateway while ``scope`` runs.

    Started in the sensor platform setup after the plant model. Gateways
    without a plant model (water softeners) get none.
    """
    entry = getattr(coordinator, "config_entry", None)
    tracker = existing_tracker(coordinator) if entry is not None else None
    if entry is None or tracker is None:
        return None
    hass = coordinator.hass
    managers = _managers(hass)
    key = (entry.entry_id, coordinator.plant_model.gateway_device.identifier)
    manager = managers.get(key)
    if manager is None:
        manager = managers[key] = ComponentDevices(hass, entry, key[1])

        def forget() -> None:
            if managers.get(key) is manager:
                del managers[key]

        entry.async_on_unload(forget)
    manager.attach(coordinator)
    scope.async_on_unload(coordinator.async_add_listener(manager.update))

    async def after_load() -> None:
        # The plant model reads its stored row first; then the start.
        try:
            await tracker.async_load()
        finally:
            manager.update()

    entry.async_create_background_task(
        hass, after_load(), "component devices start", eager_start=True
    )
    return manager


# ------------------------------------------------------- entry-wide work


def _component_devices(
    hass: HomeAssistant, entry_id: str
) -> list[tuple[dr.DeviceEntry, tuple[str, int, int]]]:
    registry = dr.async_get(hass)
    return [
        (device, parsed)
        for device in dr.async_entries_for_config_entry(registry, entry_id)
        if (parsed := component_identifier(device)) is not None
    ]


@callback
def enable(hass: HomeAssistant, entry: ConfigEntry[Any]) -> int:
    """The option was switched on: every listed component gets its device
    now; the number created."""
    created = 0
    for manager in managers_of(hass, entry.entry_id):
        manager.reset()
        try:
            created += manager.sync()
        except Exception:  # the options are saved; the next update tries again
            _LOGGER.debug("Component devices not created", exc_info=True)
    return created


@callback
def remove_all(hass: HomeAssistant, entry: ConfigEntry[Any]) -> int:
    """The option was switched off: remove every component device of the
    entry, also customised ones; the number removed."""
    registry = dr.async_get(hass)
    found = _component_devices(hass, entry.entry_id)
    for device, _ in found:
        registry.async_remove_device(device.id)
    for manager in managers_of(hass, entry.entry_id):
        manager.reset()
    return len(found)


@callback
def remove_for_other_gateways(
    hass: HomeAssistant, entry: ConfigEntry[Any], gateways: Iterable[str]
) -> int:
    """Remove the component devices of gateways that belong to no stored
    system of a managed entry (also after a gateway exchange); the number
    removed."""
    keep = frozenset(gateways)
    registry = dr.async_get(hass)
    found = [
        device
        for device, parsed in _component_devices(hass, entry.entry_id)
        if parsed[0] not in keep
    ]
    for device in found:
        registry.async_remove_device(device.id)
    return len(found)


def offered(hass: HomeAssistant, entry: ConfigEntry[Any]) -> bool:
    """The options offer the step: the option is on, or a system of the
    entry lists components."""
    if enabled(entry):
        return True
    try:
        return any(manager.listed() for manager in managers_of(hass, entry.entry_id))
    except Exception:  # the options menu never fails on the plant model
        _LOGGER.debug("Component devices not offered", exc_info=True)
        return False


def listing(hass: HomeAssistant, entry: ConfigEntry[Any]) -> str:
    """The components of the entry's systems as lines for the dialog, in
    the server's language (as the device names)."""
    from .plant_labels import plant_labels

    language = hass.config.language
    managers = [
        manager for manager in managers_of(hass, entry.entry_id) if manager.listed()
    ]
    labels = plant_labels(
        hass,
        entry.entry_id,
        {manager.gateway: manager.gateway for manager in managers},
    )
    parts: list[str] = []
    for manager in managers:
        lines = [
            f"- {device_name(item, language)}" for item in manager.listed().values()
        ]
        if len(managers) > 1:
            parts.append(f"**{labels[manager.gateway]}**\n" + "\n".join(lines))
        else:
            parts.append("\n".join(lines))
    return "\n\n".join(parts) or localize(
        language, "Keine Komponenten gemeldet", "No components reported"
    )


# ------------------------------------------------------------ diagnostics


def diagnostics(coordinator: Any) -> dict[str, Any] | None:
    """``plant.component_devices``: whether the option is on and how many
    component devices the gateway has."""
    entry = getattr(coordinator, "config_entry", None)
    hass = getattr(coordinator, "hass", None)
    if entry is None or hass is None:
        return None
    try:
        gateway = coordinator.plant_model.gateway_device.identifier
        count = sum(
            parsed[0] == gateway
            for _, parsed in _component_devices(hass, entry.entry_id)
        )
    except Exception:  # the report must stay available
        _LOGGER.debug("Component devices not reportable", exc_info=True)
        return None
    return {"enabled": enabled(entry), "count": count}


def device_report(
    hass: HomeAssistant, entry: ConfigEntry[Any], device: dr.DeviceEntry
) -> dict[str, Any] | None:
    """The block ``component`` of a component device's diagnostics; None
    for any other device. Never a serial number or an identifier."""
    parsed = component_identifier(device)
    if parsed is None:
        return None
    gateway, place, type_id = parsed
    manager = _managers(hass).get((entry.entry_id, gateway))
    listed: Mapping[ComponentKey, ComponentDevice] = (
        manager.listed() if manager is not None else {}
    )
    item = listed.get((place, type_id))
    if item is not None:
        place_key, model, role, basis = (
            item.place_key,
            item.model,
            item.role,
            item.basis,
        )
    else:
        # Not listed now: what the table says about the place and type.
        table = load_table()
        description = describe(ComponentRow(type_id, place), table)
        kind = table.component(type_id) if table is not None else None
        place_key, role, basis = description.place, description.role, description.basis
        model = (
            " / ".join((kind.name, *kind.aliases))
            if kind is not None and kind.name
            else None
        )
    return {
        "place": place_key,
        "place_number": place,
        "type_number": type_id,
        "type_name": model,
        "role": role,
        "basis": basis,
        "version": item.version if item is not None else None,
        "reported": item is not None,
        "count": item.count if item is not None else 0,
    }
