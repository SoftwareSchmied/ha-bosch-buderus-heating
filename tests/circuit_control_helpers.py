"""Fixtures and a fake cloud for the heating circuit settings tests.

The fixtures in ``fixtures/contracts`` list a base and variants that replace
or remove resources. The fake cloud answers the coordinator's guard reads
(bulk), single reads and the one write from its own state, counts every
request and can be told to answer differently (a refused or failed write,
a read-back that never shows the value, a change on the gateway).

Everything is synthetic; nothing here confirms behaviour on a real device.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.number import (
    BoschBuderusNumber,
    build_number_descriptions,
)
from custom_components.bosch_buderus_heating.pointt import Gateway, Resource
from custom_components.bosch_buderus_heating.pointt.models import BatchItemResult
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.runtime import BoschBuderusRuntimeData
from custom_components.bosch_buderus_heating.select import (
    BoschBuderusSelect,
    build_select_descriptions,
)
from custom_components.bosch_buderus_heating.writes import WriteService
from tests.ha_test_helpers import MockConfigEntry

CONTRACTS = Path(__file__).parent / "fixtures" / "contracts"
ROOT = "/heatingCircuits/hc1"
CONTROL_TYPE = ROOT + "/controlType"
SWITCH_MODE = ROOT + "/suWiSwitchMode"
THRESHOLD = ROOT + "/suWiThreshold"
COOLING_SETPOINT = ROOT + "/cooling/roomTempSetpoint"
COOLING_THRESHOLD = ROOT + "/cooling/outdoorThreshold"
COMFORT = ROOT + "/temperatureLevels/comfort2"
ECO = ROOT + "/temperatureLevels/eco"
AWAY = ROOT + "/awayTemperature"
AWAY_LEVEL = ROOT + "/temperatureLevels/awayTemperature"
COOLING_LEVEL = ROOT + "/cooling/temperatureLevels/on"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def variant(name: str, key: str) -> dict[str, Resource]:
    """The resources of one variant of a fixture, by path."""
    data = fixture(name)
    raw = {item["id"]: item for item in data["base"]}
    if key in data["variants"]:
        change = data["variants"][key]
        for item in change.get("set", ()):
            raw[item["id"]] = item
        for path in change.get("remove", ()):
            raw.pop(path, None)
    return {path: parse_resource(item) for path, item in raw.items()}


def with_flags(resource: Resource, **flags: object) -> Resource:
    return replace(resource, metadata=replace(resource.metadata, **flags))


class Cloud:
    """Answer reads from its own state and count every request."""

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        self.coordinator = coordinator
        self.state = {
            path: snapshot.resource for path, snapshot in coordinator.data.items()
        }
        self.puts: list[tuple[str, object]] = []
        self.bulk_calls: list[tuple[str, ...]] = []
        self.gets: list[str] = []
        # Called instead of storing the written value: (path, value) -> None.
        self.on_put: Callable[[str, object], None] | None = None
        self.put_error: Exception | None = None
        self.readback_error: Exception | None = None
        # A read-back keeps showing this value instead of the written one.
        self.stuck: dict[str, object] = {}
        client = coordinator.client
        client.get_resources_bulk.side_effect = self.bulk
        client.get_resource.side_effect = self.get
        client.put_resource_value.side_effect = self.put
        coordinator._write_service = WriteService(client, sleep=AsyncMock())

    async def bulk(self, gateway: str, paths: Any, **_kwargs: Any) -> tuple:
        self.bulk_calls.append(tuple(paths))
        return tuple(
            BatchItemResult(gateway, path, 200, resource=self.state[path])
            for path in paths
        )

    async def get(self, gateway: str, path: str, **_kwargs: Any) -> Resource:
        self.gets.append(path)
        if self.puts and self.readback_error is not None:
            raise self.readback_error
        resource = self.state[path]
        if path in self.stuck:
            return replace(resource, value=self.stuck[path])
        return resource

    async def put(self, gateway: str, path: str, value: object) -> None:
        self.puts.append((path, value))
        if self.put_error is not None:
            raise self.put_error
        if self.on_put is not None:
            self.on_put(path, value)
            return
        self.state[path] = replace(self.state[path], value=value)

    def change(self, path: str, **fields: Any) -> None:
        """Change a resource on the gateway (not yet seen by the coordinator)."""
        self.state[path] = replace(self.state[path], **fields)


def coordinator_for(
    hass: HomeAssistant,
    resources: dict[str, Resource],
    *,
    device_type: str = "icom",
    checked: bool = True,
) -> BoschBuderusDataUpdateCoordinator:
    """A coordinator with these readings; ``checked``: a finished discovery
    settled that the gateway is no room controller."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"brand": "buderus", "gateway_ids": ["gateway"]}
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, client, Gateway("gateway", device_type=device_type), entry
    )
    coordinator.update_interval = None
    coordinator.resources = dict(resources)
    coordinator.data = {
        path: ResourceSnapshot(resource, True, datetime.now(UTC))
        for path, resource in resources.items()
    }
    coordinator.last_update_success = True
    coordinator.room_controller_checked = checked
    entry.runtime_data = BoschBuderusRuntimeData(
        client, AsyncMock(), (coordinator.gateway,), (coordinator,)
    )
    return coordinator


def put_reading(
    coordinator: BoschBuderusDataUpdateCoordinator, resource: Resource
) -> None:
    coordinator.data[resource.path] = ResourceSnapshot(
        resource, True, datetime.now(UTC)
    )
    coordinator.resources[resource.path] = resource


def number_entity(
    hass: HomeAssistant, coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> BoschBuderusNumber:
    descriptions = {
        item.resource_path: item
        for item in build_number_descriptions(coordinator.resources)
    }
    entity = BoschBuderusNumber(coordinator, descriptions[path])
    entity.hass = hass
    return entity


def select_entity(
    hass: HomeAssistant, coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> BoschBuderusSelect:
    descriptions = {
        item.resource_path: item
        for item in build_select_descriptions(coordinator.resources)
    }
    entity = BoschBuderusSelect(coordinator, descriptions[path])
    entity.hass = hass
    return entity


def number_paths(resources: dict[str, Resource], context: Any = None) -> set[str]:
    return {
        item.resource_path for item in build_number_descriptions(resources, context)
    }


def select_paths(resources: dict[str, Resource], context: Any = None) -> set[str]:
    return {
        item.resource_path for item in build_select_descriptions(resources, context)
    }
