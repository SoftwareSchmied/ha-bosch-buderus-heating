"""Path semantics and HA registry continuity across system-language changes."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.bosch_buderus_heating.const import (
    CONF_BRAND,
    CONF_GATEWAY_IDS,
    DOMAIN,
)
from custom_components.bosch_buderus_heating.data import tokens_to_data
from custom_components.bosch_buderus_heating.pointt import (
    AuthTokens,
    Gateway,
    RequestMetrics,
)
from custom_components.bosch_buderus_heating.pointt.parsers import parse_resource
from custom_components.bosch_buderus_heating.pointt.transport import RateLimitBackoff
from custom_components.bosch_buderus_heating.resource_catalog import resource_name
from tests.ha_test_helpers import MockConfigEntry
from tests.helpers.managed_runtime import pointt_client, wait_for_sources


@pytest.mark.parametrize("language", ["de", "DE-de", "en"])
def test_cooling_level_is_a_temperature_not_an_on_off_label(language):
    """An identical leaf must keep its different circuit-specific meaning."""
    german = language.lower().startswith("de")
    for circuit in ("hc1", "hc12"):
        assert resource_name(
            f"/heatingCircuits/{circuit}/cooling/temperatureLevels/on",
            language=language,
        ) == (
            "Kühlen: Temperaturstufe" if german else "Cooling: target temperature level"
        )
        assert resource_name(
            f"/heatingCircuits/{circuit}/temperatureLevels/eco", language=language
        ) == ("Absenktemperatur" if german else "Setback temperature")
    assert resource_name(
        "/dhwCircuits/dhw2/temperatureLevels/eco", language=language
    ) == ("Startschwelle Eco+" if german else "Start threshold Eco+")
    assert resource_name("/unknown/on", language=language) == "On"


def test_path_names_do_not_spread_to_other_subtrees_and_keep_subkeys():
    assert resource_name("/system/energyTariff/gas") == "Gaspreis"
    assert resource_name("/system/other/gas") == "Gas"
    assert resource_name("/system/awayMode/temperature") == "Abwesenheitstemperatur"
    assert resource_name("/system/other/temperature") == "Temperature"
    assert resource_name("/dhwCircuits/dhw7/monitorValues", "total") == (
        "Warmwasser-Messwerte Gesamt"
    )
    assert resource_name("/dhwCircuits/dhw7/extra/monitorValues") == "Monitor Values"
    assert resource_name("/other/dhwCircuits/dhw7/monitorValues") == "Monitor Values"
    assert (
        resource_name("/solarCircuits/sc7/pumpModulation")
        == "Modulation der Solarpumpe"
    )


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("first_language", ["de", "en"])
async def test_system_language_reload_preserves_names_ids_and_disabled_entities(
    hass, first_language
):
    """Run the actual HA platforms; translating a label must not recreate it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_BRAND: "buderus",
            CONF_GATEWAY_IDS: ["synthetic-gateway"],
            **tokens_to_data(AuthTokens("access", "refresh", expires_at=4e9)),
        },
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    paths = [f"/heatingCircuits/hc{i}/temporaryRoomSetpoint" for i in range(1, 5)]

    def unique(path):
        return "synthetic-gateway:" + path.strip("/").replace("/", ":")

    custom = registry.async_get_or_create(
        "sensor", DOMAIN, unique(paths[0]), config_entry=entry
    )
    registry.async_update_entity(custom.entity_id, name="Mein Wohnzimmer")
    disabled = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        unique(paths[2]),
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    resources = {
        path: parse_resource(
            {"id": path, "type": "floatValue", "value": 21, "unitOfMeasure": "C"}
        )
        for path in paths[:3]
    }
    counter = parse_resource(
        {
            "id": "/heatSources/numberOfStarts",
            "type": "floatValue",
            "value": 42,
        }
    )
    resources[counter.path] = counter
    client = Mock()
    client.metrics = RequestMetrics()
    client.backoff = RateLimitBackoff()
    client.get_gateways = AsyncMock(return_value=(Gateway("synthetic-gateway"),))
    client.get_resources_bulk = AsyncMock(return_value=())
    client.get_resource = AsyncMock()

    async def discover(*args, startup_data, **kwargs):
        startup_data.begin_discovery()
        return dict(resources)

    second_language = "en" if first_language == "de" else "de"
    with (
        pointt_client(client),
        patch(
            "custom_components.bosch_buderus_heating.coordinator.async_discover_resources",
            side_effect=discover,
        ),
        patch(
            "custom_components.bosch_buderus_heating.coordinator.fault_resource_candidates",
            return_value=(),
        ),
        patch(
            "custom_components.bosch_buderus_heating.async_setup_notifications",
            AsyncMock(return_value=()),
        ),
    ):
        identities = None
        for language in (first_language, second_language):
            hass.config.language = language
            assert await hass.config_entries.async_setup(entry.entry_id)
            await wait_for_sources(hass, entry, 1)
            await hass.async_block_till_done()
            unnamed_id = registry.async_get_entity_id(
                "sensor", DOMAIN, unique(paths[1])
            )
            label = registry.async_get(unnamed_id).original_name
            assert resource_name(paths[1], language=language) in label
            assert label.startswith(
                "Heizkreis 2" if language == "de" else "Heating circuit 2"
            )
            assert registry.async_get(custom.entity_id).name == "Mein Wohnzimmer"
            assert (
                "Mein Wohnzimmer"
                in hass.states.get(custom.entity_id).attributes["friendly_name"]
            )
            assert (
                registry.async_get(disabled.entity_id).disabled_by
                is er.RegistryEntryDisabler.USER
            )
            assert hass.states.get(disabled.entity_id) is None
            counter_id = registry.async_get_entity_id(
                "sensor", DOMAIN, unique(counter.path)
            )
            assert (
                hass.states.get(counter_id).attributes["state_class"]
                == "total_increasing"
            )
            current = {
                item.unique_id: (item.entity_id, item.id)
                for item in er.async_entries_for_config_entry(registry, entry.entry_id)
            }
            if identities is None:
                identities = current
                # A system-language change alone does not replace cached names.
                hass.config.language = second_language
                await hass.async_block_till_done()
                assert registry.async_get(unnamed_id).original_name == label
                resources[paths[3]] = parse_resource(
                    {
                        "id": paths[3],
                        "type": "floatValue",
                        "value": 20,
                        "unitOfMeasure": "C",
                    }
                )
            else:
                assert all(
                    current[key] == identity for key, identity in identities.items()
                )
                new_id = registry.async_get_entity_id(
                    "sensor", DOMAIN, unique(paths[3])
                )
                assert (
                    resource_name(paths[3], language=language)
                    in registry.async_get(new_id).original_name
                )
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
    client.get_resource.assert_not_awaited()
