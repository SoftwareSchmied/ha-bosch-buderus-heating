"""The plant model never breaks a setup, the recordings, the diagnostics or
the options.

Every result of the plant model is optional: an unexpected error inside it
gives no result, and everything behaves as before the plant model. These
tests force such an error in each place and run the real setup of my own
K40 from its cloud capture (as the golden master does): the heat generator
sensor, the recordings and their entities, the diagnostics, the options menu
and the optional component devices stay usable, and nothing is logged as an
error.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import recordings
from custom_components.bosch_buderus_heating.component_devices import (
    OPTION,
    component_identifier,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.diagnostics import (
    _gateway_diagnostics,
    async_get_config_entry_diagnostics,
)
from custom_components.bosch_buderus_heating.plant_inputs import (
    components,
    current_plant,
    existing_tracker,
)
from custom_components.bosch_buderus_heating.recordings import gating
from tests import test_x40_golden as golden
from tests.helpers.managed_runtime import wait_for_sources
from tests.helpers.rrc_cloud import Clock, cloud_patches

PACKAGE = "custom_components.bosch_buderus_heating"
GW = "gateway-one"
PLANT = "k40_own_cloud"


@contextmanager
def _k40(hass: HomeAssistant, *, option: bool = False) -> Iterator[Any]:
    plant = golden.PLANTS[PLANT]
    cloud = plant.build()
    clock = Clock()
    entry = golden.managed_entry(hass, plant.gateway)
    if option:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, OPTION: True}
        )
    with cloud_patches(cloud, clock, plant.gateway):
        yield entry, clock


async def _start(hass: HomeAssistant, entry: Any, clock: Clock) -> Any:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await wait_for_sources(hass, entry, 1)
    await hass.async_block_till_done()
    clock.now += 3600
    coordinator = golden.source_of(entry)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    return coordinator


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        f"{record.name}: {record.getMessage()}"
        for record in caplog.records
        if record.levelno >= logging.ERROR
    ]


def _component_devices(hass: HomeAssistant, entry: Any) -> list[str]:
    return sorted(
        "{}:{}".format(*parsed[1:])
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
        if (parsed := component_identifier(device)) is not None
    )


def _heat_generator_state(hass: HomeAssistant) -> Any:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{GW}:heat_generator"
    )
    assert entity_id is not None
    return hass.states.get(entity_id)


async def _options_menu(hass: HomeAssistant, entry: Any) -> list[str]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    options = list(result["menu_options"])
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": options[0]}
    )
    return options


@pytest.mark.parametrize("option", [False, True])
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_an_error_of_the_model_breaks_nothing(
    hass: HomeAssistant,
    freezer: Any,
    caplog: pytest.LogCaptureFixture,
    option: bool,
) -> None:
    freezer.move_to(golden.FROZEN)
    caplog.set_level(logging.DEBUG, logger=PACKAGE)
    with (
        _k40(hass, option=option) as (entry, clock),
        patch(f"{PACKAGE}.plant_inputs.resolve", side_effect=RuntimeError("model")),
    ):
        coordinator = await _start(hass, entry, clock)
        tracker = existing_tracker(coordinator)
        assert tracker is not None and tracker.loaded
        # No result: as before the plant model.
        assert current_plant(coordinator) is None
        assert components(coordinator) is None
        assert not gating.hybrid_plant(coordinator)
        assert not gating.icom_heat_pump(coordinator)
        assert gating.heat_pump_source(coordinator) is None
        # The heat generator sensor exists and shows its module.
        state = _heat_generator_state(hass)
        assert state is not None and state.state not in ("unknown", "unavailable")
        # The recordings run as before (my K40 reads them from the cloud).
        manager = recordings.manager_of_gateway(hass, GW)
        assert manager is not None
        await manager.async_run("manual")
        assert manager.runs and not manager.runs[-1].get("failed")
        assert "error" not in recordings.diagnostics(hass, GW)
        # The diagnostics load; the plant block names the error.
        report = await async_get_config_entry_diagnostics(hass, entry)
        assert report
        gateway = _gateway_diagnostics(1, coordinator)
        assert gateway["plant"] == {"error": "unavailable"}
        assert "error" not in gateway["recordings"]
        # The options menu opens; the components step only with the option.
        menu = await _options_menu(hass, entry)
        assert ("components" in menu) is option
        # No component device without a component list.
        assert _component_devices(hass, entry) == []
        assert _errors(caplog) == []
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_stored_row_that_cannot_be_read_never_keeps_the_model_off(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    freezer.move_to(golden.FROZEN)
    with (
        _k40(hass) as (entry, clock),
        patch(f"{PACKAGE}.plant_inputs.parse_row", side_effect=RuntimeError("row")),
    ):
        coordinator = await _start(hass, entry, clock)
        tracker = existing_tracker(coordinator)
        assert tracker is not None and tracker.loaded
        plant = current_plant(coordinator)
        assert plant is not None and plant.plant_kind == "heat_pump"
        assert _errors(caplog) == []
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_options_open_when_the_component_list_fails(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Also an error after the plant model (in the list of the devices) never
    takes the options menu down; switching the devices on is saved even when
    creating them fails."""
    freezer.move_to(golden.FROZEN)
    with _k40(hass) as (entry, clock):
        await _start(hass, entry, clock)
        with patch(
            f"{PACKAGE}.component_devices.device_components",
            side_effect=RuntimeError("list"),
        ):
            menu = await _options_menu(hass, entry)
            assert "components" not in menu
        with patch(
            f"{PACKAGE}.component_devices.ComponentDevices.sync",
            side_effect=RuntimeError("registry"),
        ):
            flow = await hass.config_entries.options.async_init(entry.entry_id)
            await hass.config_entries.options.async_configure(
                flow["flow_id"], {"next_step_id": "components"}
            )
            result = await hass.config_entries.options.async_configure(
                flow["flow_id"], {OPTION: True}
            )
            assert result["type"] is not FlowResultType.ABORT
        assert entry.options[OPTION] is True
        assert _errors(caplog) == []
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_failed_device_is_created_with_the_next_update(
    hass: HomeAssistant, freezer: Any
) -> None:
    """One error of the device registry stops neither the other component
    devices nor, with the next update, the one that failed."""
    freezer.move_to(golden.FROZEN)
    original = dr.DeviceRegistry.async_get_or_create
    failed: list[str] = []

    def flaky(self: dr.DeviceRegistry, **kwargs: Any) -> dr.DeviceEntry:
        names = [item[1] for item in kwargs.get("identifiers", ())]
        if not failed and any(":component:" in name for name in names):
            failed.extend(names)
            raise RuntimeError("registry")
        return original(self, **kwargs)

    with (
        _k40(hass, option=True) as (entry, clock),
        patch.object(dr.DeviceRegistry, "async_get_or_create", flaky),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await wait_for_sources(hass, entry, 1)
        await hass.async_block_till_done()
        # The others at once; the failed one is missing until the next update.
        assert failed
        assert len(_component_devices(hass, entry)) == 2
        clock.now += 3600
        await golden.source_of(entry).async_refresh()
        await hass.async_block_till_done()
        assert _component_devices(hass, entry) == ["16:253", "8:65281", "8:8"]
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_diagnostics_load_when_the_recordings_fail(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to(golden.FROZEN)
    with _k40(hass) as (entry, clock):
        coordinator = await _start(hass, entry, clock)
        with patch(
            f"{PACKAGE}.recordings.diagnostics", side_effect=RuntimeError("recordings")
        ):
            report = await async_get_config_entry_diagnostics(hass, entry)
            assert report
            gateway = _gateway_diagnostics(1, coordinator)
        assert gateway["recordings"] == {"error": "unavailable"}
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
