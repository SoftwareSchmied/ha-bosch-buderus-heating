"""The heat generator entity: models by role from the module lists."""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import heat_generator
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
    ResourceSnapshot,
)
from custom_components.bosch_buderus_heating.diagnostics import (
    _contains_technical_values,
    _gateway_diagnostics,
)
from custom_components.bosch_buderus_heating.heat_generator import (
    EMPTY_TABLE,
    MAX_NAME_LENGTH,
    TABLE,
    TABLE_PATH,
    ModuleRow,
    choose,
    clean_text,
    from_data,
    interpret,
    load_table,
    parse_table,
    rows_from_cloud,
    rows_from_local,
)
from custom_components.bosch_buderus_heating.heat_generator_sensor import (
    BoschBuderusHeatGeneratorSensor,
    HeatGeneratorInputs,
    heat_generator_diagnostics,
    heat_generator_unique_id,
    track_heat_generator_sensor,
)
from custom_components.bosch_buderus_heating.inventory import (
    build_anonymized_inventory,
)
from custom_components.bosch_buderus_heating.local_selection import LocalSelection
from custom_components.bosch_buderus_heating.pointt import (
    Gateway,
    Resource,
    ResourceMetadata,
)
from tests.ha_test_helpers import MockConfigEntry, mock_restore_cache_with_extra_data
from tests.test_inventory import _diagnostics as inventory_diagnostics
from tests.test_local_api import read
from tests.test_local_selection import TARGET, FakeRuntime

GATEWAY = "gateway-one"
UNIQUE_ID = f"{GATEWAY}:heat_generator"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
INDOOR = "Logatherm WLW176i-12 TP70"
OUTDOOR = "WLW-10 MB AR"
SERIALS = (
    "SERIAL-INDOOR-1",
    "PRODUCT-SERIAL-1",
    "NUMBER-1",
    "SERIAL-OUTDOOR-1",
    "PRODUCT-SERIAL-2",
    "SERIAL-GATEWAY",
    "uuid-gateway",
)
# The reference installation as the cloud reports it (synthetic serials).
OWN_CLOUD = (
    {
        "ModuleSerialNumber": "SERIAL-INDOOR-1",
        "HwVersion": "1",
        "SwIdenStr": "com.example:hyd",
        "Tok": "8",
        "No": "NUMBER-1",
        "Id": "8",
        "Ver": "12.11.1-2fc97779",
        "ProductName": INDOOR,
        "ProductSerialNumber": "PRODUCT-SERIAL-1",
        "ModuleHwIdentStr": "XCU_THH",
        "ProductTtn": "8738213489",
        "ModuleTtn": "8750767801",
    },
    {
        "ModuleSerialNumber": "SERIAL-HMI",
        "SwIdenStr": "HMI800.2_Application_FIELD",
        "Tok": "16",
        "Id": "253",
        "Ver": "47.12-FIELD",
        "ModuleHwIdentStr": "HMI_module_02_800-20_GSBS_BU_111x",
    },
    {
        "Id": "17",
        "ModuleSerialNumber": "SERIAL-GATEWAY",
        "ModuleHwIdentStr": "MX400",
        "Ver": "15.00.01",
        "Tok": "72",
        "ProductUuid": "uuid-gateway",
    },
    {
        "ModuleSerialNumber": "SERIAL-OUTDOOR-1",
        "Tok": "8",
        "Id": "65281",
        "Ver": "9.16.0-dab9f463",
        "ProductName": OUTDOOR,
        "ProductSerialNumber": "PRODUCT-SERIAL-2",
        "ModuleHwIdentStr": "XCU_SRH",
    },
)
EXAMPLES = json.loads(
    (Path(__file__).parent / "fixtures/local_api_examples.json").read_text(
        encoding="utf-8"
    )
)["examples"]
DOCUMENTED = next(
    item["response"]
    for item in EXAMPLES
    if item["template"] == "/system/basicInfo" and item["name"] == "default"
)


OBSERVED = {"kind": "observed", "note": "test"}
DATASHEET = "https://www.bosch-homecomfort.com/de/datasheet.pdf"


def table(*entries: dict[str, object], version: int = 7) -> heat_generator.ModelTable:
    """A test table: the shipped vocabulary plus the given entries."""
    return parse_table(
        {
            "schema": 1,
            "table_version": version,
            "constructions": ["monobloc", "split", "compact"],
            "brand_families": ["bosch_group"],
            "documented_sources": ["example.com"],
            "entries": [
                {"examples": _examples(item), "evidence": OBSERVED, **item}
                for item in entries
            ],
        }
    )


def _examples(entry: dict[str, object]) -> list[dict[str, object]]:
    match = entry["match"]
    assert isinstance(match, dict)
    return [
        {
            "product_name": match.get("product_name", "Example"),
            "hardware_type": match.get("hardware_type", "HW"),
        }
    ]


def rows(*pairs: tuple[str, str | None]) -> tuple[ModuleRow, ...]:
    return tuple(ModuleRow(name, hardware) for name, hardware in pairs)


def cloud_resource(values) -> Resource:
    return Resource(
        "/system/info",
        values=tuple(values),
        has_values=True,
        metadata=ResourceMetadata(resource_type="systeminfo"),
    )


def local_system(modules, *, status="completed", observed_at=NOW):
    return read(
        {
            "id": "/system/basicInfo",
            "type": "systeminfo",
            "writeable": 0,
            "status": status,
            "timeStamp": "1684201667",
            "gatewayId": "101012902",
            "values": list(modules),
        },
        observed_at=observed_at,
    )


def local_module(name, hardware, serial="SERIAL-LOCAL-1", product="PRODUCT-LOCAL-1"):
    module = {
        "Tok": "8",
        "Id": "8",
        "Ver": "1.0",
        "ModuleSerialNumber": serial,
        "ModuleHwIdentStr": hardware,
        "ProductSerialNumber": product,
    }
    if name is not None:
        module["ProductName"] = name
    return module


def coordinator_for(
    hass: HomeAssistant,
    cloud=None,
    *,
    local=(),
    device_type: str = "MX400",
    received: datetime = NOW,
    available: bool = True,
) -> BoschBuderusDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway(GATEWAY, device_type=device_type), entry
    )
    coordinator.update_interval = None
    coordinator.resources = {}
    coordinator.data = {}
    if cloud is not None:
        resource = cloud_resource(cloud)
        coordinator.resources[resource.path] = resource
        coordinator.data[resource.path] = ResourceSnapshot(
            resource, available, received
        )
    coordinator.last_update_success = True
    coordinator.local_selection = LocalSelection(FakeRuntime(list(local)), TARGET)
    return coordinator


def set_cloud(coordinator, values, received=NOW) -> None:
    resource = cloud_resource(values)
    coordinator.data = {
        **(coordinator.data or {}),
        resource.path: ResourceSnapshot(resource, True, received),
    }


def tracked(coordinator) -> list[BoschBuderusHeatGeneratorSensor]:
    added: list = []
    entry = SimpleNamespace(async_on_unload=Mock())
    track_heat_generator_sensor(entry, coordinator, added.extend)
    return added


def _platform(hass, entry):
    from homeassistant.helpers.entity_platform import EntityPlatform

    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(__name__),
        domain="sensor",
        platform_name=DOMAIN,
        platform=None,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    platform.async_prepare()
    return platform


async def added(hass, coordinator) -> BoschBuderusHeatGeneratorSensor:
    (entity,) = tracked(coordinator)
    await _platform(hass, coordinator.config_entry).async_add_entities([entity])
    await hass.async_block_till_done()
    return entity


def shown(hass, entity) -> State:
    state = hass.states.get(entity.entity_id)
    assert state is not None
    return state


def own_plant():
    return interpret(rows_from_cloud(OWN_CLOUD), "cloud", observed_at=NOW)


# ------------------------------------------------------------- the table


def test_the_shipped_table_follows_its_schema() -> None:
    raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    loaded = load_table()
    assert loaded == TABLE
    assert loaded.version == raw["table_version"] >= 1
    assert len(loaded.entries) == len(raw["entries"])
    for item in raw["entries"]:
        assert item["evidence"]["kind"] in ("observed", "documented")
        assert item["evidence"]["note"].strip()
        assert item["examples"]
        for example in item["examples"]:
            generator = heat_generator.interpret_row(
                ModuleRow(example["product_name"], example.get("hardware_type"))
            )
            # Every entry is in effect for its examples: nothing it proves is
            # overridden by a more specific entry.
            for key in ("role", "family", "construction", "power_kw"):
                if item.get(key) is not None:
                    assert getattr(generator, key) == item[key], (item, key)
            if item.get("brand_family") is not None:
                assert generator.brand_family == item["brand_family"]


def test_the_entries_are_observed_or_documented_by_the_manufacturer() -> None:
    raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    kinds = [entry["evidence"]["kind"] for entry in raw["entries"]]
    observed = [
        entry["match"]
        for entry in raw["entries"]
        if entry["evidence"]["kind"] == "observed"
    ]
    assert observed == [
        {"product_name": INDOOR},
        {"product_name": OUTDOOR},
        {"hardware_type": "XCU_THH"},
        {"hardware_type": "XCU_SRH"},
    ]
    assert set(kinds) == {"observed", "documented"}
    for entry in raw["entries"]:
        evidence = entry["evidence"]
        if evidence["kind"] == "documented":
            assert evidence["source"].startswith("https://www.")
        # Roles come from hardware types only, never from a product name.
        if "product_name" in entry["match"]:
            assert "role" not in entry, entry["match"]
        # A power class only where a document states it for that unit.
        if entry.get("power_kw") is not None:
            assert evidence["kind"] == "documented"
            assert "power class" in evidence["note"] or "nominal" in evidence["note"]


# The product names as they appear in real module lists and in the example
# of the official documentation, with the meaning the table gives them.
@pytest.mark.parametrize(
    ("name", "family", "construction", "power_kw"),
    [
        (INDOOR, "wlw176i", "monobloc", None),
        ("WLW176i-12 E", "wlw176i", "monobloc", None),
        ("Logatherm WLW186i-12 E", "wlw186i", "monobloc", None),
        ("Logatherm WLW186i-12 TP70", "wlw186i", "monobloc", None),
        (OUTDOOR, "wlw_mb_ar", "monobloc", 10),
        ("WLW-5 MB AR", "wlw_mb_ar", "monobloc", 5),
        ("Logatherm WLW-7 MB AR", "wlw_mb_ar", "monobloc", 7),
        ("WLW-12 MB AR", "wlw_mb_ar", "monobloc", 12),
        ("Compress CS6800iAW 12 E", "cs6800i_aw", None, None),
        ("CS6800i AW 12 MB", "cs6800i_aw", None, None),
        ("CS5800iAW 12 M", "cs5800i_aw", None, None),
        ("AW 4 OR-S", "aw_or", None, None),
        ("AW 12 OR-T", "aw_or", None, None),
        ("AW 10 OR-T", "aw_or", None, 10),
        ("CS3800i AW 6 O-S", "cs3800i_aw", None, None),
        ("CS3800i AW 10 O-T", "cs3800i_aw", None, None),
        ("CS8800i AW 13 O-T (B)", "cs8800i_aw", None, None),
    ],
)
def test_documented_models(name, family, construction, power_kw) -> None:
    generator = heat_generator.interpret_row(ModuleRow(name, None))
    assert (generator.family, generator.construction, generator.power_kw) == (
        family,
        construction,
        power_kw,
    )
    assert generator.known is True
    assert generator.brand_family == "bosch_group"
    # Without a hardware type the role stays open.
    assert generator.role == "other"


@pytest.mark.parametrize(
    "name",
    [
        # Not in any manufacturer document in this spelling.
        "Logatherm WLW MB-5 AR",
        # Sizes or variants no document lists.
        "WLW-4 MB AR",
        "WLW-8 MB AR",
        "Logatherm WLW176i-5 E",
        "Logatherm WLW186i-12 T180",
        "WLW-11 MBE+ AR",
        "AW 6 OR-S",
        "AW 10 OR-S",
        "CS6800iAW 7 E",
        "CS3800i AW 5 O-S",
        "CS8800i AW 12 O-T",
        # Other brand lines and split units without a document.
        "Logatherm WLW196i-8 AR",
        "Logatherm WLW-8 SP AR",
        "Compress 7001i AW 9 OR-S",
        "WLW-10 MB AR extra",
    ],
)
def test_undocumented_names_stay_unmapped(name) -> None:
    generator = heat_generator.interpret_row(ModuleRow(name, "XCU_SRH"))
    assert generator.known is False
    assert (
        generator.family,
        generator.construction,
        generator.power_kw,
        generator.brand_family,
    ) == (None, None, None, None)
    assert generator.role == "outdoor_unit"  # from the hardware type alone


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"role": "chimney"}, "invalid_role"),
        ({"construction": "tower"}, "invalid_meaning"),
        ({"brand_family": "unknown"}, "invalid_meaning"),
        ({"power_kw": -1}, "invalid_meaning"),
        ({"power_kw": True}, "invalid_meaning"),
        ({"evidence": "test"}, "invalid_evidence"),
        ({"evidence": None}, "invalid_evidence"),
        ({"evidence": {"kind": "observed", "note": " "}}, "entry_without_evidence"),
        ({"evidence": {"kind": "guessed", "note": "x"}}, "invalid_evidence"),
        (
            {"evidence": {"kind": "observed", "note": "x", "source": DATASHEET}},
            "invalid_evidence",
        ),
        ({"evidence": {"kind": "documented", "note": "x"}}, "source_not_manufacturer"),
        (
            {
                "evidence": {
                    "kind": "documented",
                    "note": "x",
                    "source": "https://www.example-shop.com/wlw176i.pdf",
                }
            },
            "source_not_manufacturer",
        ),
        (
            {
                "evidence": {
                    "kind": "documented",
                    "note": "x",
                    "source": "http://www.bosch-homecomfort.com/datasheet.pdf",
                }
            },
            "source_not_manufacturer",
        ),
        (
            {
                "evidence": {
                    "kind": "documented",
                    "note": "x",
                    "source": "https://bosch-homecomfort.com.example.net/a.pdf",
                }
            },
            "source_not_manufacturer",
        ),
        (
            {
                "evidence": {
                    "kind": "documented",
                    "note": "x",
                    "source": "https://evilbosch-homecomfort.com/a.pdf",
                }
            },
            "source_not_manufacturer",
        ),
        (
            {
                "evidence": {
                    "kind": "documented",
                    "note": "x",
                    "source": "https://github.com/someone/api-docs",
                }
            },
            "source_not_manufacturer",
        ),
        ({"examples": []}, "entry_without_examples"),
        (
            {"examples": [{"product_name": "Other", "hardware_type": "HW"}]},
            "example_not_matched",
        ),
        ({"match": {}}, "invalid_match"),
        ({"match": {"serial": "x"}}, "invalid_match"),
        ({"unexpected": 1}, "invalid_entry"),
        ({"role": None}, "entry_without_meaning"),
    ],
)
def test_a_faulty_table_entry_is_rejected(change, problem) -> None:
    entry = {
        "match": {"product_name": "Example"},
        "role": "heat_source",
        "examples": [{"product_name": "Example", "hardware_type": "HW"}],
        "evidence": OBSERVED,
        **change,
    }
    with pytest.raises(ValueError, match=problem):
        parse_table(
            {
                "schema": 1,
                "table_version": 1,
                "constructions": ["monobloc"],
                "brand_families": ["bosch_group"],
                "documented_sources": [
                    "bosch-homecomfort.com",
                    "github.com/bosch-home-comfort",
                ],
                "entries": [entry],
            }
        )


@pytest.mark.parametrize(
    "source",
    [
        DATASHEET,
        "https://media3.bosch-homecomfort.com/files/WLW176i_AR.pdf",
        "https://github.com/bosch-home-comfort/api-docs/blob/main/k-40-rf.md",
    ],
)
def test_a_documented_entry_cites_a_manufacturer_page(source) -> None:
    models = parse_table(
        {
            "schema": 1,
            "table_version": 1,
            "constructions": ["monobloc"],
            "brand_families": [],
            "documented_sources": [
                "bosch-homecomfort.com",
                "github.com/bosch-home-comfort",
            ],
            "entries": [
                {
                    "match": {"product_name": "WLW-10 MB AR"},
                    "power_kw": 10,
                    "examples": [{"product_name": "WLW-10 MB AR"}],
                    "evidence": {
                        "kind": "documented",
                        "note": "Datasheet of the outdoor unit",
                        "source": source,
                    },
                }
            ],
        }
    )
    assert models.entries[0].evidence == "documented"
    generator = heat_generator.interpret_row(ModuleRow("WLW-10 MB AR", None), models)
    assert generator.power_kw == 10


def test_the_shipped_entries_are_observed_and_the_sources_are_manufacturers() -> None:
    assert {entry.evidence for entry in TABLE.entries} == {"observed", "documented"}
    raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    assert "github.com/bosch-home-comfort" in raw["documented_sources"]
    with pytest.raises(ValueError, match="invalid_table"):
        parse_table({**raw, "documented_sources": ["https://example.com"]})


def test_an_unknown_table_schema_is_rejected() -> None:
    raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="unsupported_table"):
        parse_table({**raw, "schema": 2})
    with pytest.raises(ValueError, match="invalid_table"):
        parse_table({**raw, "table_version": 0})


# --------------------------------------------------------------- the cases


def test_the_reference_installation() -> None:
    """Indoor unit and monobloc outdoor unit; control unit rows ignored."""
    result = own_plant()
    assert result is not None
    assert result.state == INDOOR
    attributes = result.attributes()
    assert attributes == {
        "heat_generators": [
            {
                "role": "heat_source",
                "product_name": INDOOR,
                "hardware_type": "XCU_THH",
                "family": "wlw176i",
                "construction": "monobloc",
                "power_kw": None,
            },
            {
                "role": "outdoor_unit",
                "product_name": OUTDOOR,
                "hardware_type": "XCU_SRH",
                "family": "wlw_mb_ar",
                "construction": "monobloc",
                "power_kw": 10,
            },
        ],
        "primary": {
            "family": "wlw176i",
            "construction": "monobloc",
            "power_kw": None,
            "brand_family": "bosch_group",
            "outdoor_power_kw": 10,
        },
        "outdoor_unit": OUTDOOR,
        "source": "cloud",
        "table_version": TABLE.version,
        "known": True,
    }


def test_the_example_of_the_official_documentation() -> None:
    """Two modules: roles from the hardware type, the products from the
    manufacturer's pages; no construction and no power class proven."""
    observation = read(DOCUMENTED)
    result = interpret(rows_from_local(observation.data.modules), "local")
    assert result is not None
    assert result.state == "Compress CS6800iAW 12 E"
    assert [(item.role, item.product_name) for item in result.generators] == [
        ("heat_source", "Compress CS6800iAW 12 E"),
        ("outdoor_unit", "AW 4 OR-S"),
    ]
    # Nothing guessed from the names (no "split" from "OR-S", no 12 kW).
    assert [
        (item.family, item.construction, item.power_kw) for item in result.generators
    ] == [
        ("cs6800i_aw", None, None),
        ("aw_or", None, None),
    ]
    assert result.known is True
    assert result.attributes()["primary"] == {
        "family": "cs6800i_aw",
        "construction": None,
        "power_kw": None,
        "brand_family": "bosch_group",
        "outdoor_power_kw": None,  # no power class documented for AW 4 OR-S
    }
    assert heat_generator.diagnostics(None, result)["unmapped_modules"] == []


def test_a_split_unit_as_the_table_says() -> None:
    models = table(
        # A hardware entry earlier in the file: the product entry still wins
        # for the fields both set.
        {
            "match": {"hardware_type": "IN"},
            "role": "heat_source",
            "construction": "compact",
        },
        {"match": {"hardware_type": "OUT"}, "role": "outdoor_unit"},
        {
            "match": {"product_name": "Example Split 8 IN"},
            "family": "Example Split",
            "construction": "split",
            "power_kw": 8,
        },
    )
    result = interpret(
        rows(("Example Split 8 IN", "IN"), ("Example ODU", "OUT")),
        "cloud",
        table=models,
    )
    assert result is not None
    assert result.attributes()["primary"] == {
        "family": "Example Split",
        "construction": "split",
        "power_kw": 8,
        "brand_family": None,
        "outdoor_power_kw": None,
    }
    assert result.outdoor_unit == "Example ODU"
    # The outdoor unit has no product entry: not known, nothing guessed.
    assert result.known is False
    assert result.generators[1].construction is None


def test_a_gas_boiler_alone() -> None:
    models = table({"match": {"product_name": "Example Gas 25"}, "role": "boiler"})
    result = interpret(rows(("Example Gas 25", "BOILER_HW")), "cloud", table=models)
    assert result is not None
    assert result.state == "Example Gas 25"
    assert result.primary.role == "boiler"
    assert result.outdoor_unit is None
    assert result.attributes()["primary"]["outdoor_power_kw"] is None
    assert result.known is True


def test_a_hybrid_system() -> None:
    """Boiler, heat pump and hybrid manager are three entries; the heat
    pump is the main heat generator."""
    models = table(
        {"match": {"product_name": "Example Boiler"}, "role": "boiler"},
        {"match": {"product_name": "Example HP"}, "role": "heat_source"},
        {"match": {"product_name": "Example Manager"}, "role": "hybrid_manager"},
    )
    result = interpret(
        rows(
            ("Example Manager", "HM"),
            ("Example Boiler", "GB"),
            ("Example HP", "HP"),
        ),
        "local",
        table=models,
    )
    assert result is not None
    assert [item.role for item in result.generators] == [
        "heat_source",
        "boiler",
        "hybrid_manager",
    ]
    assert result.state == "Example HP"


def test_a_cascade_of_two_heat_pumps_in_a_stable_order() -> None:
    first = (
        ("Logatherm WLW176i-12 TP70", "XCU_THH"),
        ("WLW-10 MB AR", "XCU_SRH"),
        ("Logatherm WLW176i-12 TP70", "XCU_THH"),
        ("WLW-10 MB AR", "XCU_SRH"),
        ("Example Second Stage", "XCU_THH"),
    )
    result = interpret(rows(*first), "cloud")
    reversed_order = interpret(rows(*reversed(first)), "cloud")
    assert result is not None and reversed_order is not None
    assert result.attributes() == reversed_order.attributes()
    assert result.same_modules(reversed_order)
    assert [(item.role, item.product_name) for item in result.generators] == [
        ("heat_source", "Example Second Stage"),
        ("heat_source", INDOOR),
        ("heat_source", INDOOR),
        ("outdoor_unit", OUTDOOR),
        ("outdoor_unit", OUTDOOR),
    ]
    assert result.known is False  # the second stage has no product entry
    # Several outdoor units: the first in the stable order gives the power.
    assert result.attributes()["primary"]["outdoor_power_kw"] == 10


def test_the_first_outdoor_unit_of_a_cascade_gives_the_power() -> None:
    models = table(
        {"match": {"hardware_type": "IN"}, "role": "heat_source"},
        {"match": {"hardware_type": "OUT"}, "role": "outdoor_unit"},
        {"match": {"product_name": "Example ODU 12"}, "power_kw": 12},
        {"match": {"product_name": "Example ODU 8"}, "power_kw": 8},
    )
    listed = (
        ("Example Indoor", "IN"),
        ("Example ODU 8", "OUT"),
        ("Example ODU 12", "OUT"),
    )
    result = interpret(rows(*listed), "cloud", table=models)
    reversed_order = interpret(rows(*reversed(listed)), "cloud", table=models)
    assert result is not None and reversed_order is not None
    assert result.outdoor_unit == "Example ODU 12"
    assert result.attributes()["primary"]["outdoor_power_kw"] == 12
    assert reversed_order.attributes()["primary"]["outdoor_power_kw"] == 12
    # The main heat generator keeps its own (unset) power class.
    assert result.attributes()["primary"]["power_kw"] is None


def test_modules_without_a_product_name_never_count() -> None:
    assert rows_from_cloud(OWN_CLOUD[1:3]) == ()
    assert interpret(rows_from_cloud(OWN_CLOUD[1:3]), "cloud") is None
    local = local_system([local_module(None, "K40RF")])
    assert rows_from_local(local.data.modules) == ()


@pytest.mark.parametrize(
    "values", [(), ("text", 3, None, ["list"]), ({"ProductName": ""},)]
)
def test_an_empty_or_missing_module_list(values) -> None:
    assert interpret(rows_from_cloud(values), "cloud") is None


def test_an_unknown_hardware_type() -> None:
    result = interpret(rows(("Example Unknown", "XYZ_123")), "cloud")
    assert result is not None
    assert result.primary.role == "other"
    assert result.known is False
    assert result.attributes()["heat_generators"] == [
        {
            "role": "other",
            "product_name": "Example Unknown",
            "hardware_type": "XYZ_123",
            "family": None,
            "construction": None,
            "power_kw": None,
        }
    ]


def test_a_long_or_broken_product_name() -> None:
    long_name = "Example " + "X" * 400
    result = interpret(
        rows_from_cloud(
            [
                {"ProductName": long_name, "ModuleHwIdentStr": "XCU_THH"},
                {"ProductName": "Broken\x00\x07 name\n�", "ModuleHwIdentStr": None},
                {"ProductName": "\xff\xff\xff", "ModuleHwIdentStr": "XCU_SRH"},
                {"ProductName": "�", "ModuleHwIdentStr": "XCU_SRH"},
                {"ProductName": 42, "ModuleHwIdentStr": "XCU_SRH"},
            ]
        ),
        "cloud",
    )
    assert result is not None
    names = [item.product_name for item in result.generators]
    assert names == [long_name[:MAX_NAME_LENGTH], "Broken name"]
    assert len(result.state) == MAX_NAME_LENGTH
    assert clean_text("  a \t b  ") == "a b"
    assert clean_text("\x1b[31m") == "[31m"


def test_both_sources_with_a_conflicting_name() -> None:
    cloud = interpret(rows((INDOOR, "XCU_THH")), "cloud", observed_at=NOW)
    local = interpret(
        rows(("Logatherm WLW176i-12 TP", "XCU_THH")),
        "local",
        observed_at=NOW + timedelta(minutes=5),
    )
    # Never shown before: the newer answer.
    assert choose(cloud, local, None) == (local, True)
    # The last shown answer stays while they disagree.
    assert choose(cloud, local, cloud) == (cloud, True)
    assert choose(cloud, local, local) == (local, True)
    # Agreeing answers: the newer one names the source.
    same = interpret(rows((INDOOR, "XCU_THH")), "local", observed_at=NOW)
    later = interpret(
        rows((INDOOR, "XCU_THH")), "cloud", observed_at=NOW + timedelta(hours=1)
    )
    assert choose(later, same, None) == (later, False)
    # No answer: the last shown one.
    assert choose(None, None, cloud) == (cloud, False)
    report = heat_generator.diagnostics(cloud, local)
    assert report["conflict"] is True
    assert report["source"] == "local"
    assert report["other_source_modules"] == [
        {"role": "heat_source", "product_name": INDOOR, "hardware_type": "XCU_THH"}
    ]


def test_the_kept_modules_are_worked_out_with_the_current_table() -> None:
    result = own_plant()
    assert result is not None
    data = json.loads(json.dumps(result.to_data()))
    assert from_data(data) == result
    newer = table(
        {"match": {"product_name": INDOOR}, "role": "heat_source", "power_kw": 12},
        version=2,
    )
    again = from_data(data, newer)
    assert again is not None and again.primary.power_kw == 12
    assert again.table_version == 2
    for broken in (None, [], {"source": "other", "rows": []}, {"source": "cloud"}):
        assert from_data(broken) is None
    assert from_data({"source": "cloud", "rows": [["only one"]]}) is None


def test_no_serial_number_in_state_attributes_diagnostics_or_kept_data() -> None:
    values = [
        *copy.deepcopy(OWN_CLOUD),
        # A serial number reported in the name fields is never shown.
        {
            "ProductName": "SERIAL-ODD-1",
            "ModuleSerialNumber": "serial-odd-1",
            "ModuleHwIdentStr": "PRODUCT-ODD-1",
            "ProductSerialNumber": "PRODUCT-ODD-1",
        },
        {
            "ProductName": "Example",
            "ModuleHwIdentStr": "SERIAL-ODD-2",
            "No": "SERIAL-ODD-2",
        },
        {"ProductName": "83703740000878738215205", "ModuleHwIdentStr": "XCU_THH"},
    ]
    cloud = interpret(rows_from_cloud(values), "cloud", observed_at=NOW)
    local = interpret(
        rows_from_local(
            local_system(
                [
                    local_module("SERIAL-LOCAL-9", "XCU_THH", serial="SERIAL-LOCAL-9"),
                    local_module(INDOOR, "XCU_THH"),
                ]
            ).data.modules
        ),
        "local",
    )
    assert cloud is not None and local is not None
    rendered = repr(
        (
            cloud.state,
            cloud.attributes(),
            cloud.to_data(),
            heat_generator.diagnostics(cloud, local),
            local.attributes(),
        )
    ).casefold()
    for serial in (
        *SERIALS,
        "SERIAL-ODD-1",
        "PRODUCT-ODD-1",
        "SERIAL-ODD-2",
        "83703740000878738215205",
        "SERIAL-LOCAL-9",
        "SERIAL-LOCAL-1",
        "PRODUCT-LOCAL-1",
    ):
        assert serial.casefold() not in rendered, serial
    # The row named after a hardware serial keeps its name, not the serial.
    assert ("Example", None) in {
        (item.product_name, item.hardware_type) for item in cloud.generators
    }


# ------------------------------------------------------------ the entity


async def test_only_the_cloud(hass: HomeAssistant) -> None:
    coordinator = coordinator_for(hass, OWN_CLOUD)
    entity = await added(hass, coordinator)
    assert entity.unique_id == UNIQUE_ID == heat_generator_unique_id(GATEWAY)
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    assert entity.entity_registry_enabled_default is True
    assert entity.translation_key == "heat_generator"
    state = shown(hass, entity)
    assert state.state == INDOOR
    assert state.attributes["outdoor_unit"] == OUTDOOR
    assert state.attributes["primary"]["construction"] == "monobloc"
    assert state.attributes["source"] == "cloud"
    await coordinator.async_shutdown()


async def test_only_the_local_access(hass: HomeAssistant) -> None:
    local = local_system(
        [local_module(INDOOR, "XCU_THH"), local_module(OUTDOOR, "XCU_SRH")]
    )
    coordinator = coordinator_for(hass, local=[local])
    entity = await added(hass, coordinator)
    state = shown(hass, entity)
    assert state.state == INDOOR
    assert state.attributes["source"] == "local"
    assert state.attributes["known"] is True
    # An incomplete local answer is no answer.
    other = coordinator_for(
        hass, local=[local_system([local_module(INDOOR, "XCU_THH")], status="busy")]
    )
    assert HeatGeneratorInputs(other).local() is None
    await coordinator.async_shutdown()
    await other.async_shutdown()


async def test_the_newer_of_two_agreeing_answers_names_the_source(hass) -> None:
    local = local_system(
        [local_module(INDOOR, "XCU_THH"), local_module(OUTDOOR, "XCU_SRH")],
        observed_at=NOW + timedelta(minutes=1),
    )
    coordinator = coordinator_for(hass, OWN_CLOUD, local=[local])
    entity = await added(hass, coordinator)
    assert shown(hass, entity).attributes["source"] == "local"
    report = _gateway_diagnostics(1, coordinator)["heat_generator"]
    assert report["conflict"] is False
    assert report["cloud_modules"] == report["local_modules"] == 2
    await coordinator.async_shutdown()


async def test_a_device_exchange_keeps_the_entity(hass: HomeAssistant) -> None:
    coordinator = coordinator_for(hass, OWN_CLOUD)
    entity = await added(hass, coordinator)
    entity_id = entity.entity_id
    exchanged = copy.deepcopy(OWN_CLOUD)
    exchanged[0]["ProductName"] = "Example Successor 9"
    exchanged[0]["ModuleSerialNumber"] = "SERIAL-NEW"
    set_cloud(coordinator, exchanged, NOW + timedelta(days=1))
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    state = shown(hass, entity)
    assert state.entity_id == entity_id
    assert state.state == "Example Successor 9"
    assert state.attributes["known"] is False
    registry = er.async_get(hass)
    assert [
        item.entity_id
        for item in er.async_entries_for_config_entry(
            registry, coordinator.config_entry.entry_id
        )
        if item.unique_id.endswith(":heat_generator")
    ] == [entity_id]
    await coordinator.async_shutdown()


async def test_the_state_is_written_only_when_it_changes(hass) -> None:
    coordinator = coordinator_for(hass, OWN_CLOUD)
    entity = await added(hass, coordinator)
    with patch.object(entity, "async_write_ha_state") as write:
        # The same answer read again (a new snapshot, a newer time).
        set_cloud(coordinator, copy.deepcopy(OWN_CLOUD), NOW + timedelta(hours=1))
        coordinator.async_update_listeners()
        # A missing answer keeps what is shown.
        coordinator.data = {}
        coordinator.async_update_listeners()
        assert write.call_count == 0
        changed = copy.deepcopy(OWN_CLOUD)
        changed[3]["ProductName"] = "WLW-12 MB AR"
        set_cloud(coordinator, changed, NOW + timedelta(hours=2))
        coordinator.async_update_listeners()
        assert write.call_count == 1
    await coordinator.async_shutdown()


async def test_a_start_from_the_stored_inventory_keeps_the_last_state(hass) -> None:
    coordinator = coordinator_for(hass)
    # Without a module list and not registered: no entity.
    assert tracked(coordinator) == []
    entity_id = (
        er.async_get(hass)
        .async_get_or_create(
            "sensor", DOMAIN, UNIQUE_ID, config_entry=coordinator.config_entry
        )
        .entity_id
    )
    kept = own_plant()
    assert kept is not None
    mock_restore_cache_with_extra_data(
        hass,
        [(State(entity_id, INDOOR), {"heat_generators": kept.to_data()})],
    )
    # A stored start: the reading exists without its values.
    coordinator.data = {
        "/system/info": ResourceSnapshot(cloud_resource(()), True, NOW),
    }
    entity = await added(hass, coordinator)
    assert entity.entity_id == entity_id
    state = shown(hass, entity)
    assert state.state == INDOOR
    assert state.attributes["outdoor_unit"] == OUTDOOR
    assert state.attributes["source"] == "cloud"
    await coordinator.async_shutdown()


async def test_a_registered_entity_without_kept_data_is_unknown(hass) -> None:
    coordinator = coordinator_for(hass)
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, UNIQUE_ID, config_entry=coordinator.config_entry
    )
    entity = await added(hass, coordinator)
    state = shown(hass, entity)
    assert state.state == "unknown"
    # It appears with the first answer.
    set_cloud(coordinator, OWN_CLOUD)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert shown(hass, entity).state == INDOOR
    await coordinator.async_shutdown()


@pytest.mark.parametrize(
    "content",
    [None, "not json", '{"schema": 2}', '{"schema": 1, "entries": 7}', "[]"],
    ids=["missing", "not_json", "other_schema", "faulty", "not_an_object"],
)
def test_a_missing_or_faulty_table_gives_one_warning(tmp_path, caplog, content):
    path = tmp_path / "heat_generator_models.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert heat_generator.load_shipped_table(path) is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "heat generator model table" in warnings[0].getMessage()
    assert str(tmp_path) not in warnings[0].getMessage()


def test_any_error_of_the_table_gives_the_warning(monkeypatch, caplog) -> None:
    def broken(data):
        raise re.error("bad pattern")

    monkeypatch.setattr(heat_generator, "parse_table", broken)
    with caplog.at_level(logging.WARNING):
        assert heat_generator.load_shipped_table() is None
    assert len(caplog.records) == 1


def test_the_shipped_table_loads_without_a_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert heat_generator.load_shipped_table() == TABLE
    assert caplog.records == []
    assert heat_generator.TABLE_AVAILABLE is True


async def test_without_a_table_there_is_no_entity(hass, monkeypatch) -> None:
    monkeypatch.setattr(heat_generator, "TABLE_AVAILABLE", False)
    coordinator = coordinator_for(hass)
    added_entities: list = []
    track_heat_generator_sensor(
        SimpleNamespace(async_on_unload=Mock()), coordinator, added_entities.extend
    )
    set_cloud(coordinator, OWN_CLOUD)
    coordinator.async_update_listeners()
    assert added_entities == []
    # The diagnostics still work, with the empty table.
    report = heat_generator.diagnostics(
        interpret(rows_from_cloud(OWN_CLOUD), "cloud", table=EMPTY_TABLE), None
    )
    assert report["modules"] and report["known"] is False
    await coordinator.async_shutdown()


async def test_a_module_list_found_later_adds_the_entity(hass) -> None:
    coordinator = coordinator_for(hass)
    added_entities: list = []
    track_heat_generator_sensor(
        SimpleNamespace(async_on_unload=Mock()), coordinator, added_entities.extend
    )
    assert added_entities == []
    set_cloud(coordinator, OWN_CLOUD)
    coordinator.async_update_listeners()
    coordinator.async_update_listeners()
    assert len(added_entities) == 1
    await coordinator.async_shutdown()


# Shaped like an iCom answer (synthetic names, no hardware types).
ICOM_INFO = (
    {"Id": "189", "Tok": "1", "Ver": "01.00.00", "ProductName": "Gateway"},
    {"Id": "4", "Tok": "3", "Ver": "03.00.00", "ProductName": "Boiler"},
)


def _no_log(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if "heat_generator" in record.name or record.levelno >= logging.WARNING
    ]


@pytest.mark.parametrize(
    ("device_type", "values"),
    [("rrc2", ()), ("rrc2", ICOM_INFO), ("icom", ()), ("icom", OWN_CLOUD[1:3])],
)
async def test_no_module_list_or_a_room_controller_gives_no_entity_and_no_log(
    hass, caplog, device_type, values
) -> None:
    """A room controller (its list is empty) never gets the entity; any
    gateway without a named module gets none either."""
    caplog.set_level(logging.DEBUG)
    coordinator = coordinator_for(hass, values, device_type=device_type)
    caplog.clear()
    assert tracked(coordinator) == []
    coordinator.async_update_listeners()
    report = heat_generator_diagnostics(coordinator)
    if device_type == "rrc2":
        assert report is None
        assert "heat_generator" not in _gateway_diagnostics(1, coordinator)
    else:
        assert report is not None and report["modules"] == []
    assert _no_log(caplog) == []
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, UNIQUE_ID) is None
    await coordinator.async_shutdown()


async def test_an_icom_gateway_follows_the_same_rule(hass, caplog) -> None:
    """Named modules without a known hardware type: role ``other``."""
    caplog.set_level(logging.DEBUG)
    coordinator = coordinator_for(hass, ICOM_INFO, device_type="icom")
    caplog.clear()
    entity = await added(hass, coordinator)
    state = shown(hass, entity)
    assert state.state == "Boiler"  # the first named module of the list
    assert [item["role"] for item in state.attributes["heat_generators"]] == [
        "other",
        "other",
    ]
    assert state.attributes["known"] is False
    assert state.attributes["outdoor_unit"] is None
    assert _no_log(caplog) == []
    await coordinator.async_shutdown()


def test_the_role_comes_from_the_hardware_type_never_the_position() -> None:
    """The outdoor unit listed before the indoor unit."""
    outdoor_first = interpret(
        rows(
            ("Logatherm WLW MB-5 AR", "XCU_SRH"),
            ("Logatherm WLW186i-12 E", "XCU_THH"),
        ),
        "cloud",
    )
    assert outdoor_first is not None
    assert outdoor_first.state == "Logatherm WLW186i-12 E"
    assert outdoor_first.outdoor_unit == "Logatherm WLW MB-5 AR"
    # The indoor unit is documented; this spelling of the outdoor unit is
    # not, so it stays without meaning (nothing derived from the name).
    assert outdoor_first.known is False
    assert outdoor_first.primary.family == "wlw186i"
    assert outdoor_first.attributes()["primary"]["power_kw"] is None
    assert outdoor_first.generators[1].family is None
    assert outdoor_first.generators[1].power_kw is None
    # Without hardware types every module is ``other``.
    unknown = interpret(
        rows(("Logatherm WLW MB-5 AR", None), ("Logatherm WLW186i-12 E", None)),
        "cloud",
    )
    assert unknown is not None
    assert {item.role for item in unknown.generators} == {"other"}
    assert unknown.outdoor_unit is None


async def test_an_unavailable_cloud_answer_is_no_answer(hass) -> None:
    coordinator = coordinator_for(hass, OWN_CLOUD, available=False)
    assert HeatGeneratorInputs(coordinator).cloud() is None
    assert tracked(coordinator) == []
    await coordinator.async_shutdown()


async def test_the_entity_id_stays_after_a_restart(hass: HomeAssistant) -> None:
    coordinator = coordinator_for(hass, OWN_CLOUD)
    entity = await added(hass, coordinator)
    entity_id = entity.entity_id
    await entity.async_remove()
    local = local_system([local_module(INDOOR, "XCU_THH")])
    # Started again from the local access only: the same registry entry.
    again = BoschBuderusHeatGeneratorSensor(coordinator_for(hass, local=[local]))
    await _platform(hass, coordinator.config_entry).async_add_entities([again])
    assert again.entity_id == entity_id
    await coordinator.async_shutdown()
    await again.coordinator.async_shutdown()


# ------------------------------------------------------------ diagnostics


async def test_the_diagnostics_report_modules_without_serial_numbers(hass) -> None:
    observation = read(DOCUMENTED)
    coordinator = coordinator_for(hass, OWN_CLOUD, local=[observation])
    report = _gateway_diagnostics(1, coordinator)["heat_generator"]
    assert report["table_version"] == TABLE.version
    # The cloud answer is newer; the documented local example disagrees.
    assert report["conflict"] is True
    assert report["source"] == "cloud"
    assert report["unmapped_modules"] == []
    assert {item["product_name"] for item in report["other_source_modules"]} == {
        "Compress CS6800iAW 12 E",
        "AW 4 OR-S",
    }
    rendered = repr(report)
    for serial in (*SERIALS, "83703740000878738215205", "25302791000098738806125"):
        assert serial not in rendered
    assert _contains_technical_values(
        [{"technical_metadata": [], "heat_generator": report}]
    )
    assert not _contains_technical_values(
        [{"technical_metadata": [], "heat_generator": {"modules": []}}]
    )
    assert not _contains_technical_values([{"technical_metadata": []}])
    await coordinator.async_shutdown()


def test_the_anonymized_inventory_drops_the_heat_generators() -> None:
    diagnostics = inventory_diagnostics()
    diagnostics["diagnostics_schema"] = 47
    diagnostics["privacy"].update(
        contains_raw_resource_values=True,
        raw_resource_values_scope="allowlisted_technical_metadata",
    )
    result = own_plant()
    assert result is not None
    diagnostics["gateways"][0]["heat_generator"] = heat_generator.diagnostics(
        result, None
    )
    serialized = json.dumps(build_anonymized_inventory(diagnostics))
    assert INDOOR not in serialized
    assert "XCU_THH" not in serialized
