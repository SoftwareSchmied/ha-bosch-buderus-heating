"""The plant model's packages together (S2 sensor and evidence, S3 reads).

- A hybrid system known only from its components (hybrid manager at place
  83, firmware and version gates passed, no counters of the heat pump at
  hs2): plant kind ``hybrid``, the burner share counts it as a hybrid
  system, and the heat pump's recordings at hs2 are read (basis
  ``plant_kind``).
- My own K40: no hybrid evidence, no hs2 read.
- An iCom heat pump in the shape of #467: the heat generator sensor shows its
  control unit with the plant kind, and the energy monitoring recordings are
  read instead of the boiler series.

Synthetic plants from ``tests/fixtures/plant_cases``; not yet confirmed on a
real hybrid system without hs2 counters or a real iCom heat pump.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.bosch_buderus_heating.burner_share import hybrid_detected
from custom_components.bosch_buderus_heating.heat_generator_sensor import (
    PlantViews,
    contract_attributes,
    plant_ready,
)
from custom_components.bosch_buderus_heating.plant_inputs import current_plant
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from custom_components.bosch_buderus_heating.recordings import gating
from custom_components.bosch_buderus_heating.recordings.series import (
    CLOUD_PATHS,
    HEAT_SOURCE_SERIES,
)
from tests.test_hybrid_recordings_without_counters import answers
from tests.test_icom_heat_pump_recordings import (
    ANCHOR,
    BOILER,
    MONITORING,
    NOW,
    heat_pump_answers,
    recordings,
)
from tests.test_icom_heat_pump_recordings import plant as icom_plant
from tests.test_plant_detection import load_case
from tests.test_plant_inputs import _coordinator as plant_coordinator
from tests.test_plant_inputs import _start
from tests.test_recordings_manager import Clock

HS2 = {item.key: item.path for item in HEAT_SOURCE_SERIES if item.heat_source == "hs2"}


async def _plant(hass: HomeAssistant, case: dict[str, Any]) -> Any:
    """As ``tests.test_icom_heat_pump_recordings.plant``, for a changed case."""
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


def _components_only() -> dict[str, Any]:
    """#138 with the hybrid manager as the only sign of a hybrid system: no
    resource of the hybrid control, no hybrid type value, no hs2 counters."""
    case = load_case("issue_138_hybrid_manager_new")
    case.pop("named_paths", None)
    return case


async def test_a_component_hybrid_reads_the_heat_pump_at_hs2(
    hass: HomeAssistant,
) -> None:
    coordinator = await _plant(hass, _components_only())
    plant = current_plant(coordinator)
    assert plant is not None
    assert plant.plant_kind == "hybrid"
    assert plant.evidence == ("hybrid_manager_component",)
    # S2: the burner share counts the component hybrid as a hybrid system.
    assert hybrid_detected(coordinator)
    # S3 (option B): the heat pump's recordings at hs2, decided by the kind.
    assert gating.heat_pump_source(coordinator) == "hs2"
    assert gating.heat_pump_source_basis(coordinator) == "plant_kind"
    assert gating.heat_pump_keys(coordinator) == frozenset(HS2)
    manager, calls = await recordings(hass, coordinator, Clock(NOW), answers())
    await manager.async_run("start")
    asked = {path for path, _ in calls}
    assert set(HS2.values()) <= asked
    assert set(CLOUD_PATHS) <= asked
    report = manager.diagnostics()
    assert report["heat_pump_source"] == "hs2"
    assert report["heat_pump_source_basis"] == "plant_kind"
    await coordinator.async_shutdown()


async def test_without_the_component_evidence_nothing_changes(
    hass: HomeAssistant,
) -> None:
    """The same plant without the hybrid manager row: no hybrid, no hs2."""
    case = _components_only()
    case["cloud"] = [row for row in case["cloud"] if row.get("Tok") != "83"]
    coordinator = await _plant(hass, case)
    plant = current_plant(coordinator)
    assert plant is None or plant.plant_kind != "hybrid"
    assert not hybrid_detected(coordinator)
    assert gating.heat_pump_source(coordinator) is None
    await coordinator.async_shutdown()


async def test_my_k40_gets_no_hybrid_evidence_and_no_hs2_read(
    hass: HomeAssistant,
) -> None:
    coordinator = await icom_plant(hass, "k40_own")
    plant = current_plant(coordinator)
    assert plant is not None and plant.plant_kind == "heat_pump"
    assert not hybrid_detected(coordinator)
    assert gating.heat_pump_source(coordinator) is None
    assert not gating.icom_heat_pump(coordinator)
    manager, calls = await recordings(hass, coordinator, Clock(NOW), answers())
    await manager.async_run("start")
    asked = {path for path, _ in calls}
    assert not asked & set(HS2.values())
    assert not asked & set(MONITORING.values())
    await coordinator.async_shutdown()


async def test_an_icom_heat_pump_shows_its_sensor_and_reads_its_monitoring(
    hass: HomeAssistant,
) -> None:
    """#467: the sensor's state is the control unit at place 8 with the plant
    kind, and the energy monitoring is read instead of the boiler series."""
    coordinator = await icom_plant(hass, "issue_467_icom_heat_pump")
    assert plant_ready(coordinator)
    view = PlantViews(coordinator).current()
    assert view is not None
    assert view.component_state == "CUHP"
    attributes = contract_attributes(None, None, view)
    assert attributes["state_basis"] == "component_type"
    assert attributes["plant_kind"] == "heat_pump"
    assert attributes["heat_source_type"] is None
    assert [item["type_number"] for item in attributes["components"]] == [172, 158, 165]
    assert gating.icom_heat_pump(coordinator)
    assert gating.heat_pump_source(coordinator) is None
    manager, calls = await recordings(
        hass, coordinator, Clock(NOW), heat_pump_answers()
    )
    await manager.async_run("start")
    asked = {path for path, _ in calls}
    assert set(MONITORING.values()) <= asked
    assert not asked & set(HS2.values())
    assert not BOILER & set(manager.available())
    assert {"monitoring_total_heat", "monitoring_total_compressor"} <= set(
        manager.available()
    )
    assert manager.diagnostics()["icom_heat_pump"] is True
    await coordinator.async_shutdown()
