"""Both sides of every Home Assistant compatibility bridge stay tested."""

import inspect
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.helpers import device_registry as dr

from custom_components.bosch_buderus_heating import ha_compat, local_transport
from custom_components.bosch_buderus_heating.const import DOMAIN
from custom_components.bosch_buderus_heating.ha_compat import (
    MAX_LOCAL_HEADERS,
    device_by_identifier,
    identifier_owned_elsewhere,
    local_header_limits,
)
from tests.ha_test_helpers import MockConfigEntry

IDENTIFIER = (DOMAIN, "gateway-one")


class _OlderRegistry:
    """Device registry surface of Home Assistant 2026.3."""

    def __init__(self, device):
        self.device = device
        self.lookups = []

    def async_get_device(self, identifiers=None, connections=None):
        self.lookups.append(identifiers)
        return self.device


def test_current_registry_lookup_is_scoped_to_the_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    other = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    other.add_to_hass(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={IDENTIFIER}
    )
    assert device_by_identifier(registry, IDENTIFIER, entry.entry_id).id == device.id
    if hasattr(registry, "async_get_device_by_identifier"):
        assert device_by_identifier(registry, IDENTIFIER, other.entry_id) is None
        assert not identifier_owned_elsewhere(registry, IDENTIFIER, other.entry_id)
    assert not identifier_owned_elsewhere(registry, IDENTIFIER, entry.entry_id)


@pytest.mark.parametrize(
    ("owners", "found", "blocked"),
    [
        (None, False, False),
        ({"entry"}, True, False),
        ({"entry", "other"}, True, False),
        ({"other"}, False, True),
    ],
)
def test_older_registry_checks_ownership_of_the_global_identifier(
    owners, found, blocked
):
    device = None if owners is None else SimpleNamespace(config_entries=owners)
    registry = _OlderRegistry(device)
    result = device_by_identifier(registry, IDENTIFIER, "entry")
    assert result is (device if found else None)
    assert identifier_owned_elsewhere(registry, IDENTIFIER, "entry") is blocked
    assert registry.lookups == [{IDENTIFIER}, {IDENTIFIER}]


@pytest.mark.parametrize("supported", [False, True])
def test_header_count_limit_is_only_passed_when_aiohttp_offers_it(supported):
    parameters = {"trust_env": None}
    if supported:
        parameters["max_headers"] = None
    signature = SimpleNamespace(parameters=parameters)
    local_header_limits.cache_clear()
    try:
        with patch.object(ha_compat.inspect, "signature", return_value=signature):
            assert local_header_limits() == (
                {"max_headers": MAX_LOCAL_HEADERS} if supported else {}
            )
    finally:
        local_header_limits.cache_clear()


async def test_local_session_opens_with_the_installed_aiohttp():
    session = local_transport.create_local_session()
    try:
        offered = "max_headers" in inspect.signature(aiohttp.ClientSession).parameters
        assert bool(local_header_limits()) is offered
    finally:
        await session.close()


@pytest.mark.parametrize("limits", [{}, {"max_headers": MAX_LOCAL_HEADERS}])
async def test_local_session_passes_only_offered_limits(limits):
    with (
        patch.object(local_transport, "local_header_limits", return_value=limits),
        patch.object(local_transport.aiohttp, "TCPConnector"),
        patch.object(local_transport.aiohttp, "ClientSession") as session_class,
    ):
        local_transport.create_local_session()
    kwargs = session_class.call_args.kwargs
    assert kwargs["max_line_size"] == kwargs["max_field_size"] == 4096
    assert kwargs.get("max_headers") == limits.get("max_headers")


# --------------------------------------------- native temperature members


class _BaseBefore2026_11:
    """Climate and water heater base before 2026.11: no native members."""


class _BaseFrom2026_11:
    native_temperature_unit = "°C"


def _thermostat(base):
    @ha_compat.temperature_members
    class Thermostat(base):
        _attr_native_temperature_unit = "°C"

        @property
        def native_current_temperature(self):
            return 21.5

        @property
        def native_target_temperature(self):
            return 22.0

    return Thermostat


def test_temperature_members_keep_the_old_names_before_2026_11() -> None:
    thermostat = _thermostat(_BaseBefore2026_11)
    entity = thermostat()
    assert (entity.current_temperature, entity.target_temperature) == (21.5, 22.0)
    assert thermostat._attr_temperature_unit == "°C"
    assert not hasattr(thermostat, "target_temperature_high")
    assert ha_compat.temperature_unit(SimpleNamespace(temperature_unit="°F")) == "°F"


def test_temperature_members_use_only_the_native_names_from_2026_11() -> None:
    thermostat = _thermostat(_BaseFrom2026_11)
    legacy = {"current_temperature", "target_temperature", "_attr_temperature_unit"}
    assert not legacy & set(vars(thermostat))
    assert ha_compat.temperature_unit(thermostat()) == "°C"


def test_the_real_entities_follow_the_installed_release() -> None:
    from custom_components.bosch_buderus_heating.climate import BaconClimate
    from custom_components.bosch_buderus_heating.heating_climate import (
        HeatingCircuitClimate,
    )
    from custom_components.bosch_buderus_heating.pointt_water_heater import (
        PointTWaterHeater,
    )
    from custom_components.bosch_buderus_heating.water_heater import (
        BaconWaterHeater,
    )

    legacy = {"current_temperature", "target_temperature", "_attr_temperature_unit"}
    for cls in (
        BaconClimate,
        HeatingCircuitClimate,
        PointTWaterHeater,
        BaconWaterHeater,
    ):
        native = any("native_temperature_unit" in vars(base) for base in cls.__mro__)
        assert (legacy & set(vars(cls))) == (set() if native else legacy), cls
