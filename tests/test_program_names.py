"""TP-05: the names of the weekly programs A/B (decoding, text control)."""

from __future__ import annotations

import base64
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_buderus_heating import text as text_platform
from custom_components.bosch_buderus_heating.diagnostics import (
    switch_program_write_diagnostics,
)
from custom_components.bosch_buderus_heating.pointt import (
    UnexpectedHttpStatus,
    WriteNotConfirmed,
)
from custom_components.bosch_buderus_heating.pointt.models import Gateway
from custom_components.bosch_buderus_heating.program_names import (
    config_issue,
    decoded_name,
    encode_name,
    value_config,
)
from custom_components.bosch_buderus_heating.switch_program_selection import (
    program_name,
)
from custom_components.bosch_buderus_heating.switch_program_service import (
    SwitchProgramService,
)
from custom_components.bosch_buderus_heating.write_policies import ContractRejected
from tests import test_dynamic_controls
from tests.program_kind_helpers import CountingGateway, load, resource

NAME = "/heatingCircuits/hc1/switchPrograms/nameA"
CASES = load("connectkey/switch-program-names.json")["cases"]


def case(name: str) -> dict:
    return copy.deepcopy(CASES[name])


@pytest.fixture
def environment(hass, enable_custom_integrations):
    yield from test_dynamic_controls.environment.__wrapped__(hass)


def gateway_for(environment, name: str) -> CountingGateway:
    coordinator, client, _ = environment
    coordinator._switch_program_service = SwitchProgramService(
        client, sleep=AsyncMock()
    )
    return CountingGateway(coordinator, client, {NAME: case(name)})


def test_fixture_is_synthetic():
    provenance = load("connectkey/switch-program-names.json")["provenance"]
    assert provenance["kind"] == "synthetic"
    assert provenance["physical_device_verified"] is False


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("utf8_base64", "Homeoffice"),
        ("utf16_base64", "Woche"),
        ("ascii_plain", "Office"),
        ("no_valueconfig", "Office"),
    ],
)
def test_names_are_shown_decoded(name, shown):
    assert decoded_name(resource(case(name))) == shown
    assert program_name(resource(case(name))) == shown


def test_invalid_base64_shows_no_name():
    item = case("utf8_base64")
    item["value"] = "not base64!"
    assert decoded_name(resource(item)) is None
    assert program_name(resource(item)) is None
    item = case("utf16_base64")
    item["value"] = base64.b64encode(b"\xd8\x00").decode()
    assert decoded_name(resource(item)) is None


def test_unknown_coding_shows_no_raw_text():
    assert decoded_name(resource(case("unknown_coding"))) is None


@pytest.mark.parametrize(
    ("name", "issue"),
    [
        ("utf8_base64", None),
        ("ascii_plain", None),
        ("no_valueconfig", "name_no_value_config"),
        ("unknown_coding", "name_coding_unknown"),
        ("maxlength_zero", "name_max_length_zero"),
    ],
)
def test_control_needs_a_complete_coding(name, issue):
    assert config_issue(resource(case(name))) == issue


@pytest.mark.parametrize(
    ("name", "created"),
    [
        ("utf8_base64", True),
        ("utf16_base64", True),
        ("ascii_plain", True),
        ("no_valueconfig", False),
        ("unknown_coding", False),
        ("maxlength_zero", False),
        ("writeable_0", False),
    ],
)
def test_text_control_only_with_a_writable_known_coding(name, created):
    descriptions = text_platform.descriptions({NAME: resource(case(name))})
    assert bool(descriptions) is created
    if created:
        (description,) = descriptions
        assert description.entity_registry_enabled_default is False
        assert description.native_max == value_config(resource(case(name))).max_length


def test_icom_gateway_gets_no_name_control(environment):
    coordinator, _client, _ = environment
    coordinator.gateway = Gateway("synthetic-gateway", device_type="iCom")
    added: list = []
    entry = SimpleNamespace(coordinators=(coordinator,))
    track = MagicMock()
    original = text_platform.track_control_entities
    text_platform.track_control_entities = track
    try:
        text_platform._setup_entities(entry, added.extend)
    finally:
        text_platform.track_control_entities = original
    track.assert_not_called()


def test_icom_name_token_in_diagnostics(environment):
    gateway = gateway_for(environment, "ascii_plain")
    gateway.coordinator.gateway = Gateway("synthetic-gateway", device_type="icom")
    names = switch_program_write_diagnostics(gateway.coordinator)["program_names"]
    assert names[NAME] == {"editable": False, "issue": "name_write_unproven_icom"}


def test_ascii_charset_refuses_umlauts():
    config = value_config(resource(case("ascii_plain")))
    with pytest.raises(ContractRejected, match="program_name_charset"):
        encode_name("Büro", config)


def test_length_counts_characters_not_bytes():
    config = value_config(resource(case("utf8_base64")))
    name = "Haus \U0001f3e0" + "x" * 14  # 20 characters, more bytes
    assert len(name) == 20
    assert base64.b64decode(encode_name(name, config)).decode() == name
    with pytest.raises(ContractRejected, match="program_name_too_long"):
        encode_name(name + "x", config)


def test_empty_name_is_not_written():
    config = value_config(resource(case("utf8_base64")))
    with pytest.raises(ContractRejected, match="program_name_empty"):
        encode_name("", config)


async def test_utf16_name_is_written_once_and_read_back(environment):
    gateway = gateway_for(environment, "utf16_base64")
    await gateway.coordinator.async_write_program_name(gateway.read(NAME), "Küche 2")
    assert gateway.value_puts == [
        (NAME, base64.b64encode("Küche 2".encode("utf-16-be")).decode())
    ]
    assert decoded_name(gateway.coordinator.data[NAME].resource) == "Küche 2"


async def test_ascii_name_is_written_as_is(environment):
    gateway = gateway_for(environment, "ascii_plain")
    await gateway.coordinator.async_write_program_name(gateway.read(NAME), "Home")
    assert gateway.value_puts == [(NAME, "Home")]


async def test_name_changed_in_the_app_sends_nothing(environment):
    gateway = gateway_for(environment, "ascii_plain")
    shown = gateway.read(NAME)
    gateway.change(NAME, value="Other")
    with pytest.raises(ContractRejected, match="program_name_changed"):
        await gateway.coordinator.async_write_program_name(shown, "Home")
    assert gateway.puts == 0


async def test_same_name_sends_nothing(environment):
    gateway = gateway_for(environment, "ascii_plain")
    await gateway.coordinator.async_write_program_name(gateway.read(NAME), "Office")
    assert gateway.puts == 0


async def test_unconfirmed_name_is_not_sent_again(environment):
    gateway = gateway_for(environment, "ascii_plain")
    gateway.ignore_writes = True
    with pytest.raises(WriteNotConfirmed):
        await gateway.coordinator.async_write_program_name(gateway.read(NAME), "Home")
    assert len(gateway.value_puts) == 1


@pytest.mark.parametrize("status", [400, 403, 404])
async def test_refused_name_is_not_sent_again(environment, status):
    gateway = gateway_for(environment, "ascii_plain")
    gateway.client.put_resource_value.side_effect = UnexpectedHttpStatus(status)
    with pytest.raises(UnexpectedHttpStatus):
        await gateway.coordinator.async_write_program_name(gateway.read(NAME), "Home")
    assert gateway.client.put_resource_value.await_count == 1


async def test_cloud_pause_sends_no_name(environment):
    from time import monotonic

    from custom_components.bosch_buderus_heating.pointt import RateLimited

    gateway = gateway_for(environment, "ascii_plain")
    gateway.coordinator._cloud_backoff_until = monotonic() + 600
    with pytest.raises(RateLimited):
        await gateway.coordinator.async_write_program_name(gateway.read(NAME), "Home")
    assert gateway.puts == 0


async def test_text_entity_reports_translated_refusals(environment):
    gateway = gateway_for(environment, "ascii_plain")
    (description,) = text_platform.descriptions({NAME: gateway.read(NAME)})
    entity = text_platform.ProgramNameText(gateway.coordinator, description)
    entity.hass = gateway.coordinator.hass
    assert entity.native_value == "Office"
    from homeassistant.exceptions import ServiceValidationError

    with pytest.raises(ServiceValidationError) as err:
        await entity.async_set_value("Büro")
    assert err.value.translation_key == "program_name_charset"
    assert err.value.translation_placeholders["charset"] == "ASCII"
    assert gateway.puts == 0
