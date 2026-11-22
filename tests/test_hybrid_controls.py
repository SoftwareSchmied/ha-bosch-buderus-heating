"""Hybrid plants: the control strategy and the energy prices as controls.

The shapes follow what the plants report (strategy with ``allowedValues``,
prices with limits, step and an empty unit). The entities, the policy
decisions, the dialog rows and the diagnostics are checked here; the write
transactions are in ``test_hybrid_writes.py``.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EntityCategory

from custom_components.bosch_buderus_heating import hybrid_controls as hybrid
from custom_components.bosch_buderus_heating.controls_flow import (
    control_group,
    controls,
)
from custom_components.bosch_buderus_heating.coordinator import ResourceSnapshot
from custom_components.bosch_buderus_heating.device import device_info_for_resource
from custom_components.bosch_buderus_heating.diagnostics import _control_diagnostics
from custom_components.bosch_buderus_heating.number import (
    async_setup_entry as setup_numbers,
)
from custom_components.bosch_buderus_heating.number import (
    build_number_descriptions,
)
from custom_components.bosch_buderus_heating.pointt import Resource, ResourceMetadata
from custom_components.bosch_buderus_heating.select import (
    async_setup_entry as setup_selects,
)
from custom_components.bosch_buderus_heating.select import (
    build_select_descriptions,
)
from custom_components.bosch_buderus_heating.writes import (
    GENERIC_CONTEXT,
    RRC_CONTEXT,
    ControlLocked,
    ControlUnused,
    assess_control,
    validate_control_value,
)
from tests.test_dynamic_controls import environment, snapshots  # noqa: F401
from tests.test_heating_controls import flow

STRATEGY = hybrid.STRATEGY_PATH
ELECTRICITY = hybrid.ELECTRICITY_PATH
GAS = hybrid.GAS_PATH
OIL = hybrid.OIL_PATH
PV = hybrid.FEED_IN_PATH
INTEGRATION = Path(__file__).parents[1] / "custom_components" / "bosch_buderus_heating"


def strategy(value="cost", options=("cost", "maxRenewableShare"), **meta):
    fields = {
        "resource_type": "stringValue",
        "writable": True,
        "allowed_values": options,
        "used": True,
        "available": True,
    }
    fields.update(meta)
    return Resource(
        path=STRATEGY,
        value=value,
        has_value=True,
        metadata=ResourceMetadata(**fields),
    )


def price(path=ELECTRICITY, value=0.55, **meta):
    fields = {
        "resource_type": "floatValue",
        "writable": True,
        "unit": "",
        "minimum": 0.0,
        "maximum": 100.0,
        "step_size": 0.01,
        "used": True,
        "available": True,
    }
    fields.update(meta)
    return Resource(
        path=path, value=value, has_value=True, metadata=ResourceMetadata(**fields)
    )


def plant(**overrides):
    """A hybrid plant with every path (the strategy, the four prices)."""
    resources = {
        STRATEGY: strategy(),
        ELECTRICITY: price(ELECTRICITY, 0.55),
        GAS: price(GAS, 0.09),
        OIL: price(OIL, 0.10),
        PV: price(PV, 0.08),
    }
    for path, resource in overrides.items():
        if resource is None:
            resources.pop(path)
        else:
            resources[path] = resource
    return resources


def numbers(resources, context=GENERIC_CONTEXT):
    return {d.resource_path: d for d in build_number_descriptions(resources, context)}


def selects(resources, context=GENERIC_CONTEXT):
    return {d.resource_path: d for d in build_select_descriptions(resources, context)}


# --- 1: the entities of a plant ---------------------------------------------


def test_a_plant_with_every_path_gets_one_select_and_three_numbers():
    chosen = selects(plant())
    assert list(chosen) == [STRATEGY]
    select = chosen[STRATEGY]
    assert select.translation_key == "hybrid_control_strategy"
    assert select.options == ["cost", "max_renewable_share"]
    assert select.entity_category is EntityCategory.CONFIG
    assert select.entity_registry_enabled_default is False

    chosen_numbers = numbers(plant())
    assert set(chosen_numbers) == {ELECTRICITY, GAS, PV}
    for number in chosen_numbers.values():
        assert number.entity_category is EntityCategory.CONFIG
        assert number.entity_registry_enabled_default is False
        assert number.device_class is None
    assert {n.translation_key for n in chosen_numbers.values()} == {
        "hybrid_electricity_price",
        "hybrid_gas_price",
        "hybrid_feed_in_price",
    }


async def test_the_devices_are_the_ones_of_the_sensors(hass, environment):  # noqa: F811
    coordinator, _, entry = environment
    coordinator.data = snapshots(*plant().values())
    added = []
    await setup_selects(hass, entry, added.extend)
    await setup_numbers(hass, entry, added.extend)
    assert len(added) == 4
    by_path = {e.entity_description.resource_path: e for e in added}
    for path in (STRATEGY, ELECTRICITY, GAS, PV):
        assert by_path[path].device_info == device_info_for_resource(coordinator, path)
    # The strategy belongs with the heat source, the prices with the plant.
    assert by_path[STRATEGY].device_info == device_info_for_resource(
        coordinator, "/heatSources/hybrid/bivalentSetpoint"
    )
    assert by_path[ELECTRICITY].device_info == device_info_for_resource(
        coordinator, "/system/energyTariff/oil"
    )
    assert by_path[PV].unique_id.endswith(":system:energyTariff:pv:control")
    assert by_path[STRATEGY].unique_id.endswith(
        ":heatSources:hybrid:controlStrategy:control"
    )


def test_the_state_of_the_selection_is_the_translated_option():
    # The sensor and the select show the same keys for the same wire values.
    assert hybrid.STRATEGY_KEYS["maxRenewableShare"] == "max_renewable_share"
    resource = strategy("maxRenewableShare")
    descriptions = selects({STRATEGY: resource})
    assert descriptions[STRATEGY].options == ["cost", "max_renewable_share"]


# --- 2: gas and oil ---------------------------------------------------------


@pytest.mark.parametrize(
    ("resources", "expected"),
    [
        (plant(), {ELECTRICITY, GAS, PV}),
        (plant(**{GAS: None}), {ELECTRICITY, OIL, PV}),
        (plant(**{GAS: None, OIL: None}), {ELECTRICITY, PV}),
        (plant(**{OIL: None}), {ELECTRICITY, GAS, PV}),
    ],
    ids=["gas_and_oil", "only_oil", "neither", "only_gas"],
)
def test_the_fossil_price_follows_the_app_rule(resources, expected):
    assert set(numbers(resources)) == expected


# --- 3: plants without the paths --------------------------------------------


def test_a_plant_without_the_paths_gets_nothing():
    other = {
        "/heatSources/hybrid/bivalentSetpoint": price(
            "/heatSources/hybrid/bivalentSetpoint", 2.0, unit="C"
        ),
    }
    assert numbers(other) == {}
    assert selects(other) == {}
    assert numbers({}) == {} and selects({}) == {}


def test_the_bivalence_point_and_the_reminder_stay_read_only():
    for path in (
        "/heatSources/hybrid/bivalentSetpoint",
        "/heatSources/hybrid/reminderDate",
        "/heatSources/hybrid/reminderEnable",
        "/system/energyTariff/night/electricity/value",
        "/system/variableTariff/supportStatus",
    ):
        assessment = assess_control(replace(price(), path=path), GENERIC_CONTEXT)
        assert not assessment.eligible
        assert assessment.rejection_reason == "no_scalar_control_policy"


# --- 4: flags ---------------------------------------------------------------


def test_a_value_that_is_not_writable_gives_no_control():
    resources = plant(
        **{
            STRATEGY: strategy(writable=False),
            ELECTRICITY: price(ELECTRICITY, writable=False),
        }
    )
    assert STRATEGY not in selects(resources)
    assert ELECTRICITY not in numbers(resources)
    assert GAS in numbers(resources)


def test_an_unavailable_setting_gives_no_control():
    resources = plant(
        **{
            STRATEGY: strategy(available=False),
            ELECTRICITY: price(ELECTRICITY, available=False),
        }
    )
    assert selects(resources) == {}
    assert ELECTRICITY not in numbers(resources)


def test_an_unused_setting_keeps_the_control_and_refuses_the_write():
    unused = price(ELECTRICITY, used=False)
    assert ELECTRICITY in numbers({ELECTRICITY: unused})
    with pytest.raises(ControlUnused):
        validate_control_value(unused, 0.30, GENERIC_CONTEXT)
    unused_strategy = strategy(used=False)
    assert STRATEGY in selects({STRATEGY: unused_strategy})
    with pytest.raises(ControlUnused):
        validate_control_value(unused_strategy, "maxRenewableShare", GENERIC_CONTEXT)
    assert issubclass(ControlUnused, ControlLocked)


@pytest.mark.parametrize("flag", ["maybe", "yes", 2])
def test_malformed_flags_give_no_control(flag):
    from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource

    payload = {
        "id": ELECTRICITY,
        "type": "floatValue",
        "writeable": 1,
        "value": 0.55,
        "minValue": 0,
        "maxValue": 100,
        "stepSize": 0.01,
        "used": flag,
        "available": "true",
    }
    resource = parse_resource(payload)
    assert ELECTRICITY not in numbers({ELECTRICITY: resource})


# --- 5: the options of the strategy -----------------------------------------


@pytest.mark.parametrize(
    "options",
    [(), ("cost",), ("somethingNew",), ("somethingNew", "alsoNew"), ("",)],
    ids=["empty", "one", "only_unknown", "two_unknown", "blank"],
)
def test_too_few_known_options_give_no_control(options):
    assert selects({STRATEGY: strategy(options=options)}) == {}


def test_known_and_unknown_options_offer_only_the_known_ones():
    resource = strategy(options=("cost", "heatPumpOnly", "vendorSpecial"))
    chosen = selects({STRATEGY: resource})
    assert chosen[STRATEGY].options == ["cost", "heat_pump_preferred"]
    assessment = assess_control(resource, GENERIC_CONTEXT)
    assert assessment.options == ("cost", "heatPumpOnly")
    with pytest.raises(Exception, match="not currently advertised"):
        validate_control_value(resource, "vendorSpecial", GENERIC_CONTEXT)


def test_all_six_values_are_offered_in_the_documented_order():
    resource = strategy(
        options=(
            "bivalentAlternativeMode",
            "boilerOnly",
            "environment",
            "heatPumpOnly",
            "maxRenewableShare",
            "cost",
        )
    )
    assert selects({STRATEGY: resource})[STRATEGY].options == [
        "cost",
        "max_renewable_share",
        "heat_pump_preferred",
        "environment",
        "boiler_only",
        "bivalent_alternative",
    ]


def test_a_current_value_outside_the_options_keeps_the_control_like_the_others():
    resource = strategy("vendorSpecial", options=("cost", "maxRenewableShare"))
    assert STRATEGY in selects({STRATEGY: resource})
    assert assess_control(resource, GENERIC_CONTEXT).current_value_issue == (
        "current_option_not_advertised"
    )


# --- 6: limits and step -----------------------------------------------------


@pytest.mark.parametrize(
    "meta",
    [
        {"minimum": None},
        {"maximum": None},
        {"minimum": None, "maximum": None},
        {"minimum": 10.0, "maximum": 5.0},
        {"minimum": float("nan")},
        {"maximum": float("inf")},
        {"maximum": 1000.5},
        {"minimum": -0.5},
        {"step_size": 0},
        {"step_size": -0.01},
        {"step_size": 500.0},
    ],
    ids=[
        "no_min",
        "no_max",
        "no_limits",
        "inverted",
        "nan",
        "inf",
        "too_large",
        "negative_min",
        "zero_step",
        "negative_step",
        "step_wider_than_range",
    ],
)
def test_missing_or_implausible_limits_or_steps_give_no_control(meta):
    assert ELECTRICITY not in numbers({ELECTRICITY: price(**meta)})


def test_the_limits_of_a_price_may_reach_the_documented_range():
    chosen = numbers({ELECTRICITY: price(minimum=0.0, maximum=1000.0)})
    assert chosen[ELECTRICITY].native_max_value == 1000.0


def test_a_missing_step_is_a_hundredth():
    chosen = numbers({ELECTRICITY: price(step_size=None)})
    assert chosen[ELECTRICITY].native_step == 0.01
    resource = price(step_size=None)
    validate_control_value(resource, 0.31, GENERIC_CONTEXT)
    with pytest.raises(Exception, match="step size"):
        validate_control_value(resource, 0.315, GENERIC_CONTEXT)


def test_an_advertised_step_is_the_grid():
    resource = price(step_size=0.05, minimum=0.0)
    assert numbers({ELECTRICITY: resource})[ELECTRICITY].native_step == 0.05
    validate_control_value(resource, 0.35, GENERIC_CONTEXT)
    with pytest.raises(Exception, match="step size"):
        validate_control_value(resource, 0.31, GENERIC_CONTEXT)


# --- 7: units ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("unit", "shown"),
    [
        ("", None),
        (None, None),
        ("   ", None),
        ("€/kWh", "€/kWh"),
        ("ct/kWh", "ct/kWh"),
        ("EUR/kWh", "EUR/kWh"),
        ("kr/kWh", "kr/kWh"),
        ("x" * 17, None),
        ("a\x00b", None),
        ("a\nb", None),
        ("<b>x</b>", None),
    ],
)
def test_the_unit_is_the_reported_one_or_none(unit, shown):
    assert hybrid.display_unit(unit) == shown
    description = numbers({ELECTRICITY: price(unit=unit)})[ELECTRICITY]
    assert description.native_unit_of_measurement == shown


async def test_the_entity_shows_the_reported_unit_and_follows_a_change(
    hass,
    environment,  # noqa: F811
):
    coordinator, _, entry = environment
    coordinator.data = snapshots(price(unit="€/kWh"))
    added = []
    await setup_numbers(hass, entry, added.extend)
    (number,) = added
    assert number.native_unit_of_measurement == "€/kWh"
    coordinator.async_set_updated_data(snapshots(price(unit="")))
    assert number.native_unit_of_measurement is None
    assert number.native_min_value == 0.0 and number.native_max_value == 100.0
    assert number.native_value == 0.55


# --- 8: the dialog ----------------------------------------------------------


def test_the_hybrid_paths_have_their_own_tab():
    for path in hybrid.CONTROL_PATHS:
        assert control_group(path) == "hybrid"
    assert control_group("/system/awayMode/enabled") == "system"
    assert control_group("/heatSources/hybrid/bivalentSetpoint") == "system"


async def test_the_dialog_offers_the_hybrid_group_only_with_controls(hass):
    options, coordinator, _, _ = await flow(hass)
    assert not any(path in controls(coordinator) for path in hybrid.CONTROL_PATHS)

    for resource in plant().values():
        coordinator.resources[resource.path] = resource
        coordinator.data[resource.path] = ResourceSnapshot(
            resource, True, datetime.now(UTC)
        )
    current = controls(coordinator)
    assert {path for path in current if path in hybrid.CONTROL_PATHS} == {
        STRATEGY,
        ELECTRICITY,
        GAS,
        PV,
    }
    rows = await options._control_rows(current, current=current)
    hybrid_rows = {row["key"]: row for row in rows if row["group"] == "hybrid"}
    assert set(hybrid_rows) == {STRATEGY, ELECTRICITY, GAS, PV}
    # The group comes after the circuits and the rest of the system.
    groups = list(dict.fromkeys(row["group"] for row in rows))
    assert groups[-1] == "hybrid"
    row = hybrid_rows[STRATEGY]
    assert row["kind"] == "select" and row["advanced"] is False
    assert [item["value"] for item in row["options"]] == ["cost", "maxRenewableShare"]
    assert row["options"][0]["label"]["en"] == "Cost optimized"
    assert row["options"][1]["label"]["de"] == "Wärmepumpe optimiert"
    assert row["label"]["de"] == "Hybrid-Regelungsstrategie"
    price_row = hybrid_rows[ELECTRICITY]
    assert price_row["kind"] == "number" and price_row["advanced"] is False
    assert (price_row["minimum"], price_row["maximum"], price_row["step"]) == (
        0.0,
        100.0,
        0.01,
    )
    assert price_row["unit"] == ""
    assert hybrid_rows[PV]["label"]["en"] == "Feed-in tariff"
    assert hybrid_rows[PV]["label"]["cs"]
    # The oil price is not offered beside a gas price.
    assert OIL not in current

    # Without the gas price the oil price takes its place.
    del coordinator.data[GAS]
    assert OIL in controls(coordinator) and GAS not in controls(coordinator)


async def test_a_dialog_write_goes_through_the_one_transaction(hass):
    options, coordinator, _, _ = await flow(hass)
    for resource in plant().values():
        coordinator.resources[resource.path] = resource
        coordinator.data[resource.path] = ResourceSnapshot(
            resource, True, datetime.now(UTC)
        )
    coordinator._write_service.async_write_enum.reset_mock()
    options._control_displayed = None
    result = await options.async_step_operate()
    assert result["type"] == "form"
    result = await options.async_step_operate(
        {
            "controls": {
                "changes": {STRATEGY: "maxRenewableShare", ELECTRICITY: 0.31},
                "active": "hybrid",
            }
        }
    )
    assert not result.get("errors")
    assert coordinator._write_service.async_write_enum.await_count == 1
    assert coordinator._write_service.async_write_number.await_count == 1
    assert coordinator.data[STRATEGY].resource.value == "maxRenewableShare"
    assert coordinator.data[ELECTRICITY].resource.value == 0.31
    # A value off the grid is refused in the dialog, nothing is written.
    coordinator._write_service.async_write_number.reset_mock()
    result = await options.async_step_operate(
        {"controls": {"changes": {ELECTRICITY: 0.315}, "active": "hybrid"}}
    )
    assert result["errors"]
    coordinator._write_service.async_write_number.assert_not_awaited()


# --- 9: the translations ----------------------------------------------------


def test_every_language_translates_the_strategy_states_and_names():
    keys = set(hybrid.STRATEGY_KEYS.values())
    for path in sorted((INTEGRATION / "translations").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))["entity"]
        for platform in ("select", "sensor"):
            entry = data[platform]["hybrid_control_strategy"]
            assert entry["name"]
            assert set(entry["state"]) == keys
            assert all(entry["state"].values())
        for key in hybrid.PRICE_KEYS.values():
            assert data["number"][key]["name"]
        dialog = json.loads(
            (INTEGRATION / "i18n" / path.name).read_text(encoding="utf-8")
        )["dialogs"]["controls"]
        assert dialog["hybrid"]


def test_the_sensor_shows_the_translated_state_of_each_strategy():
    from custom_components.bosch_buderus_heating.sensor import (
        _enum_options,
        _enum_state,
        _enum_translation_key,
    )

    resource = strategy("maxRenewableShare")
    key = _enum_translation_key(resource, None)
    assert key == "hybrid_control_strategy"
    assert _enum_state(key, "maxRenewableShare") == "max_renewable_share"
    assert _enum_state(key, "heatPumpOnly") == "heat_pump_preferred"
    assert _enum_state(key, "cost") == "cost"
    options = _enum_options(resource, key)
    assert set(hybrid.STRATEGY_KEYS.values()) <= set(options)
    # An unknown value stays visible as it is.
    assert "vendorSpecial" in _enum_options(strategy("vendorSpecial"), key)


def test_the_feed_in_tariff_has_the_app_name_in_every_language():
    from custom_components.bosch_buderus_heating.i18n import LANGUAGES
    from custom_components.bosch_buderus_heating.resource_catalog import resource_name

    assert resource_name(PV, language="de") == "Einspeisevergütung"
    assert resource_name(PV, language="en") == "Feed-in tariff"
    assert resource_name(PV, language="cs") != "Feed-in tariff"
    for language in LANGUAGES:
        assert resource_name(PV, language=language)


# --- 10 (RRC): the same controls on a room controller (#122 shape) ----------


def test_a_room_controller_gets_the_same_controls():
    resources = plant()
    assert set(numbers(resources, RRC_CONTEXT)) == {ELECTRICITY, GAS, PV}
    assert set(selects(resources, RRC_CONTEXT)) == {STRATEGY}
    assert assess_control(resources[STRATEGY], RRC_CONTEXT).eligible


# --- diagnostics ------------------------------------------------------------


def test_the_diagnostics_show_limits_step_unit_class_and_options_but_no_value():
    report = _control_diagnostics(price(ELECTRICITY, 0.5512, unit="€/kWh"))
    assert report["eligible"] is True
    assert report["enabled_by_default"] is False
    assert report["hybrid"] == {
        "unit_class": "text",
        "step_size": 0.01,
        "step_size_valid": True,
        "minimum": 0.0,
        "maximum": 100.0,
    }
    for unit, unit_class in (("", "empty"), (None, "none"), ("a\x00", "other")):
        shown = _control_diagnostics(price(unit=unit))["hybrid"]
        assert shown["unit_class"] == unit_class
    report = _control_diagnostics(
        strategy(
            "cost", options=("cost", "maxRenewableShare", "vendorSpecial", "bad value!")
        )
    )
    assert report["eligible"] is True
    assert report["hybrid"] == {
        "advertised_options": ["cost", "maxRenewableShare", "vendorSpecial"],
        "advertised_option_count": 4,
        "known_strategies": sorted(hybrid.STRATEGIES),
    }
    dump = json.dumps(
        [
            _control_diagnostics(price(ELECTRICITY, 0.5512)),
            _control_diagnostics(
                strategy("currentOnlyValue", options=("cost", "heatPumpOnly"))
            ),
        ]
    )
    assert "0.5512" not in dump and "currentOnlyValue" not in dump
    # Other paths get no hybrid block.
    assert "hybrid" not in _control_diagnostics(
        price("/system/energyTariff/night/electricity/value")
    )


def test_an_old_diagnostics_schema_is_kept():
    from custom_components.bosch_buderus_heating.diagnostics import (
        DIAGNOSTICS_SCHEMA_VERSION,
    )

    assert DIAGNOSTICS_SCHEMA_VERSION == 49


async def test_nothing_is_written_by_building_the_entities(
    hass,
    environment,  # noqa: F811
):
    coordinator, client, entry = environment
    client.put_resource_value = AsyncMock()
    coordinator.data = snapshots(*plant().values())
    added = []
    await setup_selects(hass, entry, added.extend)
    await setup_numbers(hass, entry, added.extend)
    client.put_resource_value.assert_not_awaited()
