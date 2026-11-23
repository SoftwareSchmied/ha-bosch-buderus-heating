"""Recordings and the plant kind together.

The plant kind decides which recordings a plant is asked for: the heat
pump's series of a hybrid system (``hs2``) and the series of a heat pump
behind an iCom gateway with its consumed energy. A boiler whose outdoor unit
is reported as not connected and a system of type ``eHybrid`` (a heat pump
with an additional heater) are no hybrid system, so they read no series of
``hs2``, whatever else they report. Whether the energy monitoring takes a
unit of power (kW) depends on the gateway only, never on the plant kind.
Real coordinators and manager runs with synthetic answers.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.plant_inputs import current_plant
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from custom_components.bosch_buderus_heating.recordings import gating
from custom_components.bosch_buderus_heating.recordings.series import (
    BY_KEY,
    SOLAR_YIELD,
)
from tests.test_hybrid_recordings_without_counters import HS2
from tests.test_hybrid_recordings_without_counters import answers as hs2_answers
from tests.test_icom_heat_pump_recordings import (
    ANCHOR,
    BOILER,
    CONSUMED,
    MONITORING,
    NOW,
    plant,
    recording,
    recordings,
)
from tests.test_plant_detection import load_case
from tests.test_plant_inputs import _coordinator as plant_coordinator
from tests.test_plant_inputs import _start
from tests.test_recordings_manager import Clock

SOLAR = SOLAR_YIELD.key


async def plant_of(hass: HomeAssistant, case: dict[str, Any]) -> Any:
    """As ``plant``, for a case changed in the test."""
    coordinator = plant_coordinator(hass, case)
    coordinator.resources[ANCHOR] = Resource(
        ANCHOR,
        value=10,
        has_value=True,
        metadata=ResourceMetadata(resource_type="floatValue"),
    )
    coordinator.local_selection.running_sources = lambda: ()
    await _start(hass, coordinator)
    return coordinator


def icom_ehybrid() -> dict[str, Any]:
    """The iCom heat pump of #467 with the system type eHybrid."""
    case = copy.deepcopy(load_case("issue_467_icom_heat_pump"))
    case["values"]["/system/type"] = "eHybrid"
    return case


def icom_boiler_not_connected() -> dict[str, Any]:
    """An iCom boiler with the resources of a hybrid control whose outdoor
    unit is not connected."""
    case = copy.deepcopy(load_case("issue_104_icom_boiler"))
    case["values"]["/heatSources/hybrid/outdoorStatus"] = "NOT_CONNECTED"
    case["named_paths"] = [
        "/heatSources/hybrid/controlStrategy",
        "/heatSources/hybrid/activeHeatSource",
    ]
    return case


def consumed_in(unit: str) -> dict[str, Any]:
    """Every series of hs2 and the consumed energy answer; the consumed
    energy as samples of the mean power of 1 kW (60 samples summing to 60)."""
    answers = dict(hs2_answers())
    answers.update(
        {path: (lambda interval: recording(interval, 1.0)) for path in HS2.values()}
    )
    answers[CONSUMED] = lambda interval: recording(interval, 60.0, 60, unit=unit)
    return answers


@pytest.mark.parametrize(
    ("case", "kind"),
    [
        ("boiler_outdoor_not_connected", "boiler"),
        ("boiler_outdoor_not_connected_hybman", "boiler"),
        ("boiler_outdoor_not_connected_heat_pump_type", "boiler"),
        ("contradiction_outdoor", "boiler"),
        ("ehybrid", "heat_pump"),
        ("ehybrid_boiler_type", "boiler"),
    ],
)
async def test_no_hybrid_system_reads_no_series_of_hs2(
    hass: HomeAssistant, case: str, kind: str
) -> None:
    """Each series of hs2 would answer; none is asked, and the boiler series
    (with the consumed energy) are read as on any boiler-shaped plant."""
    coordinator = await plant(hass, case)
    result = current_plant(coordinator)
    assert result is not None and result.plant_kind == kind and not result.hybrid
    manager, calls = await recordings(hass, coordinator, Clock(NOW), consumed_in("kWh"))
    await manager.async_run("start")
    asked = {path for path, _ in calls}
    assert not asked & set(HS2.values())
    assert not set(manager.available()) & set(HS2)
    assert "heat_pump_source" not in manager.diagnostics()
    assert gating.allowed_keys(coordinator) == BOILER
    assert CONSUMED in asked
    await coordinator.async_shutdown()


async def test_a_hybrid_system_still_reads_hs2_with_the_same_answers(
    hass: HomeAssistant,
) -> None:
    """The counterpart: the same answers on a hybrid system read hs2."""
    coordinator = await plant(hass, "hybman")
    manager, calls = await recordings(hass, coordinator, Clock(NOW), consumed_in("kWh"))
    await manager.async_run("start")
    assert set(HS2.values()) <= {path for path, _ in calls}
    assert manager.diagnostics()["heat_pump_source"] == "hs2"
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    ("unit", "taken"), [("kWh", True), ("KWh", True), ("kW", False)]
)
async def test_a_k30_boiler_with_its_outdoor_unit_not_connected_takes_no_kw(
    hass: HomeAssistant, unit: str, taken: bool
) -> None:
    """The consumed energy of a K30 boiler is read as the mean times the hours
    in kWh in any case; kW is refused there, as on every gateway but iCom."""
    coordinator = await plant(hass, "boiler_outdoor_not_connected")
    manager, _calls = await recordings(hass, coordinator, Clock(NOW), consumed_in(unit))
    await manager.async_run("start")
    series = manager.diagnostics()["series"]["total_consumed"]  # type: ignore[index]
    if taken:
        assert "total_consumed" in manager.available()
        day = NOW.date() - timedelta(days=1)
        assert manager.daily("total_consumed", day, day)[0].y == pytest.approx(24.0)
        assert series["cloud"] == "ok"
    else:
        assert "total_consumed" not in manager.available()
        assert series == {"local": None, "cloud": "unit", "unit": "kW"}
    await coordinator.async_shutdown()


async def test_an_icom_heat_pump_of_type_ehybrid_reads_its_consumed_energy(
    hass: HomeAssistant,
) -> None:
    """eHybrid does not make the iCom heat pump a hybrid system: it keeps the
    series of a heat pump with the consumed energy, in kW too, and no hs2."""
    coordinator = await plant_of(hass, icom_ehybrid())
    result = current_plant(coordinator)
    assert result is not None and result.plant_kind == "heat_pump"
    assert gating.icom_heat_pump(coordinator)
    assert gating.allowed_keys(coordinator) == {*MONITORING, SOLAR, "total_consumed"}
    manager, calls = await recordings(hass, coordinator, Clock(NOW), consumed_in("kW"))
    await manager.async_run("start")
    asked = {path for path, _ in calls}
    assert CONSUMED in asked
    assert not asked & set(HS2.values())
    assert not asked & {BY_KEY[key].path for key in BOILER - {"total_consumed"}}
    assert "total_consumed" in manager.available()
    day = NOW.date() - timedelta(days=1)
    assert manager.daily("total_consumed", day, day)[0].y == pytest.approx(24.0)
    await coordinator.async_shutdown()


async def test_an_icom_boiler_with_its_outdoor_unit_not_connected_reads_as_a_boiler(
    hass: HomeAssistant,
) -> None:
    """The boiler series as on any iCom boiler; the consumed energy is
    samples of the mean power behind the iCom gateway, so kW is taken."""
    coordinator = await plant_of(hass, icom_boiler_not_connected())
    result = current_plant(coordinator)
    assert result is not None and result.plant_kind == "boiler"
    assert not gating.icom_heat_pump(coordinator)
    assert gating.heat_pump_source(coordinator) is None
    assert gating.allowed_keys(coordinator) == BOILER
    manager, calls = await recordings(hass, coordinator, Clock(NOW), consumed_in("kW"))
    await manager.async_run("start")
    asked = {path for path, _ in calls}
    assert not asked & {*MONITORING.values(), *HS2.values()}
    assert "total_consumed" in manager.available()
    day = NOW.date() - timedelta(days=1)
    assert manager.daily("total_consumed", day, day)[0].y == pytest.approx(24.0)
    await coordinator.async_shutdown()
