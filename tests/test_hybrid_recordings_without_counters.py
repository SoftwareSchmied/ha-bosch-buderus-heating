"""Hybrid: the heat pump's own recordings are read without its counters.

On a hybrid system (plant kind ``hybrid``) the heat pump is the heat
source ``hs2``; its own recordings under that heat source are read also
when it reports no counters of its own there. Only recordings that answer
are kept (no entity of their own, as before); a 404 is asked again once a
day. A plant with a heat pump only, an iCom gateway and a plant without a
plant kind are not asked. Synthetic answers; not yet confirmed on a real
hybrid system without these counters.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.plant_detection import PlantResult
from custom_components.bosch_buderus_heating.recordings import (
    Period as RecordingPeriod,
)
from custom_components.bosch_buderus_heating.recordings import gating
from custom_components.bosch_buderus_heating.recordings.manager import (
    RECHECK_MISSING,
)
from custom_components.bosch_buderus_heating.recordings.measures import (
    EntitySpec,
    measures,
)
from custom_components.bosch_buderus_heating.recordings.sensor import (
    RecordingPeriodSensor,
)
from custom_components.bosch_buderus_heating.recordings.series import (
    BY_KEY,
    CLOUD_PATHS,
    HEAT_SOURCE_SERIES,
)
from tests.test_icom_heat_pump_recordings import (
    NOW,
    plant,
    recording,
    recordings,
)
from tests.test_recordings_manager import Clock

HS2 = {item.key: item.path for item in HEAT_SOURCE_SERIES if item.heat_source == "hs2"}
HS2_HEAT = HS2["hs2_total_heat"]
HS2_COMPRESSOR = HS2["hs2_total_compressor"]


def answers() -> dict[str, Any]:
    """The heat pump's total heat and compressor answer, the rest is 404."""
    return {
        HS2_HEAT: lambda interval: recording(interval, 2.0),
        HS2_COMPRESSOR: lambda interval: recording(interval, 0.5),
    }


@pytest.mark.parametrize(
    ("case", "source"),
    [
        ("hybman", "hs2"),
        ("outdoor_connected", "hs2"),
        ("issue_138_hybrid_manager_new", "hs2"),
        # A heat pump only, an iCom heat pump, no plant kind: not asked.
        ("k40_own", None),
        # eHybrid is a heat pump with an additional heater: not asked.
        ("ehybrid", None),
        ("icom_hybrid_place", None),
        ("bus_detecting", None),
    ],
)
async def test_a_hybrid_system_reads_hs2_without_counters(
    hass: HomeAssistant, case: str, source: str | None
) -> None:
    coordinator = await plant(hass, case)
    assert gating.heat_pump_source(coordinator) == source
    assert gating.heat_pump_keys(coordinator) == (
        frozenset(HS2) if source else frozenset()
    )
    assert gating.heat_pump_source_basis(coordinator) == (
        "plant_kind" if source else None
    )
    await coordinator.async_shutdown()


async def test_an_icom_gateway_never_reads_hs2(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a hybrid plant kind on an iCom gateway reads no hs2 series."""
    coordinator = await plant(hass, "icom_hybrid_place")
    monkeypatch.setattr(
        "custom_components.bosch_buderus_heating.plant_inputs.current_plant",
        lambda _coordinator: PlantResult("hybrid"),
    )
    assert gating.heat_pump_source(coordinator) is None
    await coordinator.async_shutdown()


async def test_the_six_series_are_read_and_a_404_is_kept(hass: HomeAssistant) -> None:
    coordinator = await plant(hass, "hybman")
    clock = Clock(NOW)
    manager, calls = await recordings(hass, coordinator, clock, answers())
    await manager.async_run("start")
    asked = [path for path, _ in calls]
    # The heat pump's six series first, then the gateway's.
    assert set(asked[:6]) == set(HS2.values())
    # The gateway's series as before (this plant shows no energy monitoring:
    # the boiler series too).
    boiler = {
        BY_KEY[key].path
        for key in ("ch_actual_power", "dhw_actual_power", "total_consumed")
    }
    assert set(asked) == {*HS2.values(), *CLOUD_PATHS, *boiler}
    assert {"hs2_total_heat", "hs2_total_compressor"} <= set(manager.available())
    # No entities of their own.
    assert not any(spec.measure.heat_source for spec in manager.specs())
    report = manager.diagnostics()
    assert report["heat_pump_source"] == "hs2"
    assert report["heat_pump_source_basis"] == "plant_kind"
    series = report["series"]
    assert series["hs2_total_heat"]["cloud"] == "ok"  # type: ignore[index]
    assert series["hs2_ch_heat"]["cloud"] == "missing"  # type: ignore[index]

    # A recording that answered 404 is asked again once a day only.
    release = manager.acquire()
    clock.now += timedelta(hours=1)
    calls.clear()
    await manager.async_run("hourly")
    asked = {path for path, _ in calls}
    assert HS2_HEAT in asked
    assert HS2["hs2_ch_heat"] not in asked
    clock.now += RECHECK_MISSING
    calls.clear()
    await manager.async_run("hourly")
    assert HS2["hs2_ch_heat"] in {path for path, _ in calls}
    release()
    await coordinator.async_shutdown()


def _heat_attributes(coordinator: Any, manager: Any) -> dict[str, Any]:
    measure = next(
        item for item in measures(("total_heat",)) if item.key == "total_heat"
    )
    spec = EntitySpec(measure, RecordingPeriod.YESTERDAY, measure.primary)
    return dict(
        RecordingPeriodSensor(coordinator, manager, spec).extra_state_attributes
    )


@pytest.mark.parametrize("answered", [True, False])
async def test_the_gateways_heat_is_the_boilers_only_with_an_answered_series(
    hass: HomeAssistant, answered: bool
) -> None:
    """Known from its plant kind alone, a hybrid system's gateway heat keeps
    its meaning until a series of the heat pump has answered (every one 404:
    no attribute)."""
    coordinator = await plant(hass, "hybman")
    manager, _calls = await recordings(
        hass, coordinator, Clock(NOW), answers() if answered else {}
    )
    await manager.async_run("start")
    assert gating.heat_pump_source_basis(coordinator) == "plant_kind"
    attributes = _heat_attributes(coordinator, manager)
    assert attributes.get("heat_generator") == ("boiler" if answered else None)
    await coordinator.async_shutdown()


async def test_a_heat_pump_only_plant_reads_no_hs2_series(hass: HomeAssistant) -> None:
    coordinator = await plant(hass, "k40_own")
    manager, calls = await recordings(hass, coordinator, Clock(NOW), answers())
    await manager.async_run("start")
    assert not {path for path, _ in calls} & set(HS2.values())
    assert "heat_pump_source" not in manager.diagnostics()
    await coordinator.async_shutdown()
