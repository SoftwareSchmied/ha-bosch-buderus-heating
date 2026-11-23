"""Optional component devices through real setups: the K40 of the golden
master in format 4 and as a local view, an entry of the older format, and a
gateway exchange.

The K40 runs follow the procedure of ``tests/test_x40_golden.py`` with the
option switched on: entities, states, writes and requests stay exactly as
recorded; only the three component devices are new.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from awesomeversion import AwesomeVersion
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.component_devices import (
    OPTION,
    component_identifier,
)
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.ha_compat import device_by_identifier
from custom_components.bosch_buderus_heating.pointt import AuthTokens, Gateway
from tests import test_managed_local_plant
from tests import test_x40_golden as golden
from tests.ha_test_helpers import MockConfigEntry
from tests.helpers.options_menu import menu_note
from tests.helpers.rrc_cloud import Clock, RrcCloud, cloud_patches
from tests.test_gateway_exchange import (
    NEW,
    OLD,
    ExchangeBackend,
    _detected,
    _flow,
    _rows,
    uses_network,
)
from tests.test_managed_pointt import eventually, running

GW = "gateway-one"
GOLDEN_RELEASE = "2026.11.0"
REGISTRY_LINES = (
    "entity gone",
    "entity changed",
    "new entity",
    "device gone",
    "device changed",
    "new device",
)
PACKAGE = "custom_components.bosch_buderus_heating"
THREE = ("8:8", "8:65281", "16:253")


def _components(hass: HomeAssistant, entry_id: str) -> dict[str, dr.DeviceEntry]:
    return {
        "{}:component:{}:{}".format(*parsed): device
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry_id)
        if (parsed := component_identifier(device)) is not None
    }


def _no_entity_on(hass: HomeAssistant, devices: dict[str, dr.DeviceEntry]) -> None:
    registry = er.async_get(hass)
    for device in devices.values():
        assert not er.async_entries_for_device(
            registry, device.id, include_disabled_entities=True
        )


@pytest.mark.parametrize("name", ["k40_cloud", "k40_own_cloud", "k40_cloud_local"])
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_k40_gains_only_its_three_component_devices(
    hass: HomeAssistant, freezer: Any, name: str
) -> None:
    freezer.move_to(golden.FROZEN)
    original = golden.managed_entry

    def with_option(hass: HomeAssistant, gateway: Gateway) -> MockConfigEntry:
        entry = original(hass, gateway)
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, OPTION: True}
        )
        return entry

    pattern = rf"{DOMAIN}:{GW}:component:(?:8:8|8:65281|16:253)"
    with (
        patch.object(golden, "managed_entry", with_option),
        patch.dict(golden.EXPECTED_NEW, {name: {"devices": (pattern,)}}),
    ):
        actual = await golden.build_snapshot(hass, golden.PLANTS[name])
        expected = json.loads(
            (golden.GOLDEN / f"{name}.json").read_text(encoding="utf-8")
        )
        found = golden.differences(name, expected, actual)
    # Entities and devices: exactly as recorded, plus the three devices.
    assert not [line for line in found if line.startswith(REGISTRY_LINES)]
    if AwesomeVersion(HA_VERSION) >= GOLDEN_RELEASE:
        # States, writes and requests too (recorded with this release; older
        # releases differ in some state attributes whatever the option).
        assert found == []
    new = [row for row in actual["devices"] if row not in expected["devices"]]
    assert sorted(row["identifiers"] for row in new) == sorted(
        [[DOMAIN, f"{GW}:component:{key}"]] for key in THREE
    )
    for row in new:
        assert row["via_device"] == [[DOMAIN, GW]]
        assert row["manufacturer"] == "Buderus"
    # No entity names a component device.
    assert not [
        row for row in actual["entities"] if ":component:" in json.dumps(row["device"])
    ]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_local_view_of_the_k40_stays_as_recorded(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A view from local readings alone never changes the registry, also
    with the option on."""
    freezer.move_to(golden.FROZEN)
    original = test_managed_local_plant.plant

    async def with_option(hass: HomeAssistant, runtime: Any = None, **kwargs: Any):
        coordinator, runtime = await original(hass, runtime, **kwargs)
        entry = coordinator.config_entry
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, OPTION: True}
        )
        return coordinator, runtime

    with patch.object(test_managed_local_plant, "plant", with_option):
        actual = await golden.build_snapshot(hass, golden.PLANTS["k40_local_view"])
    expected = json.loads(
        (golden.GOLDEN / "k40_local_view.json").read_text(encoding="utf-8")
    )
    found = golden.differences("k40_local_view", expected, actual)
    assert not [line for line in found if line.startswith(REGISTRY_LINES)]
    if AwesomeVersion(HA_VERSION) >= GOLDEN_RELEASE:
        assert found == []


def _legacy_entry(hass: HomeAssistant, options: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            "brand": "buderus",
            "gateway_ids": [GW],
            **tokens_to_data(AuthTokens("synthetic-access", "synthetic-refresh", 4e9)),
        },
        options={"polling_profile": "standard", **options},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_an_entry_of_the_older_format(hass: HomeAssistant) -> None:
    clock = Clock()
    cloud = RrcCloud(golden.k40_tree(), forbidden=set(), missing_as_500=False)
    entry = _legacy_entry(hass, {OPTION: True})
    with (
        patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)),
        cloud_patches(cloud, clock, Gateway(GW, device_type="k40")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.version == 3
        (coordinator,) = entry.runtime_data.coordinators
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        found = _components(hass, entry.entry_id)
        assert set(found) == {f"{GW}:component:{key}" for key in THREE}
        gateway = device_by_identifier(dr.async_get(hass), (DOMAIN, GW), entry.entry_id)
        assert gateway is not None
        assert {device.via_device_id for device in found.values()} == {gateway.id}
        _no_entity_on(hass, found)
        entities = sorted(
            (item.unique_id, item.device_id)
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        )
        # A start again: the same devices, nothing added, no entity moved.
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        (coordinator,) = entry.runtime_data.coordinators
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        again = _components(hass, entry.entry_id)
        assert {key: device.id for key, device in again.items()} == {
            key: device.id for key, device in found.items()
        }
        assert (
            sorted(
                (item.unique_id, item.device_id)
                for item in er.async_entries_for_config_entry(
                    er.async_get(hass), entry.entry_id
                )
            )
            == entities
        )
        # Off in the dialog: removed at once, without a reload.
        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            menu = await hass.config_entries.options.async_init(entry.entry_id)
            assert "components" in menu["menu_options"]
            form = await hass.config_entries.options.async_configure(
                menu["flow_id"], {"next_step_id": "components"}
            )
            assert form["type"] is FlowResultType.FORM
            assert (
                "System controller HMI800_2_HP"
                in (form["description_placeholders"]["components"])
            )
            result = await hass.config_entries.options.async_configure(
                form["flow_id"], {OPTION: False}
            )
            await menu_note(hass, result, "component_devices_off")
            reload.assert_not_called()
        assert _components(hass, entry.entry_id) == {}
        assert dr.async_get(hass).async_get(gateway.id) is not None
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_older_format_stays_without_devices_by_default(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    cloud = RrcCloud(golden.k40_tree(), forbidden=set(), missing_as_500=False)
    entry = _legacy_entry(hass, {})
    with (
        patch(f"{PACKAGE}.async_migrate_entry", AsyncMock(return_value=True)),
        cloud_patches(cloud, clock, Gateway(GW, device_type="k40")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        (coordinator,) = entry.runtime_data.coordinators
        clock.now += 3600
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert _components(hass, entry.entry_id) == {}
        assert (
            len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id))
            == 1
        )
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


class ComponentBackend(ExchangeBackend):
    """The new gateway also lists its components below ``/system``."""

    def resource(self, gateway: str, path: str) -> Any:
        if path == "/system" and gateway in self.modules:
            return {
                "id": "/system",
                "type": "refEnum",
                "references": [{"id": "/system/info"}],
            }
        return super().resource(gateway, path)


@uses_network
async def test_an_exchanged_gateway_gets_new_devices_and_leaves_no_orphans(
    hass: HomeAssistant,
) -> None:
    backend = ComponentBackend()
    backend.modules[NEW] = _rows("serial-")

    def switched_on(data: dict[str, Any], options: dict[str, Any]) -> None:
        options[OPTION] = True

    async with running(hass, backend, configure=switched_on) as (
        entry,
        cloud,
        owner,
        _,
    ):
        registry = dr.async_get(hass)
        gateway = registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, OLD)}
        )
        for key in ("8:8", "16:253", "56:4"):
            device = registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, f"{OLD}:component:{key}")},
            )
            registry.async_update_device(
                device.id, via_device_id=gateway.id, name_by_user="Mine"
            )
        binding = await _detected(hass, entry, cloud, owner)
        flow = await _flow(hass, entry, cloud, binding)
        await flow.async_step_init()
        await flow.async_step_continue()
        result = await flow.async_step_continue_unverified(
            {"confirm": True, "confirm_unverified": True}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        # The gateway's device continues under the new gateway.
        assert registry.async_get(gateway.id).identifiers == {(DOMAIN, NEW)}
        await eventually(
            lambda: (
                set(_components(hass, entry.entry_id))
                == {f"{NEW}:component:{key}" for key in THREE}
            ),
            timeout=10,
        )
        found = _components(hass, entry.entry_id)
        # Nothing moved: the old devices are gone, the new ones are new.
        assert all(device.name_by_user is None for device in found.values())
        assert {device.via_device_id for device in found.values()} == {gateway.id}
        _no_entity_on(hass, found)
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_component_devices_show_no_privacy_marker(
    hass: HomeAssistant,
    freezer: Any,
    caplog: pytest.LogCaptureFixture,
    hass_storage: dict[str, Any],
) -> None:
    """I7 with the option on: the marked module lists (``PRIVACYMARK`` in
    every identifying field) reach no device, device diagnostics, log or
    stored file through the component devices."""
    import logging
    from contextlib import ExitStack

    from tests.helpers.managed_runtime import wait_for_sources
    from tests.helpers.privacy_markers import (
        assert_no_markers,
        device_rows,
        diagnostics_channels,
        storage_channel,
        system_info,
    )
    from tests.test_x40_privacy_markers import _final_write, _local_marker_reading

    freezer.move_to(golden.FROZEN)
    caplog.set_level(logging.DEBUG, logger=PACKAGE)
    tree = golden.k40_tree()
    tree["/system/info"] = system_info()
    cloud = RrcCloud(tree, forbidden=set(), missing_as_500=False)
    clock = Clock()
    gateway = Gateway(GW, device_type="k40")
    entry = golden.managed_entry(hass, gateway)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, OPTION: True}
    )
    with ExitStack() as stack:
        stack.enter_context(cloud_patches(cloud, clock, gateway))
        stack.enter_context(golden.local_readings((_local_marker_reading(),)))
        assert await hass.config_entries.async_setup(entry.entry_id)
        await wait_for_sources(hass, entry, 1)
        await hass.async_block_till_done()
        clock.now += 3600
        await golden.source_of(entry).async_refresh()
        await hass.async_block_till_done()
        found = _components(hass, entry.entry_id)
        assert len(found) >= 3
        channels = {
            "devices": json.dumps(device_rows(hass, entry.entry_id)),
            **await diagnostics_channels(hass, entry),
        }
        assert any(
            '"component": {' in text
            for key, text in channels.items()
            if key.startswith("device_diagnostics")
        )
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    await _final_write(hass)
    channels["storage"] = storage_channel(hass_storage)
    channels["log"] = caplog.text
    assert_no_markers(channels)
