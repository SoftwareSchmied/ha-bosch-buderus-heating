"""Helpers for the further program kinds: fixtures and a counting gateway."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from custom_components.bosch_buderus_heating.pointt import BatchItemResult, Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.test_dynamic_controls import snapshots

FIXTURES = Path(__file__).parent / "fixtures/contracts"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def payloads(name: str) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in load(name)["resources"]}


def resource(item: dict[str, Any]) -> Resource:
    return parse_resource(copy.deepcopy(item), path=item["id"])


class CountingGateway:
    """The gateway state behind a mocked client; counts every request."""

    def __init__(self, coordinator, client, items: dict[str, dict[str, Any]]):
        self.coordinator = coordinator
        self.client = client
        self.payloads = copy.deepcopy(items)
        self.raw_puts: list[tuple[str, Any]] = []
        self.object_puts: list[tuple[str, Any]] = []
        self.value_puts: list[tuple[str, Any]] = []
        self.gets: list[str] = []
        # Applied on the next write instead of the request (a gateway that
        # ignores or changes the week).
        self.ignore_writes = False
        self.refresh()
        client.get_resource.side_effect = self.get
        client.put_resource_raw_json.side_effect = self.put_raw
        client.put_resource_object.side_effect = self.put_object
        client.put_resource_value.side_effect = self.put_value
        client.get_resources_bulk.side_effect = self.bulk

    def refresh(self) -> None:
        self.coordinator.resources = {
            path: resource(item) for path, item in self.payloads.items()
        }
        self.coordinator.data = snapshots(*self.coordinator.resources.values())
        self.coordinator.last_update_success = True

    def read(self, path: str) -> Resource:
        return resource(self.payloads[path])

    async def get(self, gateway: str, path: str, **_kwargs: Any) -> Resource:
        self.gets.append(path)
        return self.read(path)

    async def bulk(self, gateway: str, paths, **_kwargs: Any):
        return tuple(
            BatchItemResult(gateway, path, 200, resource=self.read(path))
            for path in paths
        )

    async def put_raw(self, gateway: str, path: str, body: list[Any]) -> None:
        self.raw_puts.append((path, copy.deepcopy(body)))
        if not self.ignore_writes:
            self.payloads[path]["switchPoints"] = copy.deepcopy(body)

    async def put_object(self, gateway: str, path: str, body: dict[str, Any]) -> None:
        self.object_puts.append((path, copy.deepcopy(body)))
        if not self.ignore_writes:
            self.payloads[path]["switchPoints"] = copy.deepcopy(body["switchPoints"])

    async def put_value(self, gateway: str, path: str, value: Any) -> None:
        self.value_puts.append((path, copy.deepcopy(value)))
        if not self.ignore_writes:
            self.payloads[path]["value"] = copy.deepcopy(value)

    @property
    def puts(self) -> int:
        return len(self.raw_puts) + len(self.object_puts) + len(self.value_puts)

    def change(self, path: str, **fields: Any) -> None:
        """A change made elsewhere, outside Home Assistant."""
        self.payloads[path].update(copy.deepcopy(fields))
