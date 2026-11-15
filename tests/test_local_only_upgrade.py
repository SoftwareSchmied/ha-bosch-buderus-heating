"""Upgrade from 0.9.1: reviewed local-only readings the cloud offers later.

Some gateways refuse the directory ``/heatSources/hs1/refrigerant`` (HTTP
403). Since the children of a refused directory are read directly, the cloud
offers the compressor speed and the refrigerant circuit status there, which
0.9.1 showed as local-only entities. Their entities must stay exactly as
they were: unique ID, entity ID, name and translation, classes, states and
enablement; the cloud reading becomes their second source.

Runs the managed entry with its real platforms, the real PointT client
against a synthetic cloud and published local readings.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest
from aiohttp import web
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.gateway_identity import GatewayNamespace
from custom_components.bosch_buderus_heating.local_only import definition_for
from custom_components.bosch_buderus_heating.local_only_sensor import (
    BoschBuderusLocalOnlySensor,
    local_only_unique_id,
)
from tests.test_local_api import read
from tests.test_local_backed import GATEWAY, ReadingBackend
from tests.test_managed_platforms import native
from tests.test_managed_pointt import eventually, running, source_for
from tests.test_source_isolation import paired_target

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")

HS = "/heatSources/hs1"
REFRIGERANT = HS + "/refrigerant"
SPEED = REFRIGERANT + "/compressorActualSpeed"
STATUS = REFRIGERANT + "/status"
SPEED_ID = local_only_unique_id(GATEWAY, SPEED)
STATUS_ID = local_only_unique_id(GATEWAY, STATUS)
# What the cloud's own sensors of these paths would be called.
CLOUD_SPEED_ID = f"{GATEWAY}:heatSources:hs1:refrigerant:compressorActualSpeed"
CLOUD_STATUS_ID = f"{GATEWAY}:heatSources:hs1:refrigerant:status"
# The states the cloud declares for the status (fewer than the local ones).
CLOUD_STATES = [
    "off",
    "heating",
    "cooling",
    "defrost",
    "alarm",
    "dhw",
    "pool",
    "pool_heat",
]
LOCAL_STATES = list(definition_for(STATUS).options)


@pytest.fixture(autouse=True)
def loaded_integration(hass):
    hass.config.components.add(DOMAIN)


class RefusedRefrigerantBackend(ReadingBackend):
    """The #130 shape: the refrigerant directory answers 403, its children 200."""

    speed = 38
    refrigerant_status = "heating"

    def resource(self, gateway, path):
        if path == "/heatSources":
            return {"references": [{"id": HS}]}
        if path == HS:
            return {"references": [{"id": REFRIGERANT}]}
        if path == SPEED:
            return {
                "id": SPEED,
                "type": "integerValue",
                "value": self.speed,
                "unitOfMeasure": "%",
                "writeable": False,
            }
        if path == STATUS:
            return {
                "id": STATUS,
                "type": "stringValue",
                "value": self.refrigerant_status,
                "allowedValues": CLOUD_STATES,
                "writeable": False,
            }
        return super().resource(gateway, path)

    async def bulk(self, request):
        body = (await request.json())[0]
        gateway, paths = body["gatewayId"], body["resourcePaths"]
        self.calls.append(("bulk", gateway, tuple(paths)))
        await self.wait(gateway)
        items = []
        for path in paths:
            resource = None if path == REFRIGERANT else self.resource(gateway, path)
            status = 403 if path == REFRIGERANT else 200 if resource else 404
            items.append(
                {
                    "resourcePath": path,
                    "serverStatus": 200,
                    "gatewayResponse": {"status": status, "payload": resource},
                }
            )
        return web.json_response([{"gatewayId": gateway, "resourcePaths": items}])

    async def scalar(self, request):
        path = "/" + request.match_info["path"]
        if path == REFRIGERANT:
            self.calls.append((request.method, request.match_info["gateway"], path))
            return web.Response(status=403)
        return await super().scalar(request)


def configure_for(hass, *, local_values: bool):
    """The 0.9.1 state: a cloud entry with a local access and the two
    local-only entities as 0.9.1 registered them."""

    def configure(data, options):
        target, _ = paired_target(data, options)
        options["targets"][target]["local_values"] = local_values
        options["bindings"]["local"]["enabled"] = True
        (entry,) = hass.config_entries.async_entries(DOMAIN)
        registry = er.async_get(hass)
        speed = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            SPEED_ID,
            config_entry=entry,
            suggested_object_id="heat_pump_compressor_speed",
            original_name="Heat pump Compressor speed",
            capabilities={"state_class": "measurement"},
            unit_of_measurement="%",
            # Disabled by default; this user enabled it (statistics since).
            disabled_by=None,
        )
        registry.async_update_entity_options(
            speed.entity_id, DOMAIN, {"native_unit": "%"}
        )
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            STATUS_ID,
            config_entry=entry,
            suggested_object_id="heat_pump_refrigerant_circuit_status",
            original_name="Heat pump Refrigerant circuit status",
            original_device_class="enum",
            translation_key="refrigerant_status",
            capabilities={"options": LOCAL_STATES},
        )

    return configure


_READ_TIMES = itertools.count(100.0)


def publish_local(owner, speed: int, status: str) -> None:
    runtime = owner.connections
    context = runtime._contexts["local"]
    observations = (
        read(
            {
                "id": SPEED,
                "type": "integerValue",
                "value": speed,
                "unitOfMeasure": "%",
                "writeable": 0,
            },
            observed_monotonic=next(_READ_TIMES),
        ),
        read(
            {
                "id": STATUS,
                "type": "stringValue",
                "value": status,
                "allowedValues": LOCAL_STATES[:9],
                "writeable": 0,
            },
            observed_monotonic=next(_READ_TIMES),
        ),
    )
    runtime._local_states["local"] = SimpleNamespace(
        context=context.identity_context(GatewayNamespace.K40_LOCAL),
        current_observations=observations,
    )
    runtime._notify()


def snapshot(registry: er.EntityRegistry, unique_id: str) -> dict[str, object]:
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    item = registry.async_get(entity_id)
    return {
        "entity_id": item.entity_id,
        "unique_id": item.unique_id,
        "disabled_by": item.disabled_by,
        "hidden_by": item.hidden_by,
        "translation_key": item.translation_key,
        "original_device_class": item.original_device_class,
        "entity_category": item.entity_category,
        "state_class": (item.capabilities or {}).get("state_class"),
        "options": (item.capabilities or {}).get("options"),
    }


@pytest.mark.parametrize("local_values", [False, True])
async def test_upgrade_keeps_the_local_only_entities_of_probed_readings(
    hass, local_values
) -> None:
    registry = er.async_get(hass)
    changes: list[tuple[str, str]] = []

    @callback
    def note(event) -> None:
        if event.data["action"] in ("remove", "create") or "unique_id" in (
            event.data.get("changes") or {}
        ):
            changes.append((event.data["action"], event.data["entity_id"]))

    backend = RefusedRefrigerantBackend()
    async with running(
        hass, backend, configure=configure_for(hass, local_values=local_values)
    ) as (_, _, owner, _):
        before = {key: snapshot(registry, key) for key in (SPEED_ID, STATUS_ID)}
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, note)
        await owner.start_platforms()
        publish_local(owner, 41, "defrost")
        # Discovery reads the refused directory's children directly.
        await eventually(
            lambda: (
                (coordinator := source_for(owner, GATEWAY)) is not None
                and SPEED in coordinator.resources
                and STATUS in coordinator.resources
            )
        )
        coordinator = source_for(owner, GATEWAY)
        await eventually(lambda: native(owner, "sensor", SPEED_ID) is not None)
        await eventually(lambda: native(owner, "sensor", STATUS_ID) is not None)
        speed = native(owner, "sensor", SPEED_ID)
        status = native(owner, "sensor", STATUS_ID)
        # The local-only entities stay, unchanged.
        assert isinstance(speed, BoschBuderusLocalOnlySensor)
        assert isinstance(status, BoschBuderusLocalOnlySensor)
        assert {key: snapshot(registry, key) for key in (SPEED_ID, STATUS_ID)} == (
            before
        )
        # No cloud sensor of their own, no removal, no new unique ID.
        assert registry.async_get_entity_id("sensor", DOMAIN, CLOUD_SPEED_ID) is None
        assert registry.async_get_entity_id("sensor", DOMAIN, CLOUD_STATUS_ID) is None
        assert not [item for item in changes if item[0] == "remove"]
        assert not [
            item for item in changes if item[1] in (speed.entity_id, status.entity_id)
        ]
        # The pair rule decides the shown reading; the states stay complete.
        expected_speed = 41 if local_values else backend.speed
        expected_status = "defrost" if local_values else backend.refrigerant_status
        source = "local" if local_values else "cloud"
        await eventually(
            lambda: (
                (state := hass.states.get(speed.entity_id)) is not None
                and state.state == str(expected_speed)
            )
        )
        speed_state = hass.states.get(speed.entity_id)
        assert speed_state.attributes["state_class"] == "measurement"
        assert speed_state.attributes["unit_of_measurement"] == "%"
        assert speed_state.attributes["source"] == source
        await eventually(
            lambda: (
                (state := hass.states.get(status.entity_id)) is not None
                and state.state == expected_status
            )
        )
        status_state = hass.states.get(status.entity_id)
        assert set(LOCAL_STATES) <= set(status_state.attributes["options"])
        assert status_state.attributes["source"] == source
        assert status.translation_key == "refrigerant_status"
        # A later rediscovery changes nothing either.
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        assert {key: snapshot(registry, key) for key in (SPEED_ID, STATUS_ID)} == (
            before
        )
        assert not [item for item in changes if item[0] == "remove"]


async def test_a_probed_reading_found_while_running_adds_no_cloud_sensor(
    hass,
) -> None:
    """The cloud offers a reviewed local-only reading only after the start
    (a later discovery): no cloud sensor and no hand-over either."""
    from datetime import UTC, datetime

    from custom_components.bosch_buderus_heating import sensor
    from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
    from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
    from tests.test_validity import _coordinator

    coordinator = _coordinator(hass)
    registry = er.async_get(hass)
    unique_id = local_only_unique_id("gateway-one", SPEED)
    entity_id = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id,
        config_entry=coordinator.config_entry,
        capabilities={"state_class": "measurement"},
    ).entity_id
    added: list = []
    scope = SimpleNamespace(async_on_unload=lambda _: None)
    sensor._track_resource_sensors(scope, coordinator, added.extend)
    resource = parse_resource(
        {
            "id": SPEED,
            "type": "integerValue",
            "value": 38,
            "unitOfMeasure": "%",
            "writeable": False,
        }
    )
    coordinator.resources = {SPEED: resource}
    coordinator.data = {SPEED: ResourceSnapshot(resource, True, datetime.now(UTC))}
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert added == []
    assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) == entity_id
    await coordinator.async_shutdown()
