"""B3: the room devices' own error lists as extra fault information.

Synthetic readings in the documented ``deviceErrors`` shape; no real device
offered these lists so far. They are shown next to the faults and never
create, end or announce a fault.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
from decimal import Decimal
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.faults import (
    DeviceError,
    local_device_errors,
)
from custom_components.bosch_buderus_heating.gateway_identity import GatewayNamespace
from custom_components.bosch_buderus_heating.local_api.catalog import request_for_path
from custom_components.bosch_buderus_heating.local_api.models import (
    DeviceFault,
    DeviceFaultList,
    FaultList,
)
from custom_components.bosch_buderus_heating.local_polling.inventory import (
    Freshness,
)
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.local_state import LocalCondition
from tests.test_connection_runtime import settle
from tests.test_local_faults import ITEM, Steps
from tests.test_managed_local_plant import fault_view

pytestmark = pytest.mark.usefixtures("socket_enabled", "enable_custom_integrations")
SENSOR = "defectiveTempSensor"
VALVE = "eTRVValveStatus"
_READS = itertools.count(5000.0)


def bounded(test):
    """Fail within 90 s instead of hanging."""

    @functools.wraps(test)
    async def run(*args, **kwargs):
        async with asyncio.timeout(90):
            await test(*args, **kwargs)

    return run


def observation(path, *rows, monotonic=1.0):
    return SimpleNamespace(
        request=request_for_path(path),
        observed_monotonic=monotonic,
        data=DeviceFaultList(
            tuple(
                DeviceFault(kind, Decimal(occurrence), value, tuple(allowed))
                for kind, occurrence, value, allowed in rows
            )
        ),
    )


def row(kind=SENSOR, value="active", occurrence=10):
    allowed = (
        ("inactive", "active")
        if kind == SENSOR
        else ("valveTight", "bigAdjRange", "smallAdjRange", "valveErrPos")
    )
    return kind, occurrence, value, allowed


def device_view(path, *rows, available=True):
    item = observation(path, *rows, monotonic=next(_READS))
    return SimpleNamespace(
        request=item.request,
        freshness=Freshness.FRESH if available else Freshness.EXPIRED,
        available=available,
        observation=item,
        target_interval_seconds=60,
    )


def test_the_documented_shape_gives_bounded_entries() -> None:
    path, entries = local_device_errors(
        observation(
            "/devices/device3/errors",
            row(),
            row(value="inactive", occurrence=11),
            row(VALVE, "valveErrPos", 0),
            row("bad type!", "active", 12),
        )
    )
    assert path == "/devices/device3/errors"
    assert entries == (
        DeviceError(3, SENSOR, "active"),
        DeviceError(3, SENSOR, "inactive"),
        DeviceError(3, VALVE, "other"),  # meaning not documented
    )


def test_other_readings_are_no_device_error_list() -> None:
    error_list = SimpleNamespace(
        request=request_for_path("/notifications"), data=FaultList(())
    )
    assert local_device_errors(error_list) is None
    wrong_path = observation("/devices/device3/errors", row())
    wrong_path.request = SimpleNamespace(path="/devices/device33/errors")
    assert local_device_errors(wrong_path) is None
    assert local_device_errors(object()) is None


def test_device_errors_never_change_the_fault_lifecycle(hass: HomeAssistant) -> None:
    steps = Steps(hass)
    steps.cloud(ITEM)
    steps.local(ITEM)
    states = []
    steps.tracker.async_add_state_listener(lambda: states.append(1))
    before = (steps.tracker.active, steps.tracker._serialize())
    lists = {
        "/devices/device2/errors": (
            DeviceError(2, SENSOR, "active"),
            DeviceError(2, VALVE, "other"),
        ),
        "/devices/device1/errors": (DeviceError(1, SENSOR, "inactive"),),
    }
    assert steps.tracker.process_device_errors(lists)
    assert not steps.tracker.process_device_errors(dict(lists))  # unchanged
    assert len(states) == 1
    assert steps.tracker.device_errors == (DeviceError(2, SENSOR, "active"),)
    assert (steps.tracker.active, steps.tracker._serialize()) == before
    assert steps.events == []  # no event, so no notification either
    assert steps.tracker.diagnostics()["device_errors"] == {
        "lists": 2,
        "active": 1,
        "inactive": 1,
        "other": 1,
    }
    assert steps.tracker.process_device_errors({})
    assert steps.tracker.device_errors == ()
    assert len(steps.tracker.active) == 1


def test_only_current_lists_of_a_ready_access_are_used() -> None:
    from tests.test_local_selection import TARGET, FakeRuntime

    runtime = FakeRuntime([])
    selection = LocalSelection(runtime, TARGET)
    current = device_view("/devices/device1/errors", row())
    old = device_view("/devices/device2/errors", row(), available=False)
    errors = fault_view(ITEM, monotonic=1.0)
    condition = LocalCondition.READY
    runtime.local_state = lambda binding: SimpleNamespace(
        condition=condition,
        resources=(current, old, errors),
        current_observations=(),
    )
    assert selection.device_error_lists() == (current.observation,)
    condition = LocalCondition.PAUSED
    assert selection.device_error_lists() == ()


# ------------------------------------------------------------ in the entry


@pytest.fixture
def loaded_integration(hass):
    hass.config.components.add(DOMAIN)


@bounded
@pytest.mark.usefixtures("loaded_integration")
async def test_the_fault_sensor_shows_active_device_errors(hass) -> None:
    from tests.test_cloud_off_faults import FaultBackend
    from tests.test_local_backed import GATEWAY, local_values, started
    from tests.test_local_plant_mode import readings
    from tests.test_managed_platforms import native
    from tests.test_managed_pointt import eventually, running, source_for

    backend = FaultBackend()
    async with running(hass, backend, configure=local_values()) as (_, _, owner, _):
        await started(hass, owner)
        live = source_for(owner, GATEWAY)
        await eventually(lambda: len(live.faults.active) == 2)
        entity = native(owner, "sensor", f"{GATEWAY}:active_faults")
        await eventually(lambda: hass.states.get(entity.entity_id).state == "2")
        events = []
        live.faults.async_add_event_observer(events.append)

        def publish(*views):
            runtime = owner.connections
            context = runtime._contexts["local"]
            runtime._local_states["local"] = SimpleNamespace(
                context=context.identity_context(GatewayNamespace.K40_LOCAL),
                condition=LocalCondition.READY,
                current_observations=tuple(readings()),
                resources=views,
            )
            runtime._notify()

        def attributes():
            return hass.states.get(entity.entity_id).attributes

        publish(
            device_view("/devices/device4/errors", row(), row(VALVE, "valveTight", 0))
        )
        await eventually(lambda: "device_errors" in attributes())
        assert attributes()["device_errors"] == [{"device": 4, "error_type": SENSOR}]
        assert hass.states.get(entity.entity_id).state == "2"  # no extra fault
        publish(device_view("/devices/device4/errors", row(value="inactive")))
        await eventually(lambda: "device_errors" not in attributes())
        await settle(hass)
        assert events == []
        assert len(live.faults.active) == 2
