"""Shared setup of the W2 tests: the CT200 tree with the absence fixture."""

from __future__ import annotations

import json
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.helpers.rrc_cloud import FIXTURES, Clock, RrcCloud, cloud_patches, load_tree
from tests.test_entry import _entry

RRC_GATEWAY = Gateway("gateway-one", device_type="rrc2")
EMS_GATEWAY = Gateway("gateway-one", device_type="k40")
BOOST = "/heatingCircuits/hc1/boostShortcut"
AWAY = "/system/awayMode/enabled"
AWAY_TEMPERATURE = "/system/awayMode/temperature"
FIREPLACE = "/system/fireplace/enabled"
LISTS = ("/events/list1", "/events/list2", "/events/list3")


def variants() -> dict[str, Any]:
    return json.loads((FIXTURES / "rrc" / "absence.json").read_text("utf-8"))[
        "variants"
    ]


def _link(tree: dict[str, Resource], path: str) -> None:
    """Name ``path`` in its parent's references (adding the parent)."""
    parent = path.rsplit("/", 1)[0]
    if not parent:
        return
    current = tree.get(parent)
    references = [item.path for item in current.references] if current else []
    if path not in references:
        tree[parent] = parse_resource(
            {
                "id": parent,
                "type": "refEnum",
                "references": [{"id": item} for item in (*references, path)],
            }
        )
    if current is None:
        _link(tree, parent)


def absence_tree() -> dict[str, Resource]:
    """``ct200-shapes.json`` with ``absence.json`` joined below its roots."""
    tree = load_tree("rrc/ct200-shapes.json")
    added = load_tree("rrc/absence.json")
    tree.update(added)
    for path in added:
        if path != "/events":
            _link(tree, path)
    return tree


def wire(path: str, **fields: Any) -> Resource:
    return parse_resource({"id": path, "recordable": 0, **fields})


def set_value(cloud: RrcCloud, path: str, value: Any) -> None:
    cloud.tree[path] = parse_resource({**_raw(cloud.tree[path]), "value": value})


def _raw(resource: Resource) -> dict[str, Any]:
    metadata = resource.metadata
    raw: dict[str, Any] = {
        "id": resource.path,
        "type": metadata.resource_type,
        "writeable": int(metadata.writable),
        "recordable": 0,
    }
    if metadata.used is not None:
        raw["used"] = "true" if metadata.used else "false"
    if metadata.available is not None:
        raw["available"] = "true" if metadata.available else "false"
    return raw


async def setup(hass: HomeAssistant, cloud: RrcCloud, gateway: Gateway = RRC_GATEWAY):
    clock = Clock()
    entry = _entry(hass)
    stack = cloud_patches(cloud, clock, gateway)
    stack.__enter__()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinators[0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    coordinator._write_service._sleep = sleep
    coordinator.rrc_struct_writes._sleep = sleep
    coordinator.rrc_struct_writes._clock = clock
    return entry, coordinator, clock, sleeps, stack


async def teardown(hass: HomeAssistant, entry, stack) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stack.close()


def entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


class EventCloud(RrcCloud):
    """The fake cloud with the controller's event behaviour: a POST adds an
    entry with a new ID to ``/events/list1``, a PUT or DELETE of
    ``/events/evN`` changes or removes the entry in its list. A list shows a
    change late with ``adopt_after[list]``."""

    def __init__(self, resources: dict[str, Resource] | None = None, **kwargs: Any):
        super().__init__(resources or absence_tree(), **kwargs)
        self.post_handlers["/events"] = self._create
        self.next_id = 10
        # An error the next POSTs answer with, after recording them.
        self.post_error: Exception | None = None

    async def post_resource(self, gateway_id, path, value, **kwargs):
        if self.post_error is not None:
            self.calls["POST", path] += 1
            self.writes.append(("POST", path, value))
            raise self.post_error
        return await super().post_resource(gateway_id, path, value, **kwargs)

    def _list_of(self, identifier: int) -> str | None:
        for path in LISTS:
            resource = self.tree.get(path)
            if resource is not None and any(
                isinstance(item, dict) and item.get("id") == identifier
                for item in resource.value or ()
            ):
                return path
        return None

    def _listed(self, path: str) -> list[Any]:
        pending = self._pending.get(path)
        if pending is not None:
            return list(pending.value)
        return list(self.tree[path].value or [])

    def _create(self, value: Any) -> None:
        entry = {**value[0], "id": self.next_id}
        self.next_id += 1
        self._settle("/events/list1", [*self._listed("/events/list1"), entry])

    def _settle(self, path: str, value: Any) -> None:
        match = re.fullmatch(r"/events/ev(\d+)", path)
        if match is None:
            super()._settle(path, value)
            return
        if path in self.ignore_writes:
            return
        identifier = int(match.group(1))
        source = self._list_of(identifier)
        if source is None:
            return
        entries = [
            {**value[0], "id": identifier}
            if isinstance(item, dict) and item.get("id") == identifier
            else item
            for item in self._listed(source)
        ]
        super()._settle(source, entries)

    async def delete_resource(self, gateway_id, path, **kwargs):
        match = re.fullmatch(r"/events/ev(\d+)", path)
        if match is not None and path not in self.delete_handlers:
            identifier = int(match.group(1))

            def remove() -> None:
                source = self._list_of(identifier)
                if source is not None:
                    super(EventCloud, self)._settle(
                        source,
                        [
                            item
                            for item in self._listed(source)
                            if not (
                                isinstance(item, dict) and item.get("id") == identifier
                            )
                        ],
                    )

            self.delete_handlers[path] = remove
        await super().delete_resource(gateway_id, path, **kwargs)
