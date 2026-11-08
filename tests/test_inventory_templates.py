"""1A (P49b): stored resource descriptions as templates beyond 30 days.

The descriptions keep type, unit, allowed values, limits, step and the
writable flag, but never a value. They serve the local plant view as
templates where they fit a local answer, and never make a resource present.
Inventory starts (P47/P49) still use only resources seen within 30 days.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.discovery_inventory import (
    DiscoveryInventory,
    _gateways,
    metadata_only,
)
from custom_components.bosch_buderus_heating.local_plant_view import (
    build_local_plant_view,
    template_compatible,
)
from custom_components.bosch_buderus_heating.pointt import Gateway
from tests.test_local_plant_view import cloud, local

INVENTORY = importlib.import_module(
    "custom_components.bosch_buderus_heating.discovery_inventory"
)
GATEWAY = "000123"
SETPOINT = "/heatingCircuits/hc1/currentRoomSetpoint"
LEVEL = "/dhwCircuits/dhw1/currentTemperatureLevel"
LEVELS = ["off", "low", "high", "eco"]


def described():
    return {
        SETPOINT: cloud(
            SETPOINT,
            "floatValue",
            21.5,
            unitOfMeasure="C",
            minValue=5,
            maxValue=30,
            stepSize=0.5,
        ),
        LEVEL: cloud(LEVEL, "stringValue", "high", allowedValues=LEVELS),
    }


def recorded(hass) -> DiscoveryInventory:
    inventory = DiscoveryInventory(hass, "entry")
    inventory.record(Gateway(GATEWAY, model="MX400"), described())
    return inventory


def test_descriptions_outlive_the_age_limit_without_values(
    hass: HomeAssistant,
) -> None:
    inventory = recorded(hass)
    with patch.object(INVENTORY, "MAX_AGE", timedelta(0)):
        # Inventory starts are unchanged: nothing within the age limit.
        assert inventory.resources(GATEWAY) == {}
        assert inventory.startup_gateways((GATEWAY,)) is None
        templates = inventory.templates(GATEWAY)
    assert set(templates) == {SETPOINT, LEVEL}
    for path, template in templates.items():
        assert not template.has_value and template.value is None
        assert not template.has_values and template.values == ()
        assert not template.references and not template.program_fields
        assert template.metadata == described()[path].metadata
    assert templates[SETPOINT].metadata.minimum == 5
    assert templates[SETPOINT].metadata.writable
    assert "21.5" not in repr(inventory._data()["gateways"][GATEWAY]["templates"])


def test_descriptions_survive_a_reload_after_the_age_limit(
    hass: HomeAssistant,
) -> None:
    data = recorded(hass)._data()
    later = datetime.now(UTC) + timedelta(days=31)
    ((_, stored),) = _gateways(data, later)
    assert stored.resources == {}  # too old for a start
    assert set(stored.templates) == {SETPOINT, LEVEL}
    # Saved again: the descriptions stay, the old rows are gone.
    inventory = DiscoveryInventory(hass, "entry")
    inventory._gateways = {GATEWAY: stored}
    again = inventory._data()["gateways"][GATEWAY]
    assert again["resources"] == {}
    ((_, reloaded),) = _gateways({"gateways": {GATEWAY: again}}, later)
    assert set(reloaded.templates) == {SETPOINT, LEVEL}


def test_a_store_without_descriptions_and_damaged_ones_load(
    hass: HomeAssistant,
) -> None:
    data = recorded(hass)._data()
    row = data["gateways"][GATEWAY]
    older = {key: value for key, value in row.items() if key != "templates"}
    ((_, stored),) = _gateways({"gateways": {GATEWAY: older}}, datetime.now(UTC))
    assert set(stored.resources) == set(stored.templates) == {SETPOINT, LEVEL}
    row["resources"] = {}
    row["templates"][SETPOINT] = {"path": SETPOINT}  # damaged
    ((_, stored),) = _gateways(data, datetime.now(UTC))
    assert set(stored.templates) == {LEVEL}


def test_forgotten_parts_and_gateways_lose_their_descriptions(
    hass: HomeAssistant,
) -> None:
    inventory = recorded(hass)
    inventory.forget(GATEWAY, [LEVEL])
    assert set(inventory.templates(GATEWAY)) == {SETPOINT}
    inventory.retain(())  # deselected
    assert inventory.templates(GATEWAY) == {}
    inventory = recorded(hass)
    inventory.drop(GATEWAY)  # forgotten (exchanged gateway)
    assert inventory.templates(GATEWAY) == {}


def test_a_description_is_a_template_only_where_it_fits() -> None:
    templates = {path: metadata_only(item) for path, item in described().items()}
    view = build_local_plant_view(
        [local(SETPOINT, "floatValue", 22.0, unitOfMeasure="C")],
        templates=templates,
    )
    # Never present by itself: only the path with a local answer.
    assert set(view.resources) == {SETPOINT} and view.templates_used == 1
    resource = view.resources[SETPOINT]
    assert resource.value == 22.0  # the local value, never a stored one
    assert resource.metadata.minimum == 5 and resource.metadata.step_size == 0.5
    assert not resource.metadata.writable  # still read-only
    other = local(SETPOINT, "floatValue", 22.0, unitOfMeasure="%")
    assert not build_local_plant_view([other], templates=templates).templates_used
    level = build_local_plant_view(
        [local(LEVEL, "stringValue", "low", allowedValues=["low", "boost"])],
        templates=templates,
    )
    assert not level.templates_used  # other allowed values
    # A described template has no shape, a full one keeps the shape rule.
    energy = "/heatSources/emon/totalConsumption"
    parts = [{"outputProduced": 30}, {"eheater": 2}]
    reading = build_local_plant_view(
        [local(energy, "emonValue", values=parts, unit="kWh")]
    ).resources[energy]
    full = cloud(energy, "emonValue", values=[{"outputProduced": 30}])
    assert not template_compatible(reading, full)
    assert template_compatible(reading, metadata_only(full))


def test_a_description_keeps_an_integer_whole() -> None:
    from custom_components.bosch_buderus_heating.local_plant_view import (
        from_template,
    )

    path = "/heatSources/actualModulation"
    template = metadata_only(cloud(path, "integerValue", 45, unitOfMeasure="%"))
    reading = cloud(path, "integerValue", 46.0, unitOfMeasure="%")
    assert type(from_template(reading, template).value) is int


async def test_a_discarded_view_never_writes_the_baseline(
    hass: HomeAssistant,
) -> None:
    from unittest.mock import AsyncMock

    from custom_components.bosch_buderus_heating.faults import FaultTracker

    tracker = FaultTracker(hass, "entry", GATEWAY)
    tracker._initialized = True
    tracker._store.async_save = AsyncMock()
    tracker.discard()
    await tracker.async_close()
    tracker._schedule_save()
    tracker._store.async_save.assert_not_awaited()


def test_descriptions_of_deselected_gateways_are_forgotten(
    hass: HomeAssistant,
) -> None:
    inventory = recorded(hass)
    inventory.record(Gateway("other"), described())
    inventory.keep_templates(["other"])
    assert inventory.templates(GATEWAY) == {}
    assert inventory.resources(GATEWAY)  # rows keep their age limit
    assert set(inventory.templates("other")) == {SETPOINT, LEVEL}
