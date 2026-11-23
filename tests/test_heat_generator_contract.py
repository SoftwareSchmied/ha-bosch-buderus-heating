"""The heat generator sensor as an interface for cards (contract 1).

Every plant case and every fixture plant keeps to the published schema; the
own K40 keeps its state and its previous attributes and writes no more often;
a restart writes nothing new; plants without a named module get the sensor
from their components or their plant kind; and the plant model adds hybrid
evidence (a hybrid manager at the hybrid place, a heat pump next to a boiler)
only additively, through the burner share.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import AsyncGenerator, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import (
    burner_share,
    heat_generator,
    performance_history,
    system_components,
)
from custom_components.bosch_buderus_heating.component_rows import (
    ComponentRow,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.derived_store import DerivedStore
from custom_components.bosch_buderus_heating.device import is_rrc_plant
from custom_components.bosch_buderus_heating.heat_generator import (
    interpret,
    rows_from_cloud,
)
from custom_components.bosch_buderus_heating.heat_generator_sensor import (
    CONTRACT_KEYS,
    MAX_COMPONENTS,
    PREVIOUS_KEYS,
    SENSOR_EVIDENCE,
    STATE_BASES,
    BoschBuderusHeatGeneratorSensor,
    PlantView,
    component_entries,
    component_state,
    contract_attributes,
    plant_view,
    track_heat_generator_sensor,
)
from custom_components.bosch_buderus_heating.heat_source_kind import classify
from custom_components.bosch_buderus_heating.local_only import (
    PLANT_HYBRID_EVIDENCE,
    hybrid_evidence,
)
from custom_components.bosch_buderus_heating.performance_sensors import _candidates
from custom_components.bosch_buderus_heating.plant_detection import (
    EVIDENCE_CODES,
    PlantResult,
    resolve,
)
from custom_components.bosch_buderus_heating.plant_inputs import (
    ShownComponents,
    components,
    current_plant,
    track_plant,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    BatchItemResult,
    RateLimited,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.recordings.gating import (
    heat_pump_source,
)
from custom_components.bosch_buderus_heating.system_components import (
    PLACE_KEYS,
    ROLE_VALUES,
    load_table,
)
from tests import test_derived_values
from tests.ha_test_helpers import mock_restore_cache_with_extra_data
from tests.helpers.heat_generator_contract import check_contract, schema
from tests.system_components_variants import (
    each_name_entry_alone,
    each_name_entry_removed,
)
from tests.test_coordinator_outages import clock
from tests.test_derived_values import CH, DHW, TOTAL, Plant
from tests.test_heat_generator import INDOOR, OUTDOOR, OWN_CLOUD, _platform
from tests.test_perf_hybrid_helpers import HS2_TOTAL, heat_pump
from tests.test_plant_detection import case_names, inputs_of, load_case
from tests.test_plant_inputs import (
    GATEWAY,
    NOW,
    _answers,
    _coordinator,
    _cycle,
    _local,
    _resources,
    _restart,
    _start,
)

UNIQUE_ID = f"{GATEWAY}:heat_generator"
# The own K40: its type values with the module list of the heat generator
# tests (product names and hardware types as the real plant reports them).
K40_CASE = "k40_own"


@pytest.fixture(autouse=True)
async def _stop_engines(hass: HomeAssistant) -> AsyncGenerator[None]:
    yield
    for engine in hass.data.get(performance_history._ENGINES, {}).values():
        engine.stop()


@pytest.fixture(autouse=True)
async def _stop_plant_coordinators() -> AsyncGenerator[None]:
    """Stop the coordinators that ``Plant`` of the derived value tests
    creates (their refresh timers outlive the test otherwise)."""
    yield
    while test_derived_values._COORDINATORS:
        await test_derived_values._COORDINATORS.pop().async_shutdown()


# ---------------------------------------------------------------- helpers


def _own_k40() -> dict[str, Any]:
    case = copy.deepcopy(load_case(K40_CASE))
    case["cloud"] = copy.deepcopy(list(OWN_CLOUD))
    case["local"] = None
    return case


def _tracked(coordinator: Any) -> list[BoschBuderusHeatGeneratorSensor]:
    added: list[BoschBuderusHeatGeneratorSensor] = []
    track_heat_generator_sensor(
        SimpleNamespace(async_on_unload=Mock()), coordinator, added.extend
    )
    return added


async def _sensor(
    hass: HomeAssistant, coordinator: Any
) -> BoschBuderusHeatGeneratorSensor | None:
    added = _tracked(coordinator)
    if not added:
        return None
    await _platform(hass, coordinator.config_entry).async_add_entities(added)
    await hass.async_block_till_done()
    return added[0]


def _state(hass: HomeAssistant, entity: BoschBuderusHeatGeneratorSensor) -> State:
    state = hass.states.get(entity.entity_id)
    assert state is not None
    return state


def _attributes(state: State) -> dict[str, Any]:
    return {k: v for k, v in state.attributes.items() if k != "friendly_name"}


def _changes(hass: HomeAssistant, entity_id: str) -> list[Event]:
    """State changes of one entity (a write without a change is none)."""
    seen: list[Event] = []

    def listener(event: Event) -> None:
        if event.data["entity_id"] == entity_id:
            seen.append(event)

    hass.bus.async_listen(EVENT_STATE_CHANGED, listener)
    return seen


def _beta5_attributes(kind: str | None = "air") -> dict[str, Any]:
    """What 0.9.3-beta.5 showed for the own K40 (the same code path)."""
    shown = interpret(rows_from_cloud(OWN_CLOUD), "cloud")
    assert shown is not None
    return {**shown.attributes(), "heat_source_type": kind}


# ------------------------------------------------------- schema and contract


def test_the_schema_names_the_values_of_the_code() -> None:
    """The published schema and the code use the same keys and values."""
    published = schema()
    assert published["required"] == [*PREVIOUS_KEYS, *CONTRACT_KEYS]
    assert published["properties"]["state_basis"]["enum"] == [*STATE_BASES, None]
    properties = published["properties"]
    assert properties["contract_version"] == {"const": 1}
    assert properties["plant_kind_evidence"]["items"]["enum"] == list(SENSOR_EVIDENCE)
    assert set(EVIDENCE_CODES) - set(SENSOR_EVIDENCE) == {"stored"}
    component = published["$defs"]["component"]["properties"]
    assert component["place"]["enum"] == [*PLACE_KEYS, None]
    assert component["role"]["enum"] == [*ROLE_VALUES, None]
    assert published["properties"]["components"]["maxItems"] == MAX_COMPONENTS


def test_the_contract_check_finds_a_missing_or_changed_key() -> None:
    """M15: without contract_version, or with a changed type, the check fails."""
    attributes = contract_attributes(
        interpret(rows_from_cloud(OWN_CLOUD), "cloud"), "air", PlantView()
    )
    check_contract(attributes)
    for change in (
        lambda a: a.pop("contract_version"),
        lambda a: a.update(contract_version=2),
        lambda a: a.update(state_basis="name"),
        lambda a: a.update(components=[{"place": "x"}]),
        lambda a: a.update(extra=1),
    ):
        broken = copy.deepcopy(attributes)
        change(broken)
        with pytest.raises(AssertionError):
            check_contract(broken)


@pytest.mark.parametrize("name", case_names())
async def test_contract_1_on_every_plant_case(hass: HomeAssistant, name: str) -> None:
    """Each constructed plant: the sensor (when it is created) keeps to the
    schema, and its plant kind is the plant model's."""
    case = load_case(name)
    if case.get("no_block"):
        pytest.skip("no plant model for this kind of device")
    local = [_local(case["local"])] if case["local"] else []
    coordinator = _coordinator(hass, case, local=local)
    tracker = track_plant(coordinator.config_entry, coordinator)
    await hass.async_block_till_done()
    entity = await _sensor(hass, coordinator)
    if entity is not None:
        attributes = _attributes(_state(hass, entity))
        check_contract(attributes)
        plant = current_plant(coordinator)
        assert attributes["plant_kind"] == (plant.plant_kind if plant else None)
        assert attributes["plant_kind_evidence"] == (
            [code for code in plant.evidence if code != "stored"] if plant else []
        )
        # Never a version or a serial number.
        rendered = json.dumps(attributes)
        for row in case["cloud"] or case["local"] or []:
            for key in ("Ver", "ModuleSerialNumber", "ProductSerialNumber", "No"):
                if isinstance(row.get(key), str) and len(row[key]) > 3:
                    assert row[key] not in rendered, (name, key)
    else:
        plant = current_plant(coordinator)
        assert (
            tracker is None
            or is_rrc_plant(coordinator)
            or plant is None
            or plant.plant_kind not in ("heat_pump", "hybrid", "boiler")
        )
    await coordinator.async_shutdown()


def test_the_attributes_stay_small() -> None:
    """32 components with the longest names and 16 heat generators stay below
    the recorder limit; the component list is not recorded at all."""
    table = load_table()
    assert table is not None
    longest = max(
        (item for item in table.components.values() if item.name),
        key=lambda item: len(item.name),
    )
    rows = tuple(
        ComponentRow(longest.id, tok, product_name="P" * 64)
        for tok in range(100, 100 + 40)
    )
    view = plant_view(
        PlantResult("hybrid", "gas", False, tuple(SENSOR_EVIDENCE)),
        ShownComponents("cloud", rows, False, table.table_version),
        table,
    )
    assert len(view.components) == MAX_COMPONENTS
    names = [(f"{index:02d}" + "N" * 253, "H" * 64) for index in range(16)]
    shown = interpret(
        [heat_generator.ModuleRow(name, hardware) for name, hardware in names],
        "cloud",
    )
    attributes = contract_attributes(shown, "air", view)
    check_contract(attributes)
    recorded = {k: v for k, v in attributes.items() if k != "components"}
    assert len(json.dumps(recorded).encode()) < 16384
    realistic = contract_attributes(
        interpret(rows_from_cloud(OWN_CLOUD), "cloud"), "air", view
    )
    assert len(json.dumps(realistic).encode()) < 16384
    assert "components" in BoschBuderusHeatGeneratorSensor._unrecorded_attributes


# ------------------------------------------------------------- the own K40


async def test_the_own_k40_keeps_state_and_previous_attributes(
    hass: HomeAssistant,
) -> None:
    """I2: state and the seven previous attributes byte for byte; the new
    keys show the plant model; the gateway's rows are left out (M39)."""
    coordinator = _coordinator(hass, _own_k40())
    await _start(hass, coordinator)
    entity = await _sensor(hass, coordinator)
    assert entity is not None
    state = _state(hass, entity)
    assert state.state == INDOOR
    attributes = _attributes(state)
    # The heat source type is unchanged code; this plant has no evidence.
    assert attributes["heat_source_type"] == classify(coordinator).kind
    before = _beta5_attributes(attributes["heat_source_type"])
    assert json.dumps({k: attributes[k] for k in before}, sort_keys=True) == (
        json.dumps(before, sort_keys=True)
    )
    table = load_table()
    assert table is not None
    assert {k: attributes[k] for k in CONTRACT_KEYS} == {
        "contract_version": 1,
        "state_basis": "product_name",
        "plant_kind": "heat_pump",
        "plant_kind_evidence": [
            "reported_heat_pump_type",
            "reported_heat_source_type",
            "reported_system_type",
        ],
        "fuel": None,
        "cascade": False,
        "components": [
            {
                "place": "heat_generator_control",
                "place_number": 8,
                "type_number": 8,
                "type_name": table.component(8).name,  # type: ignore[union-attr]
                "role": "heat_source",
                "basis": table.component(8).basis,  # type: ignore[union-attr]
                "product_name": INDOOR,
            },
            {
                "place": "heat_generator_control",
                "place_number": 8,
                "type_number": 65281,
                "type_name": table.component(65281).name,  # type: ignore[union-attr]
                "role": "outdoor_unit",
                "basis": table.component(65281).basis,  # type: ignore[union-attr]
                "product_name": OUTDOOR,
            },
            {
                "place": "system_controller",
                "place_number": 16,
                "type_number": 253,
                "type_name": table.component(253).name,  # type: ignore[union-attr]
                "role": "system_controller",
                "basis": table.component(253).basis,  # type: ignore[union-attr]
                "product_name": None,
            },
        ],
        "components_source": "cloud",
        "components_table_version": table.table_version,
    }
    check_contract(attributes)
    # The own K40 is no hybrid system (M17, M19).
    assert not burner_share.hybrid_detected(coordinator)
    await coordinator.async_shutdown()


async def test_one_change_after_the_update_and_none_after_a_restart(
    hass: HomeAssistant,
) -> None:
    """M40: after the update the state changes once (the new attributes),
    then never again while nothing changes; a restart (from the stored
    inventory, without the module list) shows the same state at once."""
    case = _own_k40()
    client = AsyncMock()
    coordinator = _coordinator(hass, case, client=client)
    registry = er.async_get(hass)
    entity_id = registry.async_get_or_create(
        "sensor", DOMAIN, UNIQUE_ID, config_entry=coordinator.config_entry
    ).entity_id
    shown = interpret(rows_from_cloud(OWN_CLOUD), "cloud")
    assert shown is not None
    beta5_restore = {"heat_generators": shown.to_data(), "heat_source_type": "air"}
    mock_restore_cache_with_extra_data(
        hass, [(State(entity_id, INDOOR, _beta5_attributes()), beta5_restore)]
    )
    changes = _changes(hass, entity_id)
    await _start(hass, coordinator)
    with clock() as now:
        entity = await _sensor(hass, coordinator)
        assert entity is not None and entity.entity_id == entity_id
        first = _state(hass, entity)
        assert len(changes) == 1
        assert _attributes(first) != _beta5_attributes()
        assert set(_attributes(first)) - set(_beta5_attributes()) == set(CONTRACT_KEYS)
        for _ in range(3):
            client.get_resources_bulk.return_value = _answers(coordinator)
            await _cycle(coordinator, now)
            await hass.async_block_till_done()
        assert len(changes) == 1
        # M40: no write at all while nothing changes.
        with patch.object(
            entity, "async_write_ha_state", wraps=entity.async_write_ha_state
        ) as write:
            for _ in range(2):
                client.get_resources_bulk.return_value = _answers(coordinator)
                await _cycle(coordinator, now)
                await hass.async_block_till_done()
            assert write.call_count == 0
        restore = entity.extra_restore_state_data.as_dict()
        # M16: the kept data are those of 0.9.3-beta.5, byte for byte.
        assert json.dumps(restore, sort_keys=True) == json.dumps(
            beta5_restore, sort_keys=True
        )
        assert json.dumps(restore, sort_keys=True) == json.dumps(
            {
                "heat_generators": {
                    "rows": [[INDOOR, "XCU_THH"], [OUTDOOR, "XCU_SRH"]],
                    "source": "cloud",
                },
                "heat_source_type": "air",
            },
            sort_keys=True,
        )
        before = _state(hass, entity)
        await entity.async_remove()
    await coordinator.async_shutdown()

    # A restart: the saved values and the restore data of the entity as they
    # were, the cloud from the stored inventory (no module list yet).
    await _restart(hass)
    mock_restore_cache_with_extra_data(hass, [(before, restore)])
    changes.clear()
    client = AsyncMock()
    again = _coordinator(hass, case, client=client, info=False)
    await _start(hass, again)
    with clock() as now:
        entity = await _sensor(hass, again)
        assert entity is not None
        after = _state(hass, entity)
        assert (after.state, _attributes(after)) == (before.state, _attributes(before))
        changes.clear()
        # The first cycle reads the module list again.
        again.resources = _resources(case)
        for _ in range(2):
            client.get_resources_bulk.return_value = _answers(again)
            await _cycle(again, now)
            await hass.async_block_till_done()
        assert changes == []
    await again.async_shutdown()


async def test_the_first_state_waits_for_the_stored_row(hass: HomeAssistant) -> None:
    """M21: added at once after the plant model is started (as the sensor
    platform does), the first state already has the plant model's part;
    nothing changes afterwards."""
    case = _own_k40()
    first = _coordinator(hass, case)
    await _start(hass, first)
    await first.async_shutdown()
    await _restart(hass)
    again = _coordinator(hass, case, info=False)
    # Registered before the restart.
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, UNIQUE_ID, config_entry=again.config_entry
    )
    seen: list[dict[str, Any]] = []

    def listener(event: Event) -> None:
        if event.data["entity_id"].endswith("heat_generator"):
            seen.append(dict(event.data["new_state"].attributes))

    hass.bus.async_listen(EVENT_STATE_CHANGED, listener)
    original = DerivedStore.async_load

    async def slow(store: DerivedStore) -> None:
        # Reading the file takes a moment on a real system.
        await asyncio.sleep(0.05)
        await original(store)

    with patch.object(DerivedStore, "async_load", slow):
        tracker = track_plant(again.config_entry, again)
        assert tracker is not None and not tracker.loaded
        entity = await _sensor(hass, again)
        assert entity is not None
        await hass.async_block_till_done()
    assert len(seen) == 1
    assert seen[0]["plant_kind"] == "heat_pump"
    assert seen[0]["components_source"] == "cloud"
    assert len(seen[0]["components"]) == 3
    await again.async_shutdown()


async def test_a_restart_of_a_component_hybrid_changes_nothing(
    hass: HomeAssistant,
) -> None:
    """After a restart the plant model rests on its stored row (evidence
    ``stored`` in the diagnostics); the sensor shows the same as before."""
    case = load_case("issue_138_xcu_hybrid")
    coordinator = _coordinator(hass, case)
    await _start(hass, coordinator)
    entity = await _sensor(hass, coordinator)
    assert entity is not None
    before = _state(hass, entity)
    assert before.attributes["plant_kind"] == "hybrid"
    restore = entity.extra_restore_state_data.as_dict()
    await entity.async_remove()
    await coordinator.async_shutdown()
    await _restart(hass)
    mock_restore_cache_with_extra_data(hass, [(before, restore)])
    again = _coordinator(hass, case, info=False)
    await _start(hass, again)
    plant = current_plant(again)
    assert plant is not None and "stored" in plant.evidence
    entity = await _sensor(hass, again)
    assert entity is not None
    after = _state(hass, entity)
    assert (after.state, after.attributes) == (before.state, before.attributes)
    await again.async_shutdown()


async def test_outages_in_real_cycles_change_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """5xx, 429, timeouts and a lost sign-in: the sensor keeps what it
    shows and writes nothing (real coordinator cycles)."""
    client = AsyncMock()
    coordinator = _coordinator(hass, _own_k40(), client=client)
    with (
        clock() as now,
        patch.object(coordinator.config_entry, "async_start_reauth"),
    ):
        await _start(hass, coordinator)
        entity = await _sensor(hass, coordinator)
        assert entity is not None
        client.get_resources_bulk.return_value = _answers(coordinator)
        await _cycle(coordinator, now)
        before = _state(hass, entity)
        changes = _changes(hass, entity.entity_id)
        paths = tuple(coordinator.resources)
        failures: list[dict[str, Any]] = [
            {
                "return_value": tuple(
                    BatchItemResult(GATEWAY, p, 503, server_status=503) for p in paths
                )
            },
            {"side_effect": ServiceUnavailable(503)},
            {"side_effect": RateLimited(60)},
            {"side_effect": TimeoutError()},
            {"side_effect": AuthenticationError("signed out")},
        ]
        for failure in failures:
            client.get_resources_bulk.reset_mock(return_value=True, side_effect=True)
            client.get_resource.side_effect = ServiceUnavailable(503)
            for key, value in failure.items():
                setattr(client.get_resources_bulk, key, value)
            freezer.tick(timedelta(days=1))
            await _cycle(coordinator, now, seconds=86400)
            await hass.async_block_till_done()
            assert not coordinator.last_update_success
            after = _state(hass, entity)
            assert (after.state, after.attributes) == (before.state, before.attributes)
        assert changes == []
    await coordinator.async_shutdown()


# ------------------------------------------------ plants without a named module


async def test_an_icom_heat_pump_gets_the_sensor_with_its_control_unit(
    hass: HomeAssistant,
) -> None:
    """#467 (E1): no product name; the state is the name of the control
    unit at the heat generator's place, the plant kind comes from the heat
    source type, never from the type number (M22). The control unit (172 at
    place 8) and the system controller (158 at place 16) were seen on an iCom
    gateway and carry their roles; the remote control (165) is a name only."""
    case = load_case("issue_467_icom_heat_pump")
    coordinator = _coordinator(hass, case)
    await _start(hass, coordinator)
    entity = await _sensor(hass, coordinator)
    assert entity is not None
    state = _state(hass, entity)
    assert state.state == "CUHP"
    attributes = _attributes(state)
    check_contract(attributes)
    assert attributes["state_basis"] == "component_type"
    assert attributes["heat_generators"] == []
    assert attributes["known"] is False
    assert attributes["primary"] == dict.fromkeys(
        ("family", "construction", "power_kw", "brand_family", "outdoor_power_kw")
    )
    assert attributes["plant_kind"] == "heat_pump"
    assert attributes["plant_kind_evidence"] == ["reported_heat_source_type"]
    assert attributes["heat_source_type"] is None
    table = load_table()
    assert table is not None
    assert [
        (item["place_number"], item["type_number"], item["type_name"], item["basis"])
        for item in attributes["components"]
    ] == [
        (tok, type_id, table.component(type_id).name, basis)  # type: ignore[union-attr]
        for tok, type_id, basis in (
            (8, 172, "observed"),
            (16, 158, "observed"),
            (56, 165, "name"),
        )
    ]
    assert attributes["components"][0]["type_name"] == "CUHP"
    assert [item["role"] for item in attributes["components"]] == [
        "heat_source",
        "system_controller",
        None,
    ]
    assert not burner_share.hybrid_detected(coordinator)
    await coordinator.async_shutdown()


async def test_an_icom_without_a_heat_source_type_still_shows_its_control_unit(
    hass: HomeAssistant,
) -> None:
    case = load_case("issue_467_icom_without_type")
    coordinator = _coordinator(hass, case)
    await _start(hass, coordinator)
    entity = await _sensor(hass, coordinator)
    assert entity is not None
    attributes = _attributes(_state(hass, entity))
    assert attributes["plant_kind"] is None
    assert attributes["state_basis"] == "component_type"
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    "rows",
    [
        [{"Id": "189", "Tok": "1"}, {"Id": "240", "Tok": "5"}],
        [{"Id": "253", "Tok": "16"}, {"Id": "17", "Tok": "72"}],
        [{"Id": "0", "Tok": "8"}, {"Id": "17", "Tok": "8"}],
    ],
    ids=["icom_controller", "hmi_and_gateway", "invalid_and_gateway_at_place_8"],
)
async def test_without_evidence_there_is_no_sensor(
    hass: HomeAssistant, rows: list[dict[str, str]]
) -> None:
    """M13: an iCom gateway without a component at the heat generator's
    place and without a known plant kind gets no entity."""
    case = {
        "gateway": {"device_type": "icom", "firmware": "4.1.2"},
        "cloud": rows,
        "local": None,
        "values": {"/heatSources/hs1/type": "BOILER_GAS", "/system/bus": "EMS2_0"},
    }
    coordinator = _coordinator(hass, case)
    await _start(hass, coordinator)
    assert current_plant(coordinator) is not None
    assert _tracked(coordinator) == []
    coordinator.async_update_listeners()
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, UNIQUE_ID) is None
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    ("values", "created"),
    [
        ({"/heatSources/hs1/type": "GasBoiler"}, True),
        ({"/heatSources/hs1/type": "Heatpump"}, True),
        ({"/system/type": "hybman"}, True),
        ({"/heatSources/hs1/type": "No_Appliance"}, False),
        ({"/heatSources/hs1/type": "eBoiler"}, False),
    ],
)
async def test_a_known_plant_kind_alone_gives_the_sensor(
    hass: HomeAssistant, values: dict[str, str], created: bool
) -> None:
    """A boiler, heat pump or hybrid system without a module list."""
    case = {
        "gateway": {"device_type": "k30", "firmware": "08.02.02"},
        "cloud": None,
        "local": None,
        "values": values,
    }
    coordinator = _coordinator(hass, case)
    await _start(hass, coordinator)
    added = _tracked(coordinator)
    assert bool(added) is created
    if created:
        entity = await _sensor(hass, coordinator)
        assert entity is not None
        state = _state(hass, entity)
        assert state.state == "unknown"
        attributes = _attributes(state)
        check_contract(attributes)
        assert attributes["state_basis"] is None
        assert attributes["components"] == []
    await coordinator.async_shutdown()


async def test_a_room_controller_never_gets_the_sensor(hass: HomeAssistant) -> None:
    case = load_case("issue_122_rrc_hybrid")
    coordinator = _coordinator(hass, case)
    with patch(
        "custom_components.bosch_buderus_heating.heat_generator_sensor.is_rrc_plant",
        return_value=True,
    ):
        await _start(hass, coordinator)
        assert _tracked(coordinator) == []
    await coordinator.async_shutdown()


def test_the_state_of_a_component_never_follows_the_order() -> None:
    """A role first, then the smallest type number; never the order."""
    table = load_table()
    rows = (
        ComponentRow(172, 8),
        ComponentRow(8, 8),
        ComponentRow(17, 8),
        ComponentRow(0, 8),
    )
    assert component_state(rows, table) == table.component(8).name  # type: ignore[union-attr]
    assert component_state(tuple(reversed(rows)), table) == component_state(rows, table)
    assert component_state((ComponentRow(195, 8), ComponentRow(172, 8)), table) == (
        "CUHP"
    )
    assert component_state(rows, None) is None
    assert component_entries(rows, None) == ()


# ----------------------------------------------- hybrid evidence of the model


@pytest.mark.parametrize(
    ("name", "hybrid"),
    [
        ("issue_138_xcu_hybrid", True),
        ("issue_138_hybrid_manager_new", True),
        ("combined_types", True),
        ("firmware_k40_below", False),
        ("firmware_k30_below", False),
        ("icom_hybrid_place", False),
        ("issue_467_icom_heat_pump", False),
        ("issue_130_boiler", False),
        ("k40_own", False),
    ],
)
async def test_the_hybrid_manager_counts_only_with_its_gates(
    hass: HomeAssistant, name: str, hybrid: bool
) -> None:
    """M18: a hybrid manager at the hybrid place counts only within its
    firmware and version limits and only on K30/K40 gateways; type 172 never
    counts; a boiler stays a boiler (M20)."""
    case = load_case(name)
    coordinator = _coordinator(hass, case)
    before = burner_share.hybrid_detected(coordinator)
    await _start(hass, coordinator)
    plant = current_plant(coordinator)
    assert plant is not None
    assert burner_share.hybrid_detected(coordinator) is (hybrid or before)
    assert (plant.plant_kind == "hybrid") is burner_share.hybrid_detected(coordinator)
    await coordinator.async_shutdown()


@pytest.mark.parametrize("drop", ["version", "firmware"])
async def test_a_hybrid_manager_below_its_limits_alone_is_no_hybrid(
    hass: HomeAssistant, drop: str
) -> None:
    """M18: without the paths of a hybrid control, a hybrid manager below
    its version limit or on a gateway below its firmware limit is no
    evidence."""
    case = copy.deepcopy(load_case("issue_138_hybrid_manager_new"))
    case.pop("named_paths")
    if drop == "version":
        case["cloud"][1]["Ver"] = "06.00.99"
    else:
        case["gateway"]["firmware"] = "08.02.01"
    coordinator = _coordinator(hass, case)
    await _start(hass, coordinator)
    plant = current_plant(coordinator)
    assert plant is not None and plant.plant_kind == "boiler"
    assert "component_rule_skipped" in plant.conflicts
    assert not burner_share.hybrid_detected(coordinator)
    passing = copy.deepcopy(load_case("issue_138_hybrid_manager_new"))
    passing.pop("named_paths")
    other = _coordinator(hass, passing, gateway_id="gateway-two")
    await _start(hass, other)
    assert burner_share.hybrid_detected(other)
    await coordinator.async_shutdown()
    await other.async_shutdown()


def test_the_model_evidence_only_adds() -> None:
    """Only the two codes of the model count, and nothing is ever removed."""
    readings = [("/heatSources/hybrid/control", None)]
    assert hybrid_evidence(readings, ("reported_system_type", "stored")) == (
        "hybrid_resources",
    )
    assert hybrid_evidence([], ("reported_heat_pump_type",)) == ()
    assert hybrid_evidence(readings, PLANT_HYBRID_EVIDENCE) == (
        "heat_source_types_combined",
        "hybrid_manager_component",
        "hybrid_resources",
    )
    assert hybrid_evidence([], ()) == ()


async def test_without_a_stored_result_a_start_is_as_before(
    hass: HomeAssistant,
) -> None:
    """M21: until the stored row is read there is no result and the hybrid
    evidence is that of before; once read, a component hybrid counts at
    once (no flicker after a restart)."""
    case = load_case("issue_138_xcu_hybrid")
    first = _coordinator(hass, case)
    await _start(hass, first)
    assert burner_share.hybrid_detected(first)
    await first.async_shutdown()
    await _restart(hass)

    # A start from the stored inventory: no module list yet.
    again = _coordinator(hass, case, info=False)
    assert not burner_share.hybrid_detected(again)
    tracker = track_plant(again.config_entry, again)
    assert tracker is not None
    await hass.async_block_till_done()
    assert tracker.loaded
    assert burner_share.hybrid_detected(again)
    await again.async_shutdown()

    # Without a stored row the same start behaves as before.
    fresh = _coordinator(hass, case, info=False, gateway_id="gateway-two")
    await _start(hass, fresh)
    assert not burner_share.hybrid_detected(fresh)
    await fresh.async_shutdown()


def _component_hybrid(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, *, manager: bool
) -> Plant:
    """A heat pump with a boiler, shown only by its component list: the
    gateway's counters without electricity and without a burner part, the
    heat pump's own counters under hs2."""
    plant = Plant(hass, freezer)
    for path in (TOTAL, CH, DHW):
        plant.cloud(path, values=({"outputProduced": 900.0},))
    heat_pump(plant, HS2_TOTAL, heat=1500.0, electricity=400.0)
    plant.cloud("/gateway/versionFirmware", "16.00.09")
    rows: list[dict[str, str]] = [{"Id": "195", "Tok": "8"}, {"Id": "17", "Tok": "72"}]
    if manager:
        rows.append({"Id": "16", "Tok": "83"})
    plant.cloud("/system/info", values=tuple(rows), kind="systeminfo")
    return plant


@pytest.mark.parametrize("manager", [True, False])
async def test_a_component_hybrid_without_a_burner_part_gets_the_heat_pump_factors(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, manager: bool
) -> None:
    """M19: a hybrid system shown only by its hybrid manager has the burner
    share without a burner part; its heat pump's factors appear and the
    heat pump's own recordings are read (hs2). Without the hybrid manager
    nothing changes."""
    plant = _component_hybrid(hass, freezer, manager=manager)
    coordinator = plant.coordinator
    keys_before = {key for key, _ in _candidates(coordinator)}
    assert not any(key.startswith("factor:") for key in keys_before)
    assert not burner_share.burner_share(coordinator, TOTAL)
    assert heat_pump_source(coordinator) is None
    track_plant(coordinator.config_entry, coordinator)
    await hass.async_block_till_done()
    plant_result = current_plant(coordinator)
    assert plant_result is not None
    assert (plant_result.plant_kind == "hybrid") is manager
    keys = {key for key, _ in _candidates(coordinator)}
    factors = {key for key in keys if key.startswith("factor:")}
    assert bool(factors) is manager
    if manager:
        assert {key.split(":")[1] for key in factors} == {"total"}
        assert "hybrid_manager_component" in plant_result.evidence
    assert burner_share.burner_share(coordinator, TOTAL) is manager
    assert heat_pump_source(coordinator) == ("hs2" if manager else None)


async def test_the_diagnostics_name_the_model_evidence(hass: HomeAssistant) -> None:
    """The hybrid block of the diagnostics follows the same evidence."""
    from custom_components.bosch_buderus_heating.diagnostics import (
        _hybrid_volume_flow,
    )
    from tests.test_local_signal_units_and_states import _binding, _runtime

    hybrid = _coordinator(hass, load_case("issue_138_xcu_hybrid"))
    own = _coordinator(hass, _own_k40(), gateway_id="gateway-two")
    runtime = _runtime(
        {"a": _binding("one", "pointt"), "b": _binding("two", "pointt")},
        sources={"a": hybrid, "b": own},
    )
    before = _hybrid_volume_flow(runtime, {"one": 1, "two": 2})
    assert [item["hybrid_detected"] for item in before] == [False, False]
    await _start(hass, hybrid)
    await _start(hass, own)
    report = _hybrid_volume_flow(runtime, {"one": 1, "two": 2})
    assert [(item["hybrid_detected"], item["evidence"]) for item in report] == [
        (True, ["hybrid_manager_component"]),
        (False, []),
    ]
    await hybrid.async_shutdown()
    await own.async_shutdown()


async def test_a_change_of_source_never_shows_fewer_components(
    hass: HomeAssistant,
) -> None:
    """M44: a newer local list with the same components and one gateway row
    less changes neither the components nor their source: nothing written."""
    case = load_case(K40_CASE)
    local_rows = [row for row in case["local"] if row.get("Tok") != "75"]
    coordinator = _coordinator(hass, case)
    await _start(hass, coordinator)
    entity = await _sensor(hass, coordinator)
    assert entity is not None
    before = _state(hass, entity)
    assert before.attributes["components_source"] == "cloud"
    changes = _changes(hass, entity.entity_id)
    newer = _local(local_rows, observed_at=NOW + timedelta(minutes=5))
    coordinator.local_selection._runtime.observations.append(newer)  # type: ignore[union-attr]
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert components(coordinator).source == "cloud"  # type: ignore[union-attr]
    assert changes == []
    after = _state(hass, entity)
    assert after.attributes == before.attributes
    await coordinator.async_shutdown()


# ------------------------------------------- names never decide anything (I5)


def _behaviour(case: dict[str, Any], table: Any) -> tuple[object, ...]:
    """What may never depend on a name entry: the plant kind, its evidence
    and conflicts, whether the sensor exists, the hybrid evidence and every
    component entry except its displayed name and basis."""
    inputs = inputs_of(case)
    result = resolve(inputs, table)
    rows = inputs.components
    view = plant_view(result, ShownComponents("cloud", rows, False, 1), table)
    ready = result.plant_kind in ("heat_pump", "hybrid", "boiler") or any(
        row.tok == 8 and row.type_id not in (None, 0, 17) and row.tok not in (72, 75)
        for row in rows
    )
    return (
        result.plant_kind,
        result.evidence,
        result.conflicts,
        ready,
        hybrid_evidence((), result.evidence),
        tuple(
            {k: v for k, v in item.items() if k not in ("type_name", "basis")}
            for item in view.components
        ),
    )


@pytest.mark.parametrize(
    "variants",
    [each_name_entry_removed, each_name_entry_alone],
    ids=["removed", "alone"],
)
def test_a_name_entry_never_changes_behaviour(
    variants: Callable[[], Any],
) -> None:
    """I5 (M42): each name entry removed alone, and each one alone in a table
    without the others: only shown names change."""
    cases = [load_case(name) for name in case_names()]
    cases = [case for case in cases if not case.get("no_block")]
    table = load_table()
    expected = [_behaviour(case, table) for case in cases]
    checked = 0
    for type_id, variant in variants():
        for case, before in zip(cases, expected, strict=True):
            assert _behaviour(case, variant) == before, (type_id, case["header"])
        checked += 1
    assert checked > 100


# ------------------------------------------------------------ faulty table


async def test_without_a_component_table_the_sensor_is_as_before(
    hass: HomeAssistant, tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """I6: a missing or faulty table gives one warning; the sensor shows its
    state and previous attributes, no components and no component state;
    the plant kind from the type values stays."""
    broken = tmp_path / "system_components.json"
    broken.write_text("{", encoding="utf-8")
    system_components.load_table.cache_clear()
    try:
        with (
            patch.object(system_components, "TABLE_PATH", broken),
            caplog.at_level(logging.WARNING),
        ):
            assert system_components.load_table() is None
            coordinator = _coordinator(hass, _own_k40())
            await _start(hass, coordinator)
            entity = await _sensor(hass, coordinator)
            assert entity is not None
            state = _state(hass, entity)
            icom = _coordinator(
                hass, load_case("issue_467_icom_without_type"), gateway_id="gw-2"
            )
            await _start(hass, icom)
            assert _tracked(icom) == []
            await icom.async_shutdown()
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "system component table" in warnings[0].getMessage()
        assert state.state == INDOOR
        attributes = _attributes(state)
        check_contract(attributes)
        before = _beta5_attributes(attributes["heat_source_type"])
        assert {k: attributes[k] for k in before} == before
        assert attributes["components"] == []
        assert attributes["components_source"] is None
        assert attributes["components_table_version"] is None
        assert attributes["plant_kind"] == "heat_pump"
        await coordinator.async_shutdown()
    finally:
        system_components.load_table.cache_clear()
        system_components.load_table()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_k40_entry_starts_without_a_component_table(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I6 through a real setup (format 4, the own K40): the setup works,
    one warning, the heat generator sensor as before without table
    behaviour."""
    from tests.test_x40_golden import FROZEN, PLANTS, build_snapshot

    freezer.move_to(FROZEN)
    missing = tmp_path / "missing.json"
    system_components.load_table.cache_clear()
    try:
        with (
            patch.object(system_components, "TABLE_PATH", missing),
            caplog.at_level(logging.WARNING),
        ):
            snapshot = await build_snapshot(hass, PLANTS["k40_own_cloud"])
    finally:
        system_components.load_table.cache_clear()
        system_components.load_table()
    table_warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "system component table" in record.getMessage()
    ]
    assert len(table_warnings) == 1
    own = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and record.name.startswith("custom_components.bosch_buderus_heating")
    ]
    assert own == table_warnings
    state = snapshot["states"][f"sensor {GATEWAY}:heat_generator"]
    assert state["state"] == INDOOR
    attributes = {k: v for k, v in state["attributes"].items() if k != "friendly_name"}
    check_contract(attributes)
    assert {k: attributes[k] for k in _beta5_attributes()} == _beta5_attributes()
    assert attributes["components"] == []
    assert attributes["components_table_version"] is None
    assert attributes["plant_kind"] == "heat_pump"
