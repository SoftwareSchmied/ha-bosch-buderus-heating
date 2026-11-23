"""Optional devices per system component (S4): option, lifecycle, cleanup,
diagnostics and the dialog, in the older entry format and in format 4.

Component rows are constructed (no values of a real system). The K40 runs
use the capture fixture of the golden master (``tests/test_x40_golden.py``).
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterable, Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating import (
    _consolidate_logical_devices,
    _remove_unconfigured_gateway_entries,
    _remove_unselected_gateway_entries,
    async_remove_config_entry_device,
    component_devices,
)
from custom_components.bosch_buderus_heating.component_devices import (
    OPTION,
    component_identifier,
    device_components,
    device_name,
    enable,
    enabled,
    identifier,
    managers_of,
    offered,
    parse_identifier,
    place_label,
    remove_all,
    remove_for_other_gateways,
    software_version,
    track_component_devices,
)
from custom_components.bosch_buderus_heating.component_rows import ComponentRow
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.coordinator import (
    BoschBuderusDataUpdateCoordinator,
)
from custom_components.bosch_buderus_heating.diagnostics import (
    _with_component_devices,
    async_get_device_diagnostics,
)
from custom_components.bosch_buderus_heating.plant_inputs import ShownComponents
from custom_components.bosch_buderus_heating.pointt import Gateway
from custom_components.bosch_buderus_heating.system_components import load_table
from tests.ha_test_helpers import MockConfigEntry
from tests.helpers.options_menu import menu_note, outcome
from tests.system_components_variants import (
    each_name_entry_alone,
    each_name_entry_removed,
)
from tests.test_entity_retirement import _BASE, _PLANT, _snapshot

GW = "gateway-one"
CD = "custom_components.bosch_buderus_heating.component_devices"
# Constructed rows in the shape of a K40 heat pump: two components at the
# place of the heat generator control unit, a system controller and the two
# rows of the gateway itself.
ROWS = (
    ComponentRow(8, 8, "1.2.3-abc", "Buderus Testgerät 7"),
    ComponentRow(253, 16, "4.5-FIELD"),
    ComponentRow(17, 72, "9.9"),
    ComponentRow(17, 75),
    ComponentRow(65281, 8, "6.7.8-x", "Test outdoor unit"),
)
EXPECTED = {
    f"{GW}:component:8:8",
    f"{GW}:component:8:65281",
    f"{GW}:component:16:253",
}


# ------------------------------------------------------------------ pure


def test_identifier_never_uses_a_prefix_of_another_device() -> None:
    value = identifier(GW, 8, 65281)
    assert value == f"{GW}:component:8:65281"
    assert parse_identifier(value) == (GW, 8, 65281)
    # M30: never a prefix the merge of logical devices removes.
    for kind in ("heating_circuit", "hot_water_circuit", "heat_source"):
        assert not value.startswith(f"{GW}:{kind}:")
    assert value.split(":", 1)[0] == GW
    for other in (
        GW,
        f"{GW}:devices:device3",
        f"{GW}:heat_source:hs1",
        f"{GW}:component:x:1",
        f"{GW}:component:1",
        f"{GW}:component:1234567:1",
        None,
        17,
    ):
        assert parse_identifier(other) is None


def test_gateway_rows_type_zero_and_incomplete_rows_get_no_device() -> None:
    rows = (
        *ROWS,
        ComponentRow(0, 16),
        ComponentRow(None, 16),
        ComponentRow(8, None),
        ComponentRow(17, 9),  # type 17 is the gateway at any place
        ComponentRow(4711, 72),  # place 72 is the gateway's
    )
    found = device_components(rows)
    assert set(found) == {(8, 8), (8, 65281), (16, 253)}
    assert found[(16, 253)].place_key == "system_controller"
    assert found[(16, 253)].model == "HMI800_2_HP"
    assert found[(8, 65281)].role == "outdoor_unit"


def test_versions_are_shown_whole_after_the_technical_check() -> None:
    assert software_version("12.11.1-2fc9") == "12.11.1-2fc9"
    assert software_version("47.12-FIELD") == "47.12-FIELD"
    assert software_version("15.00.01") == "15.00.01"
    assert software_version("  9.16.0-dab9f463 ") == "9.16.0-dab9f463"
    assert software_version("V01.012.05") == "V01.012.05"
    assert software_version("1" + ".2" * 31) == "1" + ".2" * 31  # 63 characters
    for value in (
        None,
        "",
        "abc",  # no digit
        "1.2 3",  # a space inside
        "1.2.3\x00",  # a control character drops the whole text
        "1.2\u200b.3",  # so does any other unprintable character
        "1.2;3",
        "1" + ".2" * 32,  # longer than 64 characters
    ):
        assert software_version(value) is None, value


def test_duplicates_share_one_device_in_any_order() -> None:
    rows = (
        *ROWS,
        ComponentRow(65281, 8, "6.7.8-x", "Test outdoor unit"),
        ComponentRow(253, 16, "4.6"),
    )
    results = [
        device_components(order)
        for order in itertools.islice(itertools.permutations(rows), 0, 5040, 37)
    ]
    assert all(result == results[0] for result in results)  # M35
    found = results[0]
    assert found[(8, 65281)].count == 2
    # The same version twice: shown.
    assert found[(8, 65281)].version == "6.7.8-x"
    assert not found[(8, 65281)].versions_differ
    # The whole text counts: another build of the same version differs.
    other = device_components(
        (*ROWS, ComponentRow(65281, 8, "6.7.8-y", "Test outdoor unit"))
    )
    assert other[(8, 65281)].version is None and other[(8, 65281)].versions_differ
    # Different versions of one kind at one place: none.
    assert found[(16, 253)].version is None and found[(16, 253)].versions_differ
    assert found[(8, 8)].count == 1


def test_names_follow_the_server_language() -> None:
    found = device_components(ROWS)
    assert device_name(found[(16, 253)], "en") == "System controller HMI800_2_HP"
    assert device_name(found[(16, 253)], "de") == "Systemregler HMI800_2_HP"
    assert device_name(found[(16, 253)], "cs") == "Systémový regulátor HMI800_2_HP"
    # C8: a product name without the brand.
    assert device_name(found[(8, 8)], "en") == "Testgerät 7"
    unknown = device_components([ComponentRow(999_998, 999_997)])
    assert device_name(unknown[(999_997, 999_998)], "en") == "Component 999998"
    assert device_name(unknown[(999_997, 999_998)], "de") == "Komponente 999998"
    brand_only = device_components([ComponentRow(253, 16, None, "Buderus")])
    assert device_name(brand_only[(16, 253)], "en") == "System controller HMI800_2_HP"
    assert place_label("remote_hc2", "en") == "Remote control heating circuit 2"
    assert place_label("remote_hc3_legacy", "de") == "Fernbedienung Heizkreis 3"
    assert place_label("hybrid_manager", "sv") == "Hybridstyrning"
    for key in (
        "heat_generator_control",
        "basic_controller",
        "pool",
        "fresh_water",
        "solar",
        "radio",
        "ventilation",
    ):
        assert place_label(key, "fr") != place_label(None, "fr")
    alias = device_components([ComponentRow(103, 9)])
    assert alias[(9, 103)].model == "IPM2 / ISM2"


def _all_rows() -> list[ComponentRow]:
    table = load_table()
    assert table is not None
    places = sorted(table.places)
    return [
        ComponentRow(type_id, places[index % len(places)])
        for index, type_id in enumerate(sorted(table.components))
    ]


def test_name_entries_never_change_which_devices_exist() -> None:
    """I5: every entry known by its name removed or alone: the same devices."""
    rows = _all_rows()
    base = set(device_components(rows))
    assert len(base) > 100
    for type_id, table in itertools.chain(
        each_name_entry_removed(), each_name_entry_alone()
    ):
        assert set(device_components(rows, table)) == base, type_id


# ------------------------------------------------------------- lifecycle


class FakeCoordinator:
    """The parts of a coordinator the component devices use."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        gateway: str = GW,
        *,
        read_only: bool = False,
    ) -> None:
        self.hass = hass
        self.config_entry = entry
        self.plant_model = SimpleNamespace(
            gateway_device=SimpleNamespace(identifier=gateway)
        )
        self.read_only_view = read_only
        self.rows: tuple[ComponentRow, ...] | None = ()
        self._listeners: list[Any] = []

    def async_add_listener(self, listener: Any) -> Any:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def cycle(self, rows: Iterable[ComponentRow] | None = None) -> None:
        """An update pass, with a new answer when ``rows`` is given."""
        if rows is not None:
            self.rows = tuple(rows)
        for listener in list(self._listeners):
            listener()


def _shown(coordinator: Any) -> ShownComponents | None:
    rows = getattr(coordinator, "rows", None)
    if rows is None:
        return None
    return ShownComponents("cloud", rows, False, 1)


@pytest.fixture(autouse=True)
def fake_plant():
    tracker = SimpleNamespace(async_load=AsyncMock())
    with (
        patch(f"{CD}.components", side_effect=_shown),
        patch(f"{CD}.existing_tracker", return_value=tracker),
    ):
        yield


def _entry(hass: HomeAssistant, *, on: bool = True, version: int = 3):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=version,
        data={"brand": "buderus", "gateway_ids": [GW]},
        options={OPTION: True} if on else {},
    )
    entry.add_to_hass(hass)
    return entry


def _gateway(hass: HomeAssistant, entry, gateway: str = GW):
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, gateway)},
        manufacturer="Buderus",
        name="Heating",
    )


async def _start(hass, entry, rows=ROWS, gateway=GW, **kwargs):
    coordinator = FakeCoordinator(hass, entry, gateway, **kwargs)
    coordinator.rows = tuple(rows) if rows is not None else None
    scope = SimpleNamespace(unloads=[])
    scope.async_on_unload = scope.unloads.append
    manager = track_component_devices(scope, coordinator)
    await hass.async_block_till_done()
    return coordinator, manager, scope


def _components(hass, entry) -> dict[str, dr.DeviceEntry]:
    return {
        identifier(*parsed): device
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
        if (parsed := component_identifier(device)) is not None
    }


def _registry(hass) -> list[tuple]:
    devices = dr.async_get(hass).devices
    # Older releases iterate device IDs; the entries are the values there.
    entries = devices.values() if isinstance(devices, Mapping) else devices
    return sorted(
        (
            sorted(device.identifiers),
            device.name,
            device.via_device_id,
            device.sw_version,
            device.model,
        )
        for device in entries
    )


async def test_off_by_default_nothing_changes(hass: HomeAssistant) -> None:
    entry = _entry(hass, on=False)
    assert not enabled(entry)
    _gateway(hass, entry)
    before = _registry(hass)
    coordinator, manager, _ = await _start(hass, entry)
    coordinator.cycle(ROWS)
    assert manager is not None and manager.sync() == 0
    assert _registry(hass) == before  # M29
    assert enable(hass, entry) == 0  # the option itself is still off


async def test_on_creates_info_devices_below_the_gateway(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    gateway = _gateway(hass, entry)
    await _start(hass, entry)
    found = _components(hass, entry)
    assert set(found) == EXPECTED
    for device in found.values():
        assert device.via_device_id == gateway.id
        assert device.serial_number is None  # M33
        assert device.manufacturer == "Buderus"
        assert device.hw_version is None
        # No entity sits on a component device.
        assert not er.async_entries_for_device(
            er.async_get(hass), device.id, include_disabled_entities=True
        )
    system = found[f"{GW}:component:16:253"]
    assert system.name == "System controller HMI800_2_HP"
    assert system.model == "HMI800_2_HP" and system.model_id == "253"
    assert system.sw_version == "4.5-FIELD"
    assert found[f"{GW}:component:8:8"].name == "Testgerät 7"
    assert found[f"{GW}:component:8:8"].sw_version == "1.2.3-abc"
    assert found[f"{GW}:component:8:65281"].sw_version == "6.7.8-x"
    # The gateway's own rows give no device, so their version is nowhere.
    assert "9.9" not in {device.sw_version for device in found.values()}


async def test_on_off_on_restores_names_and_areas(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    gateway = _gateway(hass, entry)
    other = MockConfigEntry(domain="other_domain")
    other.add_to_hass(hass)
    registry = dr.async_get(hass)
    foreign = registry.async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("other_domain", f"{GW}:component:8:8")},
    )
    radio = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{GW}:devices:device3")}
    )
    await _start(hass, entry)
    found = _components(hass, entry)
    room = ar.async_get(hass).async_create("Technik")
    mine = found[f"{GW}:component:16:253"]
    registry.async_update_device(
        mine.id, name_by_user="Mein Regler", area_id=room.id, labels={"heizung"}
    )
    hass.config_entries.async_update_entry(entry, options={OPTION: False})
    assert remove_all(hass, entry) == 3  # also the customised one (M32)
    assert _components(hass, entry) == {}
    # M31: the gateway's, another integration's and other devices stay.
    for kept in (gateway, foreign, radio):
        assert registry.async_get(kept.id) is not None
    assert remove_all(hass, entry) == 0
    hass.config_entries.async_update_entry(entry, options={OPTION: True})
    assert enable(hass, entry) == 3
    again = _components(hass, entry)
    assert set(again) == EXPECTED
    back = again[f"{GW}:component:16:253"]
    assert back.name_by_user == "Mein Regler"
    assert back.area_id == room.id and back.labels == {"heizung"}


async def test_the_parent_comes_first_and_links_later(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    registry = dr.async_get(hass)
    # Left from earlier without its link (for example created before the
    # gateway's device existed in an older run).
    loose = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{GW}:component:16:253")}
    )
    # One no longer listed: only the link at the start reaches it.
    gone = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{GW}:component:59:4")}
    )
    coordinator, _, _ = await _start(hass, entry)
    assert set(_components(hass, entry)) == {
        f"{GW}:component:16:253",
        f"{GW}:component:59:4",
    }
    gateway = _gateway(hass, entry)
    coordinator.cycle()
    found = _components(hass, entry)
    assert set(found) == {*EXPECTED, f"{GW}:component:59:4"}
    assert all(device.via_device_id == gateway.id for device in found.values())
    assert registry.async_get(loose.id).via_device_id == gateway.id
    assert registry.async_get(gone.id).via_device_id == gateway.id


async def test_a_deleted_device_comes_back_only_at_the_next_start(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    _gateway(hass, entry)
    coordinator, _, scope = await _start(hass, entry)
    registry = dr.async_get(hass)
    deleted = _components(hass, entry)[f"{GW}:component:16:253"]
    # D42: a component device has no entity, so the user may delete it.
    assert await async_remove_config_entry_device(hass, entry, deleted)
    registry.async_remove_device(deleted.id)
    coordinator.cycle(list(ROWS))  # a new answer with the same components
    coordinator.cycle([*ROWS, ComponentRow(4, 56)])  # and one more
    found = _components(hass, entry)
    assert f"{GW}:component:16:253" not in found
    assert f"{GW}:component:56:4" in found  # a new component gets its device
    assert found[f"{GW}:component:56:4"].name == (
        "Remote control heating circuit 1 HMI800_3_FOSSIL"
    )
    # The next start (the entry is set up again).
    for unload in scope.unloads:
        unload()
    component_devices._managers(hass).clear()
    await _start(hass, entry, [*ROWS, ComponentRow(4, 56)])
    assert set(_components(hass, entry)) == {*EXPECTED, f"{GW}:component:56:4"}


async def test_a_component_no_longer_listed_keeps_its_device(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    _gateway(hass, entry)
    coordinator, _, _ = await _start(hass, entry)
    coordinator.cycle(ROWS[:1])
    coordinator.rows = None  # no list at all (an outage, a start from storage)
    coordinator.cycle()
    assert set(_components(hass, entry)) == EXPECTED


async def test_a_component_reported_later_gets_its_device_at_once(
    hass: HomeAssistant,
) -> None:
    """A component that appears in a later update gets its device in that
    update, also when the others already have theirs."""
    entry = _entry(hass)
    _gateway(hass, entry)
    coordinator, _, _ = await _start(hass, entry)
    assert set(_components(hass, entry)) == EXPECTED
    later = ComponentRow(4, 59, "2.0")
    coordinator.cycle([*ROWS, later])
    assert set(_components(hass, entry)) == {*EXPECTED, f"{GW}:component:59:4"}
    coordinator.cycle([*ROWS, later, ComponentRow(3, 56)])
    assert set(_components(hass, entry)) == {
        *EXPECTED,
        f"{GW}:component:59:4",
        f"{GW}:component:56:3",
    }


async def test_versions_follow_the_shown_list(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    _gateway(hass, entry)
    coordinator, _, _ = await _start(hass, entry)
    key = f"{GW}:component:16:253"
    coordinator.cycle([*ROWS[:1], ComponentRow(253, 16, "4.6"), *ROWS[2:]])
    assert _components(hass, entry)[key].sw_version == "4.6"
    coordinator.cycle([*ROWS[:1], ComponentRow(253, 16, "V-text"), *ROWS[2:]])
    assert _components(hass, entry)[key].sw_version == "4.6"  # unknown: kept
    coordinator.cycle([*ROWS, ComponentRow(253, 16, "4.7")])
    assert _components(hass, entry)[key].sw_version is None  # they differ


async def test_the_order_of_the_answer_never_matters(hass: HomeAssistant) -> None:
    results = []
    for order in (ROWS, tuple(reversed(ROWS)), ROWS[2:] + ROWS[:2]):
        entry = _entry(hass)
        _gateway(hass, entry)
        await _start(hass, entry, order)
        results.append(
            sorted(
                (key.removeprefix(GW), device.name, device.sw_version)
                for key, device in _components(hass, entry).items()
            )
        )
    assert results[0] == results[1] == results[2]


async def test_a_local_view_never_changes_the_registry(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    _gateway(hass, entry)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{GW}:component:16:253")}
    )
    before = _registry(hass)
    coordinator, manager, _ = await _start(hass, entry, read_only=True)
    coordinator.cycle(ROWS)
    assert manager.sync() == 0
    assert enable(hass, entry) == 0
    assert _registry(hass) == before


async def test_without_a_plant_model_there_is_nothing_to_follow(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    with patch(f"{CD}.existing_tracker", return_value=None):
        assert (
            track_component_devices(SimpleNamespace(), FakeCoordinator(hass, entry))
            is None
        )
    no_entry = SimpleNamespace(config_entry=None)
    assert track_component_devices(SimpleNamespace(), no_entry) is None
    # A failing update never stops the coordinator's update.
    _gateway(hass, entry)
    coordinator, manager, _ = await _start(hass, entry)
    with patch(f"{CD}.device_components", side_effect=RuntimeError) as failing:
        coordinator.cycle(ROWS[:2])
        coordinator.cycle(ROWS[:3])
    assert failing.call_count == 2
    assert manager.listed().keys() == device_components(ROWS[:3]).keys()


def test_only_a_device_with_one_component_identifier_counts(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    registry = dr.async_get(hass)
    both = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{GW}:component:16:253"), (DOMAIN, "other")},
    )
    alone = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{GW}:component:8:8"), ("other", "x")},
    )
    assert component_identifier(both) is None
    assert component_identifier(alone) == (GW, 8, 8)
    assert remove_all(hass, entry) == 1
    assert registry.async_get(both.id) is not None


async def test_a_gateway_device_created_again_gets_its_component_devices(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    first = _gateway(hass, entry)
    coordinator, _, _ = await _start(hass, entry)
    registry = dr.async_get(hass)
    registry.async_remove_device(first.id)
    # Home Assistant unlinks the devices below a removed device.
    unlinked = {device.via_device_id for device in _components(hass, entry).values()}
    assert unlinked == {None}
    second = _gateway(hass, entry)
    coordinator.cycle(tuple(reversed(ROWS)))
    found = _components(hass, entry)
    assert set(found) == EXPECTED
    assert {device.via_device_id for device in found.values()} == {second.id}


async def test_a_restarted_source_keeps_its_manager(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    _gateway(hass, entry)
    _, first, _ = await _start(hass, entry)
    _, again, _ = await _start(hass, entry)  # the same gateway, a new source
    assert again is first
    component_devices._managers(hass).clear()  # as if it ended elsewhere
    _, other, _ = await _start(hass, entry)
    assert other is not first
    await entry._async_process_on_unload(hass)
    assert component_devices._managers(hass) == {}


async def test_the_older_format_cleanups(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    gateway = _gateway(hass, entry)
    _gateway(hass, entry, "gateway-two")
    await _start(hass, entry)
    await _start(hass, entry, gateway="gateway-two")
    assert len(_components(hass, entry)) == 6
    coordinator = BoschBuderusDataUpdateCoordinator(
        hass, AsyncMock(), Gateway(GW, device_type="k40"), entry
    )
    # The merge of logical devices leaves component devices alone.
    _consolidate_logical_devices(hass, entry, (coordinator,))
    assert len(_components(hass, entry)) == 6
    assert all(
        device.via_device_id is not None for device in _components(hass, entry).values()
    )
    # A deselected gateway loses them with its other devices.
    _remove_unselected_gateway_entries(hass, entry, frozenset({GW}))
    assert set(_components(hass, entry)) == EXPECTED
    assert dr.async_get(hass).async_get(gateway.id) is not None


@pytest.mark.parametrize("case", ["clean", "unreadable"])
async def test_format_4_removes_them_for_a_gateway_without_a_system(
    hass: HomeAssistant, case: str
) -> None:
    entry = _entry(hass, version=4)
    for gateway in (GW, "gateway-two", "gateway-old"):
        _gateway(hass, entry, gateway)
        await _start(hass, entry, gateway=gateway)
    registry = dr.async_get(hass)
    customised = _components(hass, entry)["gateway-old:component:16:253"]
    registry.async_update_device(customised.id, name_by_user="Mine")
    load = (
        patch(f"{_BASE}.configuration.load_configuration", side_effect=ValueError)
        if case == "unreadable"
        else patch(
            f"{_BASE}.configuration.load_configuration",
            return_value=_snapshot(_PLANT, bindings=("gateway-two",)),
        )
    )
    with load:
        _remove_unconfigured_gateway_entries(hass, entry)
    gateways = {key.split(":", 1)[0] for key in _components(hass, entry)}
    if case == "clean":
        assert gateways == {GW, "gateway-two"}
    else:
        assert gateways == {GW, "gateway-two", "gateway-old"}
    assert remove_for_other_gateways(hass, entry, [GW]) == (3 if case == "clean" else 6)


# ------------------------------------------------------- dialog, reports


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_dialog_switches_on_and_off(hass: HomeAssistant) -> None:
    entry = _entry(hass, on=False, version=1)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert "components" not in menu["menu_options"]  # nothing listed, off
    hass.config_entries.options.async_abort(menu["flow_id"])
    _gateway(hass, entry)
    await _start(hass, entry)
    assert offered(hass, entry)
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["menu_options"][-1] == "components"
    form = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "components"}
    )
    assert form["type"] is FlowResultType.FORM and form["step_id"] == "components"
    listed = form["description_placeholders"]["components"]
    assert "- System controller HMI800_2_HP" in listed
    assert "Testgerät 7" in listed and GW not in listed
    assert _components(hass, entry) == {}
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], {OPTION: True}
    )
    assert await outcome(hass, result) == "component_devices_on"
    assert "below the gateway's device" in await menu_note(
        hass, result, "component_devices_on"
    )
    assert entry.options[OPTION] is True
    assert set(_components(hass, entry)) == EXPECTED
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "components"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPTION: False}
    )
    assert result["description_placeholders"] == {"count": "3"}
    assert "removed: 3" in await menu_note(hass, result, "component_devices_off")
    assert entry.options[OPTION] is False
    assert _components(hass, entry) == {}


async def test_the_dialog_lists_several_systems_or_none(hass: HomeAssistant) -> None:
    entry = _entry(hass, version=1)
    assert component_devices.listing(hass, entry) == "No components reported"
    assert offered(hass, entry)  # on, even without a list
    _gateway(hass, entry)
    await _start(hass, entry)
    await _start(hass, entry, [ComponentRow(4, 56)], gateway="gateway-two")
    listed = component_devices.listing(hass, entry)
    assert "**Heating**" in listed and "**gateway-two**" in listed
    assert [manager.gateway for manager in managers_of(hass, entry.entry_id)] == [
        GW,
        "gateway-two",
    ]


async def test_reports_count_and_describe_the_devices(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    gateway = _gateway(hass, entry)
    coordinator, _, _ = await _start(hass, entry)
    assert _with_component_devices({"plant_kind": None}, coordinator) == {
        "plant_kind": None,
        "component_devices": {"enabled": True, "count": 3},
    }
    assert _with_component_devices({"error": "unavailable"}, coordinator) == {
        "error": "unavailable"
    }
    assert _with_component_devices({}, SimpleNamespace()) == {}
    broken = SimpleNamespace(hass=hass, config_entry=entry)
    assert component_devices.diagnostics(broken) is None
    found = _components(hass, entry)
    report = await async_get_device_diagnostics(
        hass, entry, found[f"{GW}:component:16:253"]
    )
    assert report["component"] == {
        "place": "system_controller",
        "place_number": 16,
        "type_number": 253,
        "type_name": "HMI800_2_HP",
        "role": "system_controller",
        "basis": "observed",
        "version": "4.5-FIELD",
        "reported": True,
        "count": 1,
    }
    assert report["privacy"]["contains_raw_resource_values"] is True
    assert report["device"]["has_parent_device"] is True
    assert report["device"]["entity_count"] == 0
    plain = await async_get_device_diagnostics(hass, entry, gateway)
    assert "component" not in plain
    assert plain["privacy"]["contains_raw_resource_values"] is False
    # Not listed any more: the table still describes it.
    coordinator.rows = None
    gone = await async_get_device_diagnostics(
        hass, entry, found[f"{GW}:component:8:65281"]
    )
    assert gone["component"]["reported"] is False
    assert gone["component"]["type_name"] == "XCU_SRH"
    assert gone["component"]["version"] is None
    assert gone["privacy"]["contains_raw_resource_values"] is False
    assert GW not in json.dumps(gone)
