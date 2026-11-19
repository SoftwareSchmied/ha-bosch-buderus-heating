"""Environmental energy per mode, and the default entities of the energy views.

The environmental energy is the heat produced minus the electricity put in,
for the total, heating, hot water and pool; never negative, never for
cooling. The twelve entities of the performance factors and the plant
recordings that are new in the default set are created enabled only for a
new installation; an existing registry entry keeps its state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import _enable_new_default_entities
from custom_components.bosch_buderus_heating import environmental_energy as module
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.counter_selection import counter_vector
from custom_components.bosch_buderus_heating.environmental_energy import (
    environmental_energy,
)
from custom_components.bosch_buderus_heating.performance_history import Period as FP
from custom_components.bosch_buderus_heating.performance_sensors import (
    ENABLED_FACTORS,
)
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
)
from custom_components.bosch_buderus_heating.recordings.measures import (
    entity_specs,
    measures,
)
from custom_components.bosch_buderus_heating.recordings.periods import Period
from custom_components.bosch_buderus_heating.recordings.sensor import unique_id
from custom_components.bosch_buderus_heating.sensor import (
    BoschBuderusSensor,
    build_sensor_descriptions,
)
from tests.ha_test_helpers import MockConfigEntry
from tests.test_entry import _entry
from tests.test_recordings_gaps_and_load import NOW
from tests.test_recordings_manager import Clock, Cloud, key, manager

TOTAL = "/heatSources/emon/totalConsumption"
MODES = {
    "ch": "/heatSources/emon/chConsumption",
    "dhw": "/heatSources/emon/dhwConsumption",
    "pool": "/heatSources/emon/poolConsumption",
}
COOLING = "/heatSources/emon/coolingConsumption"


def _collection(
    path: str, produced: float, compressor: float, eheater: float
) -> Resource:
    return Resource(
        path=path,
        values=(
            {"compressor": compressor},
            {"eheater": eheater},
            {"outputProduced": produced},
        ),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )


def _environmental(resource: Resource) -> list[object]:
    return [
        item
        for item in build_sensor_descriptions({resource.path: resource})
        if item.value_kind == "environmental_energy"
    ]


def _sensor(hass: HomeAssistant, resource: Resource) -> BoschBuderusSensor:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass,
        SimpleNamespace(),  # type: ignore[arg-type]
        Gateway("gateway-one", device_type="MX300", firmware_version="1.2.3"),
        entry,
    )
    coordinator.resources = {resource.path: resource}
    coordinator.update_interval = None
    coordinator.data = {
        resource.path: ResourceSnapshot(resource, True, datetime.now(UTC))
    }
    coordinator.last_update_success = True
    description = _environmental(resource)[0]
    return BoschBuderusSensor(coordinator, description)  # type: ignore[arg-type]


# ------------------------------------------------------ environmental energy
@pytest.mark.parametrize("path", [TOTAL, *MODES.values()])
def test_every_mode_domain_has_the_same_formula(hass: HomeAssistant, path: str) -> None:
    sensor = _sensor(hass, _collection(path, 20.0, 10.0, 2.0))
    assert sensor.native_value == pytest.approx(8.0)
    # The total keeps its established unique ID; the modes follow the schema.
    expected = (
        "environmental_energy"
        if path == TOTAL
        else path.strip("/").replace("/", ":") + ":environmental_energy"
    )
    assert sensor.entity_description.unique_key == expected


@pytest.mark.parametrize("path", [TOTAL, *MODES.values()])
def test_a_negative_difference_is_never_shown_nor_a_reset(
    hass: HomeAssistant, path: str
) -> None:
    sensor = _sensor(hass, _collection(path, 10.0, 9.0, 2.0))
    assert sensor.native_value is None
    # A rounding edge is zero, never a negative number.
    assert _sensor(hass, _collection(path, 0.3, 0.1, 0.2)).native_value == 0.0
    # A net total: a decrease is not a new meter cycle.
    assert sensor.state_class is SensorStateClass.TOTAL


def test_cooling_and_other_collections_have_no_environmental_energy() -> None:
    assert _environmental(_collection(COOLING, 20.0, 10.0, 0.0)) == []
    hs = _collection("/heatSources/hs1/emon/totalConsumption", 20.0, 10.0, 2.0)
    assert _environmental(hs) == []
    # Nor does the counter selection wait for it, where no entity shows it.
    cooling = counter_vector(_collection(COOLING, 20.0, 10.0, 0.0)) or {}
    assert "=environmental_energy" not in cooling
    for path in (TOTAL, *MODES.values()):
        vector = counter_vector(_collection(path, 20.0, 10.0, 2.0)) or {}
        assert "=environmental_energy" in vector


@pytest.mark.parametrize("path", [TOTAL, *MODES.values()])
def test_an_incomplete_collection_has_no_entity(path: str) -> None:
    incomplete = Resource(
        path=path,
        values=({"compressor": 12.0}, {"outputProduced": 30.0}),
        metadata=ResourceMetadata(resource_type="emonValue"),
    )
    assert _environmental(incomplete) == []


def test_only_the_total_is_enabled_by_default() -> None:
    total = _environmental(_collection(TOTAL, 2.0, 1.0, 0.0))[0]
    assert total.entity_registry_enabled_default  # type: ignore[attr-defined]
    for path in MODES.values():
        description = _environmental(_collection(path, 2.0, 1.0, 0.0))[0]
        assert description.entity_registry_enabled_default is False  # type: ignore[attr-defined]


def test_the_difference_needs_every_reported_part() -> None:
    assert environmental_energy({"outputProduced": 5.0, "compressor": 1.0}) is None
    both = {"outputProduced": 5.0, "compressor": 1.0, "eheater": 1.0}
    assert environmental_energy(both) == pytest.approx(3.0)
    assert module.has_environmental_energy(TOTAL)
    assert not module.has_environmental_energy(COOLING)


# ------------------------------------------------------------------ defaults
PLANT = {
    "total_heat": 1.0,
    "total_compressor": 1.0,
    "total_eheater": 1.0,
    "ch_heat": 1.0,
    "ch_compressor": 1.0,
    "ch_eheater": 1.0,
    "dhw_heat": 1.0,
    "cooling_heat": 1.0,
    "outdoor_temperature": 1.0,
    "total_starts": 1.0,
}


def test_twelve_new_default_entities() -> None:
    """D-DEF: the factor of the total for yesterday, the month, the previous
    month and the previous year; heat and electricity of the total for the
    month, the previous month, the year and the previous year."""
    assert {period for period, _ in ENABLED_FACTORS} == set(FP)
    assert {mode for _, mode in ENABLED_FACTORS} == {"total"}
    new_factors = {period for period, _ in ENABLED_FACTORS} - {
        FP.TODAY,
        FP.CURRENT_YEAR,
        FP.SINCE_COMMISSIONING,
    }
    assert new_factors == {
        FP.YESTERDAY,
        FP.CURRENT_MONTH,
        FP.PREVIOUS_MONTH,
        FP.PREVIOUS_YEAR,
    }
    enabled = {
        (spec.measure.key, spec.period)
        for spec in entity_specs(measures(PLANT))
        if spec.enabled_default
    }
    assert enabled == {
        (measure, period)
        for measure in ("total_heat", "total_electricity")
        for period in Period
    }
    assert len(enabled - {(m, Period.YESTERDAY) for m, _ in enabled}) == 8


def test_existing_entries_keep_their_state_when_defaults_change(
    hass: HomeAssistant,
) -> None:
    """Registry entries made before (disabled by the integration) are not
    enabled by the new defaults: nothing enables the factor, the recording
    or the environmental entity of a mode."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    factor = (
        "gateway-one:heatSources:emon:totalConsumption:"
        "calculated_performance_factor_current_month"
    )
    specs = entity_specs(measures(PLANT))
    recording = unique_id(
        "gateway-one",
        next(
            s
            for s in specs
            if s.period is Period.CURRENT_MONTH and s.measure.key == "total_heat"
        ),
    )
    mode = "gateway-one:heatSources:emon:chConsumption:environmental_energy"
    ids = {
        uid: registry.async_get_or_create(
            "sensor",
            DOMAIN,
            uid,
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        ).entity_id
        for uid in (factor, recording, mode)
    }
    coordinator = SimpleNamespace(
        gateway=Gateway("gateway-one"),
        resources={
            TOTAL: _collection(TOTAL, 20.0, 10.0, 2.0),
            MODES["ch"]: _collection(MODES["ch"], 20.0, 10.0, 2.0),
        },
    )
    _enable_new_default_entities(hass, entry, (coordinator,))  # type: ignore[arg-type]
    for uid, entity_id in ids.items():
        state = registry.async_get(entity_id)
        assert state is not None
        assert state.disabled_by is er.RegistryEntryDisabler.INTEGRATION, uid


# ------------------------------------------------------------- measured load
async def _cloud_reads(hass: HomeAssistant, wanted: list[str]) -> list[int]:
    """Cloud reads of the start run and of 36 hourly runs, no local access."""
    clock = Clock(NOW)
    cloud = Cloud()
    rec = await manager(hass, clock, [], cloud, entry=MockConfigEntry(domain=DOMAIN))
    rec.acquire()  # the performance factors read the energy recordings
    for item in wanted:
        rec.request(item)
    await rec.async_run("start")
    per_run = [len(cloud.calls)]
    for _ in range(36):
        done = len(cloud.calls)
        clock.now += timedelta(hours=1)
        await rec.async_run("hourly")
        per_run.append(len(cloud.calls) - done)
    return per_run


async def test_the_extra_cloud_reads_of_the_new_defaults(
    hass: HomeAssistant,
) -> None:
    """Without a local access the new entities cost today's value of the
    series of the current month and year once an hour. The previous month
    and year add nothing: they belong to the back-fill the performance
    factors already cause."""
    old = [key(m, Period.YESTERDAY) for m in ("total_heat", "total_electricity")]
    new = [
        key(m, period) for m in ("total_heat", "total_electricity") for period in Period
    ]
    before = await _cloud_reads(hass, old)
    after = await _cloud_reads(hass, new)
    extra = [a - b for a, b in zip(after, before, strict=True)]
    assert extra[0] == 0  # the start run is unchanged
    # The test plant answers heat and compressor (no heater series): the
    # extra is today of those two, every hour, nothing else.
    assert set(extra[1:]) <= {0, 1, 2}
    assert sum(extra[1:25]) <= 2 * 24
    assert max(after) <= 24  # inside the budget of a run
