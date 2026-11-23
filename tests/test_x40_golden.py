"""X40 baseline: golden master of entities, devices, states and requests.

Recorded on the state before the plant model (``7eb6094``), so that every later
package of the plant model can show what it changes on my own K40 heat pump and
on every fixture plant: nothing except the new attribute keys of the heat
generator sensor, the diagnostics block and one stored row.

Procedure (the same for recording and comparing)
------------------------------------------------
Each plant runs once in a fresh Home Assistant test instance with the wall clock
frozen at ``FROZEN`` and the coordinator clock at a fixed start:

1. setup: the entry is set up through Home Assistant; the first discovery and
   the first data read run inside it. A PointT plant runs as a managed entry
   (format 4) against the fake cloud of ``tests/helpers/rrc_cloud.py``;
2. cycle: the coordinator clock moves on by one hour and the coordinator runs
   one regular cycle (``async_refresh``).

The local plant view of the K40 runs through the platform builders of the
managed runtime with the local capture published as current readings; the
cloud-and-local K40 is the managed cloud entry whose coordinator shows the
local capture as current local readings. Wallboxes run as managed entries
against their section reads, the separately connected cloud devices through
their account (pushed messages, no requests).

Recorded per plant (``tests/fixtures/x40-golden/<plant>.json``):

- ``entities``: entity registry entries of the entry (platform, unique ID,
  entity ID, device identifiers, category, disabled_by, translation key,
  original name);
- ``devices``: device registry entries (identifiers, parent, name, model,
  manufacturer, versions; never a serial number);
- ``states``: state and every attribute of each enabled entity after the cycle;
- ``writes``: state writes per entity in setup and cycle;
- ``requests``: per phase how often each cloud path was read (bulk and
  single), the number of bulk requests and their paths (sorted), query reads,
  recordings and writes; local requests where the harness makes any (none of
  the current harnesses reads over a local transport: ``local`` is ``None``).

Normalisation: the random config entry ID becomes ``<entry>``, the random
binding, connection, target and owner IDs of a managed entry become
``<binding>``, ``<connection>``, ``<target>`` and ``<owner>``. The wall clock
is frozen, so dates and times are not normalised. The response time sensor of
the request metrics measures real time; its state becomes ``<volatile>``
(``VOLATILE_STATES``, attribute keys in ``VOLATILE``). Bulk requests are kept as
a sorted list of sorted path lists; everything else is sorted by key.

Not part of the golden master: diagnostics (own tests), log output, the
restore cache.

Home Assistant versions: the files are recorded on 2026.11. On an older
Home Assistant (down to the minimum version 2026.5) two parts that Home
Assistant makes itself are taken out of the golden state before comparing:
the state attribute ``temperature_unit`` of climate and water heater entities
and the area in front of a generated entity ID. Both are detected by the
feature (``HA_TEMPERATURE_UNIT``, ``HA_AREA_IN_ENTITY_ID``); on 2026.11
nothing is taken out. Recording needs 2026.11.

Instructions for packages
-------------------------
Run ``tests/test_x40_golden.py``. A difference fails with a list of what
changed per plant. Never record again to make a difference go away. Instead,
extend exactly one of these allowances, each limited to what the plant model
is meant to change:

- ``NEW_ATTRIBUTES``: unique ID pattern (``re.fullmatch``) -> attribute keys an
  entity may gain. Existing attributes keep their values; the state stays. The
  plant model adds to the heat generator sensor (``r".*:heat_generator"``):
  ``contract_version``, ``state_basis``, ``plant_kind``,
  ``plant_kind_evidence``, ``fuel``, ``cascade``, ``components``,
  ``components_source``, ``components_table_version`` (a test keeps it to
  these).
- ``WRITE_DELTAS``: plant -> unique ID pattern -> state writes an entity may add
  in each phase (for example one more write of the heat generator sensor in
  the phase that adds its new attributes).
- ``EXPECTED_NEW``: plant -> ``{"entities": (...), "devices": (...),
  "requests": (...)}`` with patterns of new entities (unique ID), new devices
  (one identifier joined with ``:``) and new request paths. Only for fixture
  plants; the K40 plants (``k40_*``) never get an entry (a test checks this).
- ``POSITION_FREE``: request paths that may move to another bulk request; the
  number of bulk requests and the reads of every path stay the same
  (``/system/info``, I3).
- ``CAPTURE_ROLE_PLANTS``: the one exception for K40 plants (E3, #138). The
  capture behind ``k40_cloud`` and ``k40_cloud_local`` has every hardware
  type blanked; with the role from the type number these two plants show the
  roles, outdoor unit, outdoor power and outdoor unit software version entity
  my own K40 (``k40_own_cloud``) shows. Exactly these values are listed;
  everything else stays forbidden, and ``k40_own_cloud`` and
  ``k40_local_view`` stay unchanged (tests check both).

A new plant gets its golden file with ``X40_GOLDEN_UPDATE=1`` (that run writes
the files and fails on purpose). Recording an existing plant again is only for
a reviewed reason that is not part of the plant model.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.climate import const as climate_const
from homeassistant.components.water_heater import const as water_heater_const
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, StateMachine
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    Gateway,
    Resource,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from tests.ha_test_helpers import MockConfigEntry
from tests.helpers.heat_generator_contract import check_contract
from tests.helpers.managed_runtime import wait_for_sources
from tests.helpers.rrc_cloud import (
    FIXTURES,
    Clock,
    RrcCloud,
    cloud_patches,
    load_tree,
    with_containers,
)

GOLDEN = Path(__file__).parent / "fixtures" / "x40-golden"
PACKAGE = "custom_components.bosch_buderus_heating"
FROZEN = "2026-11-20T10:00:00+00:00"
RECORDED_AT = "7eb6094"
GATEWAY_ID = "gateway-one"
DEFAULT_GATEWAY = Gateway(GATEWAY_ID)
UPDATE = os.environ.get("X40_GOLDEN_UPDATE") == "1"

# ----------------------------------------------------------------- allowances
# Empty on the baseline. Extend only with what the plant model is meant to
# change (see above).
# Contract 1 of the heat generator sensor (S2): the new keys, and the keys of
# 0.9.1 to 0.9.3-beta.5 where a sensor without a named module showed only
# ``heat_source_type`` and ``known`` (contract 1 always has every key; a
# key that was there keeps its value).
HEAT_GENERATOR_CONTRACT_KEYS: frozenset[str] = frozenset(
    {
        "contract_version",
        "state_basis",
        "plant_kind",
        "plant_kind_evidence",
        "fuel",
        "cascade",
        "components",
        "components_source",
        "components_table_version",
        "heat_generators",
        "primary",
        "outdoor_unit",
        "source",
        "table_version",
        "known",
        "heat_source_type",
    }
)
NEW_ATTRIBUTES: dict[str, frozenset[str]] = {
    r".*:heat_generator": HEAT_GENERATOR_CONTRACT_KEYS,
}
WRITE_DELTAS: dict[str, dict[str, int]] = {}
# Targeted reads (module_probes): a fixture tree whose /system does not list
# /system/info reads it once in the first batch, and a heating plant whose hot
# water list names no circuit asks for the operation mode of dhw1 and dhw2 (R5).
_SYSTEM_INFO: tuple[str, ...] = (r"/system/info",)
_HOT_WATER: tuple[str, ...] = (r"/dhwCircuits/dhw[12]/operationMode",)
EXPECTED_NEW: dict[str, dict[str, tuple[str, ...]]] = {
    **{
        name: {"requests": _SYSTEM_INFO}
        for name in (
            "connectkey_programs_ab",
            "connectkey_variable_tariff",
            "connectkey_ventilation_program",
            "connectkey_zone_controls",
            "connectkey_zone_programs",
            "holidays_legacy",
            "icom_readings",
            "icom_season_circuit",
            "icom_tbso",
            "icom_temperature_levels",
            "rrc_ct200",
            "rrc_device_settings",
            "rrc_dhw",
            "rrc_dhw_week_program",
            "rrc_extras",
            "rrc_readings",
            "rrc_tc100",
            "rrc_zone_climate",
            "rrc_zones_programs",
            "wddw2_readings",
            "wddw2_switch_program",
        )
    },
    **{
        name: {"requests": (*_SYSTEM_INFO, *_HOT_WATER)}
        for name in (
            "connectkey_cooling_programs",
            "connectkey_programs_absolute",
            "connectkey_readings",
            "rrc_absence",
            "rrc_circuit",
            "rrc_system",
        )
    },
    "connectkey_zone_climate": {"requests": _HOT_WATER},
}
POSITION_FREE: frozenset[str] = frozenset({"/system/info"})
# E3 (#138): the capture behind k40_cloud and k40_cloud_local
# (captures/k40-cloud-2026-11-05.json) has the hardware type
# (ModuleHwIdentStr) of every module blanked. Since 0.9.3-beta.6 a module
# without a hardware type gets its role from its type number at its place, so
# these two plants now show the roles my own K40 with hardware types
# (k40_own_cloud) shows already. Exactly these differences are allowed there,
# nothing else; k40_own_cloud and k40_local_view stay unchanged.
CAPTURE_ROLE_PLANTS: frozenset[str] = frozenset({"k40_cloud", "k40_cloud_local"})
CAPTURE_ROLE_SENSOR = "sensor gateway-one:heat_generator"
# heat_generators[].role: other -> this role, per product name.
CAPTURE_ROLES: dict[str, str] = {
    "Logatherm WLW176i-12 TP70": "heat_source",
    "WLW-10 MB AR": "outdoor_unit",
}
# outdoor_unit: null -> this product name.
CAPTURE_OUTDOOR_UNIT = "WLW-10 MB AR"
# primary.outdoor_power_kw: null -> this power.
CAPTURE_OUTDOOR_POWER_KW = 10
# The new entity: the outdoor unit software version, with the state of my own
# K40 and its attributes without a hardware type.
CAPTURE_NEW_ENTITY = "gateway-one:outdoor_unit_software_version"

# ------------------------------------------------- Home Assistant versions
# The golden files are recorded on Home Assistant 2026.11. Older versions down
# to the minimum version (2026.5) differ in two things Home Assistant does
# itself, never the integration:
# - the state attribute ``temperature_unit`` of climate and water heater
#   entities (newer versions have ClimateEntityStateAttribute and
#   WaterHeaterStateAttribute and show the unit; older ones do not);
# - the area of an entity's device in front of a generated entity ID (newer
#   versions have EntityNamePart; older ones build the entity ID from the
#   device and entity name only).
# Each is detected by the feature, not by a version number. On a Home
# Assistant without the feature exactly that part is taken out of the golden
# state before comparing (``for_this_home_assistant``); with the feature
# nothing is taken out, so the comparison on 2026.11 stays complete.
HA_TEMPERATURE_UNIT: dict[str, bool] = {
    "climate": hasattr(climate_const, "ClimateEntityStateAttribute"),
    "water_heater": hasattr(water_heater_const, "WaterHeaterStateAttribute"),
}
HA_AREA_IN_ENTITY_ID: bool = hasattr(er, "EntityNamePart")
# Recording needs the Home Assistant the golden files describe.
HA_AS_RECORDED: bool = all(HA_TEMPERATURE_UNIT.values()) and HA_AREA_IN_ENTITY_ID

# Measured with the real clock of the test run, never the same twice.
VOLATILE_STATES: tuple[str, ...] = (
    r".*:request_metrics:pointt_api_response_time_last_hour",
)
VOLATILE: dict[str, frozenset[str]] = {}

_RANDOM_IDS = re.compile(r"\b(binding|connection|target|owner)_[0-9a-f]{32}\b")


# --------------------------------------------------------------------- plants
@dataclass(frozen=True, slots=True)
class Plant:
    """One plant of the golden master and how it is built."""

    name: str
    kind: str  # pointt, local_view, wallbox, bacon
    describe: str
    source: str
    build: Callable[[], Any] = field(repr=False)
    gateway: Gateway = DEFAULT_GATEWAY
    options: dict[str, Any] = field(default_factory=dict, repr=False)


def _cloud(tree: dict[str, Resource], **kwargs: Any) -> Callable[[], RrcCloud]:
    def build() -> RrcCloud:
        return RrcCloud(dict(tree), **kwargs)

    return build


def _fixture(relative: str) -> dict[str, Any]:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def _wire(items: list[dict[str, Any]]) -> dict[str, Resource]:
    return {item["id"]: parse_resource(item) for item in items}


def _cases(relative: str) -> dict[str, Resource]:
    """A plant from the payloads of a readings fixture (the first per path)."""
    payloads: dict[str, dict[str, Any]] = {}
    for case in _fixture(relative)["cases"]:
        payloads.setdefault(case["payload"]["id"], case["payload"])
    return with_containers(_wire(list(payloads.values())))


def k40_tree() -> dict[str, Resource]:
    return load_tree("captures/k40-cloud-2026-11-05.json")


def _k40_own_cloud_tree() -> dict[str, Resource]:
    """The capture with the module list of my own plant as
    ``tests/test_heat_generator.py`` states it (hardware types kept)."""
    from tests.test_heat_generator import OWN_CLOUD

    tree = k40_tree()
    tree["/system/info"] = replace(
        tree["/system/info"], values=copy.deepcopy(OWN_CLOUD), has_values=True
    )
    return tree


def _icom_tree(variant: str | None = None) -> tuple[dict[str, Resource], set[str]]:
    data = _fixture("icom/gateway-shapes.json")
    tree = _wire(data["resources"])
    if variant is not None:
        info = data["variants"][variant]["system_info"]
        tree[info["id"]] = parse_resource(info)
    return tree, set(data["unavailable"].get("403", ()))


def _icom_variants() -> tuple[str, ...]:
    return tuple(sorted(_fixture("icom/gateway-shapes.json")["variants"]))


def _aircon_tree() -> dict[str, Resource]:
    data = _fixture("pointt-rac/airconditioning.json")
    programs = _fixture("pointt-rac/switch-programs.json")
    tree = {
        path: parse_resource(payload, path=path)
        for path, payload in {**data["resources"], **programs["resources"]}.items()
    }
    for path, value in data["detection"]["midea"].items():
        tree[path] = parse_resource(
            {"id": path, "type": "stringValue", "value": value}, path=path
        )
    return with_containers(tree)


def _tc100_tree() -> dict[str, Resource]:
    from tests.test_rrc_tc100_setup import plant as tc100_plant

    return {
        **with_containers(load_tree("rrc/tc100-string-flags.json")),
        **tc100_plant(),
    }


def _plants() -> dict[str, Plant]:
    k40 = Gateway(GATEWAY_ID, device_type="k40")
    icom = Gateway(GATEWAY_ID, device_type="icom")
    k40_source = "captures/k40-cloud-2026-11-05.json"
    plants = [
        Plant(
            "k40_cloud",
            "pointt",
            "K40 heat pump of my own system (WLW176i-12 TP70), cloud capture",
            k40_source,
            _cloud(k40_tree(), forbidden=set(), missing_as_500=False),
            k40,
        ),
        Plant(
            "k40_own_cloud",
            "pointt",
            "K40 cloud capture with the module list of the heat generator tests",
            k40_source + " + tests/test_heat_generator.py OWN_CLOUD",
            _cloud(_k40_own_cloud_tree(), forbidden=set(), missing_as_500=False),
            k40,
        ),
        Plant(
            "k40_cloud_local",
            "pointt",
            "K40 cloud capture with the local capture as current local readings",
            k40_source + " + local_capture/k40rf-heat-pump-2026-11-08.jsonl",
            _cloud(k40_tree(), forbidden=set(), missing_as_500=False),
            k40,
            {"local": True},
        ),
        Plant(
            "k40_local_view",
            "local_view",
            "K40 local capture as the local plant view (cloud access off)",
            "local_capture/k40rf-heat-pump-2026-11-08.jsonl",
            lambda: None,
            Gateway("000123"),
        ),
        Plant(
            "rrc_ct200",
            "pointt",
            "RRC 2.0 controller with 11 zones and 12 radio devices",
            "rrc/ct200-shapes.json",
            _cloud(load_tree("rrc/ct200-shapes.json")),
        ),
        Plant(
            "rrc_tc100",
            "pointt",
            "TC100.2-shaped room controller (string flags, Base64 names)",
            "rrc/tc100-string-flags.json + tests/test_rrc_tc100_setup.py plant",
            _cloud(_tc100_tree(), forbidden=set(), missing_as_500=False),
            Gateway(GATEWAY_ID, model="MX400"),
            {"no_fault_probes": True},
        ),
        Plant(
            "pointt_air_conditioner",
            "pointt",
            "air conditioner on a PointT gateway (Midea detection) with programs",
            "pointt-rac/airconditioning.json + pointt-rac/switch-programs.json",
            _cloud(_aircon_tree(), forbidden=set(), missing_as_500=False),
            Gateway(GATEWAY_ID, device_type="rac"),
        ),
    ]
    for variant in (None, *_icom_variants()):
        tree, forbidden = _icom_tree(variant)
        plants.append(
            Plant(
                "icom_gateway" + (f"_{variant}" if variant else ""),
                "pointt",
                "iCom gateway with a boiler controller"
                + (f", /system/info variant {variant}" if variant else ""),
                "icom/gateway-shapes.json",
                _cloud(tree, forbidden=forbidden, missing_as_500=False),
                icom,
            )
        )
    for name, relative in (
        ("rrc_absence", "rrc/absence.json"),
        ("rrc_circuit", "rrc/circuit.json"),
        ("rrc_device_settings", "rrc/device_settings.json"),
        ("rrc_dhw", "rrc/dhw.json"),
        ("rrc_dhw_week_program", "rrc/dhw-week-program.json"),
        ("rrc_extras", "rrc/extras.json"),
        ("rrc_system", "rrc/system.json"),
        ("rrc_zone_climate", "rrc/zone_climate.json"),
        ("rrc_zones_programs", "rrc/zones-programs.json"),
        ("connectkey_cooling_programs", "connectkey/cooling-switch-programs.json"),
        ("connectkey_programs_ab", "connectkey/switch-programs-ab.json"),
        ("connectkey_programs_absolute", "connectkey/switch-programs-absolute.json"),
        (
            "connectkey_ventilation_program",
            "connectkey/ventilation-switch-program.json",
        ),
        ("connectkey_zone_climate", "connectkey/zone-climate.json"),
        ("connectkey_zone_controls", "connectkey/zone-controls.json"),
        ("connectkey_zone_programs", "connectkey/zone-switch-programs.json"),
        ("holidays_legacy", "holidays/legacy.json"),
        ("wddw2_switch_program", "wddw2/switch-program.json"),
    ):
        plants.append(
            Plant(
                name,
                "pointt",
                f"resources of {relative} below their containers",
                relative,
                _cloud(
                    with_containers(load_tree(relative)),
                    forbidden=set(),
                    missing_as_500=False,
                ),
            )
        )
    for name, relative, gateway in (
        ("connectkey_variable_tariff", "connectkey/variable-tariff-k40.json", k40),
        ("icom_season_circuit", "icom/season-circuit.json", icom),
        ("icom_tbso", "icom/tbso.json", icom),
        ("icom_temperature_levels", "icom/temperature-levels.json", icom),
    ):
        plants.append(
            Plant(
                name,
                "pointt",
                f"base resources of {relative} below their containers",
                relative,
                _cloud(
                    with_containers(_wire(_fixture(relative)["base"])),
                    forbidden=set(),
                    missing_as_500=False,
                ),
                gateway,
            )
        )
    for name, relative, gateway in (
        ("connectkey_readings", "connectkey/readings.json", Gateway(GATEWAY_ID)),
        ("icom_readings", "icom/readings.json", icom),
        ("rrc_readings", "rrc/readings.json", Gateway(GATEWAY_ID)),
        (
            "water_softener_readings",
            "water_softener/readings.json",
            Gateway(GATEWAY_ID, model="watersoftener"),
        ),
        ("wddw2_readings", "wddw2/readings.json", Gateway(GATEWAY_ID)),
    ):
        plants.append(
            Plant(
                name,
                "pointt",
                f"payloads of {relative} as one plant below their containers",
                relative,
                _cloud(_cases(relative), forbidden=set(), missing_as_500=False),
                gateway,
            )
        )
    for name, relative in (
        ("wallbox_cp0", "pointt-wallbox/cp0.json"),
        ("wallbox_cp0_control", "pointt-wallbox/cp0-control.json"),
    ):
        plants.append(
            Plant(
                name,
                "wallbox",
                f"wallbox on a PointT gateway ({relative})",
                relative,
                lambda relative=relative: _fixture(relative),
                Gateway(GATEWAY_ID, device_type="commodule"),
            )
        )
    for family, profile in (
        ("dhw", "midea"),
        ("dhw", "rwh"),
        ("dry", "dry_4000i"),
        ("dry", "dry_8000i"),
        ("purifier", "air_1001i"),
        ("purifier", "air_6001i"),
        ("rac", "ac_186i3"),
        ("rac", "cl_7000i"),
    ):
        plants.append(
            Plant(
                f"bacon_{family}_{profile}",
                "bacon",
                f"separately connected cloud device ({family}, {profile})",
                f"bacon-{family}/{profile}.json",
                lambda family=family, profile=profile: (family, profile),
            )
        )
    return {plant.name: plant for plant in plants}


PLANTS = _plants()


# ------------------------------------------------------------------ recording
class Writes:
    """State writes per entity ID in the current phase."""

    def __init__(self) -> None:
        self.phases: dict[str, Counter[str]] = {}
        self.current: Counter[str] | None = None

    def phase(self, name: str | None) -> None:
        self.current = None if name is None else self.phases.setdefault(name, Counter())

    @contextmanager
    def counting(self) -> Iterator[None]:
        original = StateMachine.async_set_internal
        writes = self

        def counted(self: StateMachine, entity_id: str, *args: Any, **kwargs: Any):
            if writes.current is not None:
                writes.current[entity_id] += 1
            return original(self, entity_id, *args, **kwargs)

        with patch.object(StateMachine, "async_set_internal", counted):
            yield


def _cloud_requests(cloud: RrcCloud) -> dict[str, Any]:
    reads: Counter[str] = Counter(path for paths in cloud.requests for path in paths)
    reads.update(cloud.single)
    queries = Counter(
        path for (method, path) in cloud.calls.elements() if method == "GET?"
    )
    return {
        "reads": dict(sorted(reads.items())),
        "read_count": sum(reads.values()),
        "bulk_count": len(cloud.requests),
        "bulks": sorted(sorted(paths) for paths in cloud.requests),
        "single": dict(sorted(Counter(cloud.single).items())),
        "queries": dict(sorted(queries.items())),
        "recordings": dict(
            sorted(
                Counter(
                    f"{path} {interval}" for path, interval in cloud.recordings
                ).items()
            )
        ),
        "writes": len(cloud.writes),
    }


def _reset(cloud: RrcCloud) -> None:
    cloud.requests.clear()
    cloud.single.clear()
    cloud.calls.clear()
    cloud.recordings.clear()
    cloud.writes.clear()


def _plain(value: Any) -> Any:
    """JSON-ready, sorted where the order carries no meaning."""
    from homeassistant.helpers.json import json_encoder_default

    if isinstance(value, Enum):
        return _plain(value.value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_plain(item) for item in value), key=repr)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return _plain(json_encoder_default(value))


def _normaliser(entry_id: str) -> Callable[[Any], Any]:
    def text(value: str) -> str:
        value = value.replace(entry_id, "<entry>")
        return _RANDOM_IDS.sub(lambda match: f"<{match.group(1)}>", value)

    def normal(value: Any) -> Any:
        if isinstance(value, str):
            return text(value)
        if isinstance(value, dict):
            return {text(key): normal(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normal(item) for item in value]
        return value

    return normal


def _key(platform: str, unique_id: str) -> str:
    return f"{platform} {unique_id}"


def _snapshot(
    hass: HomeAssistant, entry: ConfigEntry, writes: Writes
) -> dict[str, Any]:
    normal = _normaliser(entry.entry_id)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)

    def identifiers(device_id: str | None) -> list[list[str]] | None:
        device = devices.async_get(device_id) if device_id else None
        if device is None:
            return None
        return normal(sorted([list(item) for item in device.identifiers]))

    device_rows = sorted(
        (
            {
                "identifiers": identifiers(device.id),
                "via_device": identifiers(device.via_device_id),
                "name": device.name,
                "model": device.model,
                "model_id": device.model_id,
                "manufacturer": device.manufacturer,
                "sw_version": device.sw_version,
                "hw_version": device.hw_version,
                "entry_type": _plain(device.entry_type),
            }
            for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
        ),
        key=lambda row: json.dumps(row["identifiers"]),
    )
    entity_rows = []
    states: dict[str, Any] = {}
    written: dict[str, dict[str, int]] = {}
    # Only on a Home Assistant without the area in generated entity IDs: the
    # area of each entity's device (see ``for_this_home_assistant``).
    areas: dict[str, str] = {}
    for item in er.async_entries_for_config_entry(entities, entry.entry_id):
        unique_id = normal(item.unique_id)
        key = _key(item.domain, unique_id)
        if not HA_AREA_IN_ENTITY_ID:
            device = devices.async_get(item.device_id) if item.device_id else None
            area = (
                ar.async_get(hass).async_get_area(device.area_id)
                if device is not None and device.area_id
                else None
            )
            if area is not None:
                areas[key] = slugify(area.name)
        entity_rows.append(
            {
                "platform": item.domain,
                "unique_id": unique_id,
                "entity_id": item.entity_id,
                "device": identifiers(item.device_id),
                "entity_category": _plain(item.entity_category),
                "disabled_by": _plain(item.disabled_by),
                "translation_key": item.translation_key,
                "original_name": item.original_name,
            }
        )
        counts = {
            phase: counter[item.entity_id]
            for phase, counter in sorted(writes.phases.items())
            if counter[item.entity_id]
        }
        if counts:
            written[key] = counts
        state = hass.states.get(item.entity_id)
        if state is None:
            continue
        shown = {
            "state": state.state,
            "attributes": normal(_plain(dict(state.attributes))),
        }
        if any(re.fullmatch(pattern, unique_id) for pattern in VOLATILE_STATES):
            shown["state"] = "<volatile>"
        for pattern, keys in VOLATILE.items():
            if re.fullmatch(pattern, unique_id):
                for name in keys & shown["attributes"].keys():
                    shown["attributes"][name] = "<volatile>"
        states[key] = shown
    entity_rows.sort(key=lambda row: (row["platform"], row["unique_id"]))
    return {
        "entities": entity_rows,
        "devices": device_rows,
        "states": dict(sorted(states.items())),
        "writes": dict(sorted(written.items())),
        **({"areas": dict(sorted(areas.items()))} if areas else {}),
    }


# -------------------------------------------------------------------- runners
def managed_entry(hass: HomeAssistant, gateway: Gateway) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            "brand": "buderus",
            "gateway_ids": [gateway.gateway_id],
            **tokens_to_data(AuthTokens("synthetic-access", "synthetic-refresh", 4e9)),
        },
        options={"polling_profile": "standard"},
    )
    entry.add_to_hass(hass)
    return entry


def source_of(entry: ConfigEntry) -> Any:
    owner = entry.runtime_data
    (source,) = (
        owner.connections.source_data(key)
        for key in owner.connections.snapshot.bindings
    )
    return source


@contextmanager
def local_readings(extra: tuple[Any, ...] = ()) -> Iterator[None]:
    """The local capture (and ``extra`` readings) as current local readings of
    the K40 target."""
    from custom_components.bosch_buderus_heating.local_selection import (
        LocalSelection,
    )
    from custom_components.bosch_buderus_heating.managed_pointt import (
        ManagedPointTCoordinator,
    )
    from tests.test_local_capture import observations
    from tests.test_local_selection import FakeRuntime

    class _LocalRuntime(FakeRuntime):
        def subscribe(self, listener: Any, **_kwargs: Any) -> Callable[[], None]:
            return lambda: None

    original = ManagedPointTCoordinator.__init__

    def init(self: Any, source: Any, client: Any, gateway: Gateway) -> None:
        original(self, source, client, gateway)
        target = source.binding.target_id
        self.local_selection = LocalSelection(
            _LocalRuntime((*observations(), *extra), enabled=True, target=target),
            target,
        )

    with patch.object(ManagedPointTCoordinator, "__init__", init):
        yield


async def _run_pointt(
    hass: HomeAssistant, plant: Plant, writes: Writes
) -> dict[str, Any]:
    cloud = plant.build()
    clock = Clock()
    entry = managed_entry(hass, plant.gateway)
    with ExitStack() as stack:
        stack.enter_context(cloud_patches(cloud, clock, plant.gateway))
        if plant.options.get("no_fault_probes"):
            stack.enter_context(
                patch(
                    f"{PACKAGE}.coordinator.fault_resource_candidates", return_value=()
                )
            )
        if plant.options.get("local"):
            stack.enter_context(local_readings())
        writes.phase("setup")
        assert await hass.config_entries.async_setup(entry.entry_id)
        await wait_for_sources(hass, entry, 1)
        await hass.async_block_till_done()
        setup = _cloud_requests(cloud)
        _reset(cloud)
        writes.phase("cycle")
        clock.now += 3600
        await source_of(entry).async_refresh()
        await hass.async_block_till_done()
        cycle = _cloud_requests(cloud)
        writes.phase(None)
        snapshot = _snapshot(hass, entry, writes)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    snapshot["requests"] = {"cloud": {"setup": setup, "cycle": cycle}, "local": None}
    return snapshot


async def _add_platforms(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: Any
) -> list[Any]:
    """The platform builders of the managed runtime for one coordinator."""
    import importlib
    import logging
    from datetime import timedelta

    from homeassistant.const import Platform
    from homeassistant.helpers.entity_platform import EntityPlatform

    from custom_components.bosch_buderus_heating.const import PLATFORMS
    from custom_components.bosch_buderus_heating.platform_scope import PlatformScope

    platforms = []
    for platform in PLATFORMS:
        module = importlib.import_module(f"{PACKAGE}.{platform.value}")
        entity_platform = EntityPlatform(
            hass=hass,
            logger=logging.getLogger(__name__),
            domain=platform.value,
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=30),
            entity_namespace=None,
        )
        entity_platform.config_entry = entry
        entity_platform.async_prepare()
        module._setup_entities(
            PlatformScope(
                entry_id=entry.entry_id if platform is Platform.SENSOR else "",
                async_on_unload=entry.async_on_unload,
                coordinators=(coordinator,),
            ),
            entity_platform._async_schedule_add_entities_for_entry,
        )
        platforms.append(entity_platform)
    await hass.async_block_till_done()
    return platforms


async def open_local_view(
    hass: HomeAssistant, readings: tuple[Any, ...]
) -> tuple[Any, Any, list[Any]]:
    """The local plant view of a K40 with these readings published, and its
    entities added through the platform builders."""
    from tests.test_managed_local_plant import Runtime
    from tests.test_managed_local_plant import plant as local_plant

    class _Runtime(Runtime):
        def subscribe(self, listener: Any, **_kwargs: Any) -> Callable[[], None]:
            return super().subscribe(listener)

    coordinator, runtime = await local_plant(hass, _Runtime())
    runtime.publish(readings)
    platforms = await _add_platforms(hass, coordinator.config_entry, coordinator)
    await hass.async_block_till_done()
    return coordinator, runtime, platforms


async def close_local_view(
    hass: HomeAssistant, coordinator: Any, platforms: list[Any]
) -> None:
    for platform in platforms:
        await platform.async_reset()
    await coordinator.async_release_retention()
    await coordinator.async_shutdown()
    await coordinator.config_entry._async_process_on_unload(hass)
    await hass.async_block_till_done()


async def _run_local_view(
    hass: HomeAssistant, plant: Plant, writes: Writes
) -> dict[str, Any]:
    from tests.test_local_capture import observations

    writes.phase("setup")
    coordinator, runtime, platforms = await open_local_view(hass, observations())
    writes.phase("cycle")
    # The same answers read again: one more local reading cycle.
    runtime.publish(observations())
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    writes.phase(None)
    snapshot = _snapshot(hass, coordinator.config_entry, writes)
    calls = len(coordinator.client_mock.mock_calls)
    await close_local_view(hass, coordinator, platforms)
    snapshot["requests"] = {"cloud": None, "local": None, "client_calls": calls}
    return snapshot


async def _run_wallbox(
    hass: HomeAssistant, plant: Plant, writes: Writes
) -> dict[str, Any]:
    from tests.helpers.managed_runtime import wallbox_coordinators

    data = plant.build()
    calls: list[str] = []

    async def section(gateway: str, name: str) -> Any:
        calls.append(name)
        return copy.deepcopy(data[name])

    async def leaf(gateway: str, path: str) -> Any:
        from custom_components.bosch_buderus_heating.pointt import ResourceNotFound

        calls.append(path)
        if path not in data.get("leaves", {}):
            raise ResourceNotFound(path, 404)
        return copy.deepcopy(data["leaves"][path])

    entry = managed_entry(hass, plant.gateway)
    client = f"{PACKAGE}.PointTClient"
    with ExitStack() as stack:
        for item in (
            patch(f"{client}.get_gateways", AsyncMock(return_value=(plant.gateway,))),
            patch(f"{client}.get_wallbox_section", side_effect=section),
            patch(f"{client}.get_wallbox_leaf", side_effect=leaf),
            patch(f"{client}.post_wallbox_command", AsyncMock()),
            patch(f"{client}.put_wallbox_setting", AsyncMock()),
            patch(f"{PACKAGE}.async_setup_notifications", AsyncMock(return_value=())),
        ):
            stack.enter_context(item)
        writes.phase("setup")
        assert await hass.config_entries.async_setup(entry.entry_id)
        await wait_for_sources(hass, entry, 1)
        await hass.async_block_till_done()
        setup = dict(sorted(Counter(calls).items()))
        calls.clear()
        writes.phase("cycle")
        (coordinator,) = wallbox_coordinators(entry)
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        cycle = dict(sorted(Counter(calls).items()))
        writes.phase(None)
        snapshot = _snapshot(hass, entry, writes)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    snapshot["requests"] = {
        "cloud": {"setup": {"reads": setup}, "cycle": {"reads": cycle}},
        "local": None,
    }
    return snapshot


async def _run_bacon(
    hass: HomeAssistant, plant: Plant, writes: Writes
) -> dict[str, Any]:
    import asyncio
    import importlib

    from custom_components.bosch_buderus_heating.config_flow import (
        BoschBuderusConfigFlow,
    )
    from tests.test_bacon_ha import entry as bacon_entry
    from tests.test_bacon_session import emit

    family, profile = plant.build()
    module = importlib.import_module(f"tests.test_bacon_{family}")
    account, _entity, data = module.configured(profile)
    entry = bacon_entry(hass)

    async def wait() -> None:
        await asyncio.Event().wait()

    with (
        patch.object(BoschBuderusConfigFlow, "VERSION", 3),
        patch(f"{PACKAGE}.BaconAccount", return_value=account),
        patch.object(account, "run", side_effect=wait),
    ):
        writes.phase("setup")
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        setup = len(account._mqtt.publish.mock_calls)
        writes.phase("cycle")
        # A push device: the same shadow reported again.
        emit(account, "shadows/state/get/accepted", data["shadow"])
        await hass.async_block_till_done()
        cycle = len(account._mqtt.publish.mock_calls) - setup
        writes.phase(None)
        snapshot = _snapshot(hass, entry, writes)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    snapshot["requests"] = {
        "cloud": {"setup": {"publish": setup}, "cycle": {"publish": cycle}},
        "local": None,
    }
    return snapshot


RUNNERS: dict[
    str, Callable[[HomeAssistant, Plant, Writes], Awaitable[dict[str, Any]]]
] = {
    "pointt": _run_pointt,
    "local_view": _run_local_view,
    "wallbox": _run_wallbox,
    "bacon": _run_bacon,
}


async def build_snapshot(hass: HomeAssistant, plant: Plant) -> dict[str, Any]:
    """Run the defined procedure for one plant (wall clock frozen by the caller)."""
    writes = Writes()
    with writes.counting():
        return await RUNNERS[plant.kind](hass, plant, writes)


# ------------------------------------------------------------------ comparing
def _matches(value: str, patterns: tuple[str, ...] | frozenset[str]) -> bool:
    return any(re.fullmatch(pattern, value) for pattern in patterns)


def _unique_id(key: str) -> str:
    return key.split(" ", 1)[1]


def _entity_differences(
    expected: dict[str, Any], actual: dict[str, Any], new: tuple[str, ...]
) -> list[str]:
    found = []
    before = {_key(row["platform"], row["unique_id"]): row for row in expected}
    after = {_key(row["platform"], row["unique_id"]): row for row in actual}
    for key, row in before.items():
        if key not in after:
            found.append(f"entity gone: {key}")
        elif after[key] != row:
            changed = sorted(name for name in row if after[key].get(name) != row[name])
            found.append(f"entity changed: {key}: {', '.join(changed)}")
    found += [
        f"new entity: {key}"
        for key in sorted(after.keys() - before.keys())
        if not _matches(_unique_id(key), new)
    ]
    return found


def _device_differences(
    expected: list[dict[str, Any]], actual: list[dict[str, Any]], new: tuple[str, ...]
) -> list[str]:
    found = []
    before = {json.dumps(row["identifiers"]): row for row in expected}
    after = {json.dumps(row["identifiers"]): row for row in actual}
    for key, row in before.items():
        if key not in after:
            found.append(f"device gone: {key}")
        elif after[key] != row:
            changed = sorted(name for name in row if after[key].get(name) != row[name])
            found.append(f"device changed: {key}: {', '.join(changed)}")
    for key in sorted(after.keys() - before.keys()):
        if not any(_matches(":".join(item), new) for item in after[key]["identifiers"]):
            found.append(f"new device: {key}")
    return found


def _state_differences(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    found = []
    for key, before in expected.items():
        after = actual.get(key)
        if after is None:
            found.append(f"state gone: {key}")
            continue
        if after["state"] != before["state"]:
            found.append(
                f"state changed: {key}: {before['state']!r} -> {after['state']!r}"
            )
        for name, value in before["attributes"].items():
            if name not in after["attributes"]:
                found.append(f"attribute gone: {key}: {name}")
            elif after["attributes"][name] != value:
                found.append(f"attribute changed: {key}: {name}")
        allowed = frozenset().union(
            *(
                keys
                for pattern, keys in NEW_ATTRIBUTES.items()
                if re.fullmatch(pattern, _unique_id(key))
            )
        )
        extra = sorted(
            after["attributes"].keys() - before["attributes"].keys() - allowed
        )
        if extra:
            found.append(f"new attributes: {key}: {', '.join(extra)}")
    return found


def _write_differences(
    name: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
    entities: set[str],
) -> list[str]:
    found = []
    deltas = WRITE_DELTAS.get(name, {})
    for key in sorted((expected.keys() | actual.keys()) & entities):
        before = expected.get(key, {})
        after = actual.get(key, {})
        delta = sum(
            value
            for pattern, value in deltas.items()
            if re.fullmatch(pattern, _unique_id(key))
        )
        for phase in sorted(before.keys() | after.keys() | {"setup", "cycle"}):
            wanted = before.get(phase, 0) + delta
            if after.get(phase, 0) != wanted:
                found.append(
                    f"state writes: {key}: {phase}: {after.get(phase, 0)} "
                    f"instead of {wanted}"
                )
    return found


def _counted(
    label: str,
    expected: dict[str, int],
    actual: dict[str, int],
    new: tuple[str, ...],
) -> list[str]:
    found = [
        f"{label}: {path}: {actual.get(path, 0)} instead of {count}"
        for path, count in expected.items()
        if actual.get(path, 0) != count
    ]
    found += [
        f"{label}: new {path}"
        for path in sorted(actual.keys() - expected.keys())
        if not _matches(path.split(" ", 1)[0], new)
    ]
    return found


def _request_differences(
    expected: dict[str, Any] | None, actual: dict[str, Any] | None, new: tuple[str, ...]
) -> list[str]:
    if expected is None or actual is None:
        return [] if expected == actual else ["requests: channel appeared or vanished"]
    found = []
    for phase in sorted(expected.keys() | actual.keys()):
        before, after = expected.get(phase, {}), actual.get(phase, {})
        for label in sorted(before.keys() | after.keys()):
            old, current = before.get(label), after.get(label)
            if isinstance(old, dict) or isinstance(current, dict):
                found += _counted(f"{phase} {label}", old or {}, current or {}, new)
        if "bulks" in before:
            known = set(before.get("reads", {}))

            def stripped(
                bulks: list[list[str]], known: set[str] = known
            ) -> list[list[str]]:
                rows = [
                    [
                        path
                        for path in bulk
                        if path not in POSITION_FREE
                        and (path in known or not _matches(path, new))
                    ]
                    for bulk in bulks
                ]
                return sorted(row for row in rows if row)

            extra_only = sum(
                1
                for bulk in after.get("bulks", [])
                if bulk
                and all(path not in known and _matches(path, new) for path in bulk)
            )
            if after.get("bulk_count", 0) - extra_only != before["bulk_count"]:
                found.append(
                    f"{phase} bulk requests: {after.get('bulk_count')} instead of "
                    f"{before['bulk_count']}"
                )
            if stripped(after.get("bulks", [])) != stripped(before["bulks"]):
                found.append(f"{phase} bulk requests: other paths together")
        for label in ("writes", "publish"):
            if before.get(label) != after.get(label):
                found.append(
                    f"{phase} {label}: {after.get(label)} "
                    f"instead of {before.get(label)}"
                )
    return found


def _with_capture_roles(expected: dict[str, Any]) -> dict[str, Any]:
    """The golden state of a capture plant with exactly the changes E3 makes
    (see ``CAPTURE_ROLE_PLANTS``); the new entity as on my own K40."""
    changed = copy.deepcopy(expected)
    sensor = changed["states"][CAPTURE_ROLE_SENSOR]
    attributes = sensor["attributes"]
    for item in attributes["heat_generators"]:
        if item["role"] == "other" and item["product_name"] in CAPTURE_ROLES:
            item["role"] = CAPTURE_ROLES[item["product_name"]]
    if attributes["outdoor_unit"] is None:
        attributes["outdoor_unit"] = CAPTURE_OUTDOOR_UNIT
    if attributes["primary"]["outdoor_power_kw"] is None:
        attributes["primary"]["outdoor_power_kw"] = CAPTURE_OUTDOOR_POWER_KW
    own = json.loads(_path("k40_own_cloud").read_text(encoding="utf-8"))
    key = f"sensor {CAPTURE_NEW_ENTITY}"
    row = next(
        item
        for item in own["entities"]
        if (item["platform"], item["unique_id"]) == ("sensor", CAPTURE_NEW_ENTITY)
    )
    # Entity ID and name follow the device of this plant ("Heating").
    generator = next(
        item
        for item in changed["entities"]
        if item["unique_id"] == CAPTURE_ROLE_SENSOR.split(" ", 1)[1]
    )
    prefix = sensor["attributes"]["friendly_name"].removesuffix("Heat generator")
    changed["entities"].append(
        {
            **row,
            "entity_id": generator["entity_id"].removesuffix("heat_generator")
            + "outdoor_unit_software_version",
        }
    )
    state = copy.deepcopy(own["states"][key])
    state["attributes"]["friendly_name"] = prefix + row["original_name"]
    state["attributes"]["hardware_type"] = None
    changed["states"][key] = state
    changed["writes"][key] = dict(own["writes"][key])
    return changed


def for_this_home_assistant(
    expected: dict[str, Any], areas: dict[str, str]
) -> dict[str, Any]:
    """The golden state as the running Home Assistant shows it: without the
    parts an older Home Assistant does not make (see ``HA_TEMPERATURE_UNIT``
    and ``HA_AREA_IN_ENTITY_ID``); unchanged on the recorded version.
    ``areas``: the area of each entity's device in this run (slug)."""
    if HA_AS_RECORDED:
        return expected
    changed = copy.deepcopy(expected)
    for key, state in changed["states"].items():
        domain = key.split(" ", 1)[0]
        if not HA_TEMPERATURE_UNIT.get(domain, True):
            state["attributes"].pop("temperature_unit", None)
    if not HA_AREA_IN_ENTITY_ID:
        for row in changed["entities"]:
            area = areas.get(_key(row["platform"], row["unique_id"]))
            domain, _, object_id = row["entity_id"].partition(".")
            if area and object_id.startswith(f"{area}_"):
                row["entity_id"] = f"{domain}.{object_id.removeprefix(f'{area}_')}"
    return changed


def differences(
    name: str, expected: dict[str, Any], actual: dict[str, Any]
) -> list[str]:
    """Everything that differs beyond the allowances, as readable lines."""
    expected = for_this_home_assistant(expected, actual.get("areas", {}))
    if name in CAPTURE_ROLE_PLANTS:
        expected = _with_capture_roles(expected)
    new = EXPECTED_NEW.get(name, {})
    found = _entity_differences(
        expected["entities"], actual["entities"], new.get("entities", ())
    )
    found += _device_differences(
        expected["devices"], actual["devices"], new.get("devices", ())
    )
    found += _state_differences(expected["states"], actual["states"])
    known = {_key(row["platform"], row["unique_id"]) for row in expected["entities"]}
    found += _write_differences(name, expected["writes"], actual["writes"], known)
    for channel in sorted(expected["requests"].keys() | actual["requests"].keys()):
        before = expected["requests"].get(channel)
        after = actual["requests"].get(channel)
        if isinstance(before, dict) or isinstance(after, dict):
            found += [
                f"{channel} {line}"
                for line in _request_differences(before, after, new.get("requests", ()))
            ]
        elif before != after:
            found.append(f"{channel}: {after!r} instead of {before!r}")
    return found


def _path(name: str) -> Path:
    return GOLDEN / f"{name}.json"


def _provenance(plant: Plant) -> dict[str, Any]:
    source = plant.source.split(" ")[0]
    stated: dict[str, Any] = {}
    if source.endswith(".json") and (FIXTURES / source).exists():
        stated = _fixture(source).get("provenance", {})
    return {
        "kind": "test_snapshot",
        "physical_device_verified": False,
        "recorded_at": RECORDED_AT,
        "recorded_on": "2026-11-22",
        "plant": plant.describe,
        "source": plant.source,
        "device_class": f"{plant.kind}:"
        + (plant.gateway.device_type or plant.gateway.model or "unspecified"),
        "firmware": stated.get("firmware"),
        "procedure": "tests/test_x40_golden.py: setup with the first discovery, "
        "then one coordinator cycle one hour later; wall clock frozen at " + FROZEN,
        "normalisation": "entry ID and managed-entry IDs replaced; request metric "
        "response times marked <volatile>",
    }


# ---------------------------------------------------------------------- tests
@pytest.mark.parametrize("name", sorted(PLANTS))
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_plant_matches_the_state_before_the_plant_model(
    hass: HomeAssistant, freezer: Any, name: str
) -> None:
    freezer.move_to(FROZEN)
    plant = PLANTS[name]
    actual = await build_snapshot(hass, plant)
    if UPDATE:
        assert HA_AS_RECORDED, "record only on the Home Assistant of the golden files"
        text = json.dumps(
            {"provenance": _provenance(plant), **actual},
            indent=1,
            sort_keys=True,
            ensure_ascii=False,
        )
        GOLDEN.mkdir(exist_ok=True)
        _path(name).write_bytes((text + "\n").encode("utf-8"))
        pytest.fail(f"{name}: golden file written; run again without X40_GOLDEN_UPDATE")
    expected = json.loads(_path(name).read_text(encoding="utf-8"))
    found = differences(name, expected, actual)
    assert not found, f"{name}:\n" + "\n".join(found)
    # Every heat generator sensor of every plant keeps to contract 1.
    for key, state in actual["states"].items():
        if key.endswith(":heat_generator"):
            check_contract(state["attributes"])


def test_every_plant_has_its_golden_file_and_nothing_else() -> None:
    files = {path.stem for path in GOLDEN.glob("*.json")}
    assert files == set(PLANTS)


def test_the_k40_plants_never_get_new_entities_or_requests() -> None:
    assert not [name for name in EXPECTED_NEW if name.startswith("k40")]
    assert set(EXPECTED_NEW) <= set(PLANTS)
    assert set(WRITE_DELTAS) <= set(PLANTS)
    for allowance in EXPECTED_NEW.values():
        assert set(allowance) <= {"entities", "devices", "requests"}


def test_the_capture_exception_covers_the_two_capture_plants_only() -> None:
    """E3: only the plants of the capture with blanked hardware types, and
    only towards what my own K40 with hardware types shows."""
    assert frozenset({"k40_cloud", "k40_cloud_local"}) == CAPTURE_ROLE_PLANTS
    assert "k40_own_cloud" not in CAPTURE_ROLE_PLANTS
    assert "k40_local_view" not in CAPTURE_ROLE_PLANTS
    own = json.loads(_path("k40_own_cloud").read_text(encoding="utf-8"))
    attributes = own["states"][CAPTURE_ROLE_SENSOR]["attributes"]
    assert {
        item["product_name"]: item["role"] for item in attributes["heat_generators"]
    } == CAPTURE_ROLES
    assert attributes["outdoor_unit"] == CAPTURE_OUTDOOR_UNIT
    assert attributes["primary"]["outdoor_power_kw"] == CAPTURE_OUTDOOR_POWER_KW
    for name in CAPTURE_ROLE_PLANTS:
        before = json.loads(_path(name).read_text(encoding="utf-8"))
        # The golden files themselves keep the state before E3.
        assert {
            item["role"]
            for item in before["states"][CAPTURE_ROLE_SENSOR]["attributes"][
                "heat_generators"
            ]
        } == {"other"}
        assert f"sensor {CAPTURE_NEW_ENTITY}" not in before["states"]


@pytest.mark.usefixtures("recorded_home_assistant")
def test_the_capture_exception_allows_nothing_else() -> None:
    """Another role, another value or the same change on another K40 plant
    is still a difference."""
    for name in ("k40_own_cloud", "k40_local_view"):
        golden = json.loads(_path(name).read_text(encoding="utf-8"))
        changed = copy.deepcopy(golden)
        (sensor,) = (key for key in golden["states"] if key.endswith(":heat_generator"))
        changed["states"][sensor]["attributes"]["known"] = "X"
        assert differences(name, golden, changed) == [
            f"attribute changed: {sensor}: known"
        ]
    golden = json.loads(_path("k40_cloud").read_text(encoding="utf-8"))
    allowed = _with_capture_roles(golden)
    assert differences("k40_cloud", golden, allowed) == []
    wrong = copy.deepcopy(allowed)
    wrong["states"][CAPTURE_ROLE_SENSOR]["attributes"]["heat_generators"][0]["role"] = (
        "boiler"
    )
    assert differences("k40_cloud", golden, wrong) == [
        f"attribute changed: {CAPTURE_ROLE_SENSOR}: heat_generators"
    ]
    wrong = copy.deepcopy(allowed)
    wrong["states"][f"sensor {CAPTURE_NEW_ENTITY}"]["state"] = "1.0"
    assert differences("k40_cloud", golden, wrong) == [
        f"state changed: sensor {CAPTURE_NEW_ENTITY}: "
        f"{allowed['states'][f'sensor {CAPTURE_NEW_ENTITY}']['state']!r} -> '1.0'"
    ]
    wrong = copy.deepcopy(allowed)
    wrong["states"][CAPTURE_ROLE_SENSOR]["attributes"]["primary"]["family"] = "x"
    assert differences("k40_cloud", golden, wrong) == [
        f"attribute changed: {CAPTURE_ROLE_SENSOR}: primary"
    ]


@pytest.fixture
def recorded_home_assistant() -> Iterator[None]:
    """Tests that compare golden files with each other: as recorded."""
    with _home_assistant(recorded=True):
        yield


@contextmanager
def _home_assistant(recorded: bool) -> Iterator[None]:
    """Compare as on the recorded Home Assistant, or as on an older one."""
    with (
        patch.dict(HA_TEMPERATURE_UNIT, dict.fromkeys(HA_TEMPERATURE_UNIT, recorded)),
        patch(f"{__name__}.HA_AREA_IN_ENTITY_ID", recorded),
        patch(f"{__name__}.HA_AS_RECORDED", recorded),
    ):
        yield


def test_an_older_home_assistant_differs_only_in_its_own_parts() -> None:
    """Without the two features exactly their parts are taken out; every
    other difference stays one, also on an older Home Assistant."""
    golden = json.loads(_path("rrc_ct200").read_text(encoding="utf-8"))
    units = [
        key
        for key, state in golden["states"].items()
        if key.split(" ", 1)[0] in ("climate", "water_heater")
        and "temperature_unit" in state["attributes"]
    ]
    assert units
    entity = next(row for row in golden["entities"] if row["entity_id"].count("_") >= 2)
    domain, _, object_id = entity["entity_id"].partition(".")
    area, _, rest = object_id.partition("_")
    key = _key(entity["platform"], entity["unique_id"])
    # The same plant as an older Home Assistant shows it.
    older = copy.deepcopy(golden)
    for unit in units:
        del older["states"][unit]["attributes"]["temperature_unit"]
    next(row for row in older["entities"] if row["unique_id"] == entity["unique_id"])[
        "entity_id"
    ] = f"{domain}.{rest}"
    older["areas"] = {key: area}
    with _home_assistant(recorded=True):
        assert differences("rrc_ct200", golden, golden) == []
        found = differences("rrc_ct200", golden, older)
        assert f"entity changed: {key}: entity_id" in found
        assert {f"attribute gone: {unit}: temperature_unit" for unit in units} <= set(
            found
        )
    with _home_assistant(recorded=False):
        assert differences("rrc_ct200", golden, older) == []
        # Another area, another attribute or another unit stays a difference.
        wrong = copy.deepcopy(older)
        wrong["areas"] = {key: "other"}
        assert differences("rrc_ct200", golden, wrong) == [
            f"entity changed: {key}: entity_id"
        ]
        wrong = copy.deepcopy(older)
        attributes = wrong["states"][units[0]]["attributes"]
        name = next(item for item in sorted(attributes) if item != "friendly_name")
        attributes[name] = "changed"
        assert differences("rrc_ct200", golden, wrong) == [
            f"attribute changed: {units[0]}: {name}"
        ]
        wrong = copy.deepcopy(older)
        wrong["states"][units[0]]["attributes"]["temperature_unit"] = "°F"
        assert differences("rrc_ct200", golden, wrong) == [
            f"new attributes: {units[0]}: temperature_unit"
        ]


def test_the_allowances_start_empty_or_name_planned_keys() -> None:
    """Only the keys the plant model adds to the heat generator sensor, and
    the keys of 0.9.1 to 0.9.3-beta.5 its contract 1 always shows."""
    planned = {
        "contract_version",
        "state_basis",
        "plant_kind",
        "plant_kind_evidence",
        "fuel",
        "cascade",
        "components",
        "components_source",
        "components_table_version",
        # Contract 1 always has every key: a sensor without a named module
        # showed only heat_source_type and known before.
        "heat_generators",
        "primary",
        "outdoor_unit",
        "source",
        "table_version",
        "known",
        "heat_source_type",
    }
    for pattern, keys in NEW_ATTRIBUTES.items():
        assert pattern == r".*:heat_generator", pattern
        assert keys <= planned


def test_the_comparison_finds_each_kind_of_difference() -> None:
    """The comparison itself, on a small constructed snapshot."""
    base: dict[str, Any] = {
        "entities": [
            {"platform": "sensor", "unique_id": "gw:a", "entity_id": "sensor.a"},
            {
                "platform": "sensor",
                "unique_id": "gw:heat_generator",
                "entity_id": "sensor.h",
            },
        ],
        "devices": [{"identifiers": [["d", "gw"]], "name": "Heating"}],
        "states": {
            "sensor gw:a": {"state": "1", "attributes": {"unit": "C"}},
            "sensor gw:heat_generator": {"state": "X", "attributes": {"known": True}},
        },
        "writes": {"sensor gw:a": {"setup": 1}},
        "requests": {
            "cloud": {
                "setup": {
                    "reads": {"/a": 1, "/system/info": 1},
                    "bulk_count": 2,
                    "bulks": [["/a"], ["/b", "/system/info"]],
                },
            },
            "local": None,
        },
    }
    base["requests"]["cloud"]["setup"]["reads"]["/b"] = 1
    assert differences("k40_own_cloud", base, copy.deepcopy(base)) == []
    changed = copy.deepcopy(base)
    changed["entities"].pop(0)
    changed["states"]["sensor gw:heat_generator"]["attributes"]["plant_kind"] = None
    changed["states"]["sensor gw:heat_generator"]["state"] = "Y"
    changed["writes"]["sensor gw:a"] = {"setup": 2}
    changed["requests"]["cloud"]["setup"]["reads"]["/c"] = 1
    changed["devices"][0]["name"] = "Other"
    # The comparison itself, without the allowances of the plant model.
    with patch.dict(NEW_ATTRIBUTES, {}, clear=True):
        found = differences("k40_own_cloud", base, changed)
    assert "entity gone: sensor gw:a" in found
    assert "new attributes: sensor gw:heat_generator: plant_kind" in found
    assert "state changed: sensor gw:heat_generator: 'X' -> 'Y'" in found
    assert "state writes: sensor gw:a: setup: 2 instead of 1" in found
    assert "cloud setup reads: new /c" in found
    assert any(line.startswith("device changed:") for line in found)
    # /system/info may move to another bulk request (I3), nothing else.
    moved = copy.deepcopy(base)
    moved["requests"]["cloud"]["setup"]["bulks"] = [["/a", "/system/info"], ["/b"]]
    assert differences("k40_own_cloud", base, moved) == []
    regrouped = copy.deepcopy(base)
    regrouped["requests"]["cloud"]["setup"]["bulks"] = [["/a", "/b", "/system/info"]]
    regrouped["requests"]["cloud"]["setup"]["bulk_count"] = 1
    assert differences("k40_own_cloud", base, regrouped) == [
        "cloud setup bulk requests: 1 instead of 2",
        "cloud setup bulk requests: other paths together",
    ]
    with patch.dict(NEW_ATTRIBUTES, {r".*:heat_generator": frozenset({"plant_kind"})}):
        allowed = copy.deepcopy(base)
        allowed["states"]["sensor gw:heat_generator"]["attributes"]["plant_kind"] = None
        assert differences("k40_own_cloud", base, allowed) == []
    with patch.dict(WRITE_DELTAS, {"k40_own_cloud": {"gw:a": 1}}):
        assert differences("k40_own_cloud", base, base) == [
            "state writes: sensor gw:a: cycle: 0 instead of 1",
            "state writes: sensor gw:a: setup: 1 instead of 2",
        ]
    with patch.dict(
        EXPECTED_NEW, {"rrc_ct200": {"entities": (r"gw:new",), "requests": (r"/c",)}}
    ):
        grown = copy.deepcopy(base)
        grown["entities"].append(
            {"platform": "sensor", "unique_id": "gw:new", "entity_id": "sensor.n"}
        )
        grown["requests"]["cloud"]["setup"]["reads"]["/c"] = 1
        grown["requests"]["cloud"]["setup"]["bulks"].append(["/c"])
        grown["requests"]["cloud"]["setup"]["bulk_count"] = 3
        assert differences("rrc_ct200", base, grown) == []
        assert differences("rrc_tc100", base, grown) == [
            "new entity: sensor gw:new",
            "cloud setup reads: new /c",
            "cloud setup bulk requests: 3 instead of 2",
            "cloud setup bulk requests: other paths together",
        ]
