"""Local fault notification settings and compatibility with existing options."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_buderus_heating.const import (
    CONF_FAULT_NOTIFICATIONS,
    CONF_GATEWAY_IDS,
    CONF_POLLING_PROFILE,
    DOMAIN,
)
from custom_components.bosch_buderus_heating.notifications import (
    gateway_notification_key,
    notification_policy,
)


async def _start(hass, entry):
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    assert menu["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "notifications"}
    )


@pytest.mark.parametrize("enabled", [True, False])
async def test_offline_options_preserve_existing_settings(
    hass, enable_custom_integrations, enabled
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_GATEWAY_IDS: ["one"]},
        options={CONF_POLLING_PROFILE: "cloud_friendly", "future_setting": "keep"},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.bosch_buderus_heating.config_flow.PointTClient.get_gateways",
            AsyncMock(
                side_effect=AssertionError("Local settings must not query PointT")
            ),
        ),
        patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload,
    ):
        result = await _start(hass, entry)
        assert result["type"] is FlowResultType.FORM
        assert result["data_schema"]({}) == {CONF_FAULT_NOTIFICATIONS: True}
        # An independent setting changed while the form was open.
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "future_setting": "new"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_FAULT_NOTIFICATIONS: enabled}
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_POLLING_PROFILE] == "cloud_friendly"
        assert entry.options["future_setting"] == "new"
        assert notification_policy(entry.options, "one") == (enabled, 0)
        reload.assert_not_awaited()


async def test_multiple_installations_are_independent_and_user_names_are_used(
    hass, enable_custom_integrations
):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_GATEWAY_IDS: ["one", "two"]})
    entry.add_to_hass(hass)
    for gateway in ("one", "two"):
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, gateway)},
            name="Heat pump",
        )
        dr.async_get(hass).async_update_device(device.id, name_by_user="My heating")
    result = await _start(hass, entry)
    selector = next(iter(result["data_schema"].schema.values()))
    assert [item["label"] for item in selector.config["options"]] == [
        "My heating (1)",
        "My heating (2)",
    ]
    assert selector.config["multiple"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FAULT_NOTIFICATIONS: [gateway_notification_key("two")]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert notification_policy(entry.options, "one") == (False, 0)
    assert notification_policy(entry.options, "two") == (True, 0)
    result = await _start(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_FAULT_NOTIFICATIONS: [
                gateway_notification_key("one"),
                gateway_notification_key("two"),
            ]
        },
    )
    assert notification_policy(entry.options, "one") == (True, 1)
    assert notification_policy(entry.options, "two") == (True, 0)


async def test_no_configured_installation_is_explained(
    hass, enable_custom_integrations
):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_GATEWAY_IDS: []})
    entry.add_to_hass(hass)
    result = await _start(hass, entry)
    assert result["reason"] == "no_notification_gateways"


async def test_changed_gateway_selection_does_not_apply_to_another_installation(
    hass, enable_custom_integrations
):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_GATEWAY_IDS: ["one"]})
    entry.add_to_hass(hass)
    result = await _start(hass, entry)
    hass.config_entries.async_update_entry(entry, data={CONF_GATEWAY_IDS: ["two"]})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FAULT_NOTIFICATIONS: False}
    )
    assert result["reason"] == "notification_gateways_changed"
    assert not entry.options


async def test_profile_language_uses_translated_menu_independent_of_system_language(
    hass, enable_custom_integrations
):
    from homeassistant.helpers.translation import async_get_translations

    hass.config.language = "en"
    german = await async_get_translations(hass, "de", "options", {DOMAIN})
    english = await async_get_translations(hass, "en", "options", {DOMAIN})
    prefix = f"component.{DOMAIN}.options.step.init.menu_options"
    assert german[f"{prefix}.notifications"] == "Benachrichtigungen"
    assert english[f"{prefix}.notifications"] == "Notifications"
    assert german[f"{prefix}.holidays"] == "Urlaubszeiten"


@pytest.mark.parametrize(
    "value,expected",
    [
        ({}, (True, 0)),
        ({"enabled": False, "reset": 2}, (False, 2)),
        ({"enabled": "false", "reset": True}, (True, 0)),
        ({"enabled": True, "reset": -3}, (True, 0)),
        ({"enabled": 0, "reset": "bad"}, (True, 0)),
    ],
)
def test_malformed_saved_notification_settings_are_safe(value, expected):
    assert (
        notification_policy(
            {CONF_FAULT_NOTIFICATIONS: {gateway_notification_key("one"): value}}, "one"
        )
        == expected
    )
