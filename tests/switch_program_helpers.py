"""Z1/Z2 test helpers: a synthetic gateway with programs A/B and hot water A."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from custom_components.bosch_buderus_heating.pointt import BatchItemResult, Resource
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.test_dynamic_controls import snapshots

FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures/contracts/connectkey/switch-programs-ab.json"
    ).read_text(encoding="utf-8")
)
PAYLOADS: dict[str, dict[str, Any]] = {
    item["id"]: item for item in FIXTURE["resources"]
}
HC = "/heatingCircuits/hc1"
ACTIVE = f"{HC}/activeSwitchProgram"
MODE = f"{HC}/switchProgramMode"
PROGRAM_A = f"{HC}/switchPrograms/A"
PROGRAM_B = f"{HC}/switchPrograms/B"
DHW = "/dhwCircuits/dhw1/switchPrograms/A"


def payload(path: str) -> dict[str, Any]:
    return copy.deepcopy(PAYLOADS[path])


def resource(item: dict[str, Any]) -> Resource:
    return parse_resource(item, path=item["id"])


class Gateway:
    """The gateway state behind a mocked client; counts every write."""

    def __init__(self, coordinator, client, **changes: dict[str, Any] | None):
        self.coordinator = coordinator
        self.client = client
        self.payloads = {path: payload(path) for path in PAYLOADS}
        for path, item in changes.items():
            if item is None:
                self.payloads.pop(path, None)
            else:
                self.payloads[path] = item
        self.raw_puts: list[tuple[str, list[Any]]] = []
        self.value_puts: list[tuple[str, Any]] = []
        self.gets: list[str] = []
        coordinator.resources = {
            path: resource(item) for path, item in self.payloads.items()
        }
        coordinator.data = snapshots(*coordinator.resources.values())
        coordinator.last_update_success = True
        client.get_resource.side_effect = self.get
        client.put_resource_raw_json.side_effect = self.put_raw
        client.put_resource_value.side_effect = self.put_value
        client.get_resources_bulk.side_effect = self.bulk

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
        self.payloads[path]["switchPoints"] = copy.deepcopy(body)

    async def put_value(self, gateway: str, path: str, value: Any) -> None:
        self.value_puts.append((path, value))
        self.payloads[path]["value"] = value

    def change(self, path: str, **fields: Any) -> None:
        """A change made elsewhere (for example in the app)."""
        self.payloads[path].update(fields)
