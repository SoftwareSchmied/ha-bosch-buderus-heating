"""A synthetic air conditioner on a PointT gateway for the aircon tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.bosch_buderus_heating.aircon import AirconCoordinator
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.pointt import AuthTokens, Gateway
from custom_components.bosch_buderus_heating.pointt.models import BatchItemResult
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.ha_test_helpers import MockConfigEntry

FIXTURE = Path(__file__).parent / "fixtures/contracts/pointt-rac/airconditioning.json"
GATEWAY = "rac-gateway-A"
B = "/airConditioning"
CREATED: list[AirconCoordinator] = []


def fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class FakeUnit:
    """A stateful unit: bulk and single reads, scalar PUTs, programmable faults."""

    def __init__(self, detection="midea"):
        data = fixture()
        self.resources = copy.deepcopy(data["resources"])
        self.detection = {
            path: {"id": path, "type": "stringValue", "value": value}
            for path, value in data["detection"][detection].items()
        }
        self.status = {}  # path -> item status of bulk/single reads
        self.bulk_calls = []
        self.bulk_kwargs = []
        self.gets = []
        self.puts = []
        self.put_error = None
        self.apply_puts = True
        self.read_back = None  # callable(path) -> payload override or exception
        self.bulk_error = None
        self.backoff = SimpleNamespace(remaining=0.0)

    def payload(self, path):
        if path in self.detection:
            return self.detection[path]
        return self.resources.get(path)

    def set(self, path, **fields):
        self.resources[path].update(fields)

    async def get_resources_bulk(self, gateway_id, paths, **kwargs):
        self.bulk_calls.append(tuple(paths))
        self.bulk_kwargs.append(kwargs)
        if self.bulk_error is not None:
            raise self.bulk_error
        items = []
        for path in paths:
            status = self.status.get(path)
            payload = self.payload(path)
            if status is not None or payload is None:
                items.append(BatchItemResult(gateway_id, path, status or 404))
            else:
                items.append(
                    BatchItemResult(
                        gateway_id, path, 200, parse_resource(payload, path=path)
                    )
                )
        return tuple(items)

    async def get_resource(self, gateway_id, path, **kwargs):
        self.gets.append(path)
        if self.read_back is not None:
            override = self.read_back(path)
            if isinstance(override, BaseException):
                raise override
            if override is not None:
                return parse_resource(override, path=path)
        return parse_resource(self.payload(path), path=path)

    async def put_resource_value(self, gateway_id, path, value):
        self.puts.append((path, value))
        if self.put_error is not None:
            raise self.put_error
        if self.apply_puts:
            self.resources[path]["value"] = value


def make_entry(hass, *, friendly=False):
    config = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            "brand": "buderus",
            "gateway_ids": [GATEWAY],
            **tokens_to_data(AuthTokens("synthetic-access", "synthetic-refresh", 4e9)),
        },
        options={"polling_profile": "cloud_friendly" if friendly else "standard"},
    )
    config.add_to_hass(hass)
    return config


class Sleeps:
    """Record read-back waits instead of sleeping."""

    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)


async def make_coordinator(hass, unit=None, *, refresh=True, friendly=False):
    unit = unit or FakeUnit()
    coordinator = AirconCoordinator(
        hass,
        unit,
        Gateway(GATEWAY, device_type="rac"),
        make_entry(hass, friendly=friendly),
    )
    CREATED.append(coordinator)
    sleeps = Sleeps()
    coordinator._write_service._sleep = sleeps
    if refresh:
        await coordinator.async_refresh()
    return coordinator, unit, sleeps


@pytest.fixture(autouse=True)
async def aircon_cleanup():
    """Stop the refresh a write requests, so no timer outlives a test."""
    yield
    while CREATED:
        await CREATED.pop().async_shutdown()
