"""Program names: coding edge cases and the write service's refusals."""

from __future__ import annotations

import base64
import copy
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_buderus_heating.pointt.exceptions import ProtocolError
from custom_components.bosch_buderus_heating.program_names import (
    ProgramNameService,
    config_issue,
    decoded_name,
    value_config,
)
from custom_components.bosch_buderus_heating.write_policies import ContractRejected
from tests.program_kind_helpers import load, resource

NAME = "/heatingCircuits/hc1/switchPrograms/nameA"
CASES = load("connectkey/switch-program-names.json")["cases"]


def case(name: str, **changes) -> dict:
    item = copy.deepcopy(CASES[name])
    item.update(changes)
    return item


def test_no_resource_has_no_coding_and_no_control():
    assert value_config(None) is None
    assert config_issue(None) == "not_a_name"


def test_other_path_is_not_a_name():
    item = case("ascii_plain", id="/heatingCircuits/hc1/switchPrograms/cp")
    assert config_issue(resource(item)) == "not_a_name"


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "floatValue", "value": 1.0},
        {"value": 5},
    ],
)
def test_name_of_unexpected_shape_has_no_control(changes):
    item = case("ascii_plain", **changes)
    assert config_issue(resource(item)) == "name_shape_unexpected"
    assert decoded_name(resource(item)) is None


def test_non_text_charset_is_an_unknown_coding():
    item = case(
        "utf8_base64",
        valueConfig={"charset": 8, "codingType": "base64", "maxLength": 20},
    )
    assert value_config(resource(item)) is None
    assert config_issue(resource(item)) == "name_coding_unknown"
    # An unknown coding never shows raw text that could be Base64.
    assert decoded_name(resource(item)) is None


def test_complete_coding_with_undecodable_value_has_no_control():
    item = case("utf8_base64", value="not base64!")
    assert value_config(resource(item)) is not None
    assert config_issue(resource(item)) == "name_not_decodable"


class FakeClient:
    """A gateway holding one name; counts reads and writes."""

    def __init__(self, item: dict, *, apply: bool = True, put_error=None):
        self.item = item
        self.apply = apply
        self.put_error = put_error
        self.puts: list[str] = []
        self.gets = 0

    async def get_resource(self, gateway_id, path, **_kwargs):
        self.gets += 1
        return resource(self.item)

    async def put_resource_value(self, gateway_id, path, value):
        self.puts.append(value)
        if self.apply:
            self.item["value"] = value
        if self.put_error is not None:
            raise self.put_error


def service(client) -> ProgramNameService:
    return ProgramNameService(client, sleep=AsyncMock(), delays=(0.0,))


@pytest.mark.parametrize(
    "changes",
    [
        {"writeable": 0},
        {"used": False},
        {"available": False},
    ],
)
async def test_unwritable_or_unused_name_is_refused_without_any_request(changes):
    item = case("ascii_plain", **changes)
    client = FakeClient(item)
    with pytest.raises(ContractRejected, match="program_name_unsupported"):
        await service(client).write("gw", resource(item), "Home")
    assert client.gets == 0
    assert client.puts == []


async def test_name_becoming_unused_meanwhile_sends_nothing():
    item = case("ascii_plain")
    client = FakeClient(item)
    shown = resource(item)
    item["used"] = False
    with pytest.raises(ContractRejected, match="program_name_changed"):
        await service(client).write("gw", shown, "Home")
    assert client.puts == []


async def test_accepted_write_with_unreadable_answer_is_decided_by_reading():
    item = case("ascii_plain")
    client = FakeClient(item, put_error=ProtocolError("garbled answer"))
    result = await service(client).write("gw", resource(item), "Home")
    assert client.puts == ["Home"]
    assert decoded_name(result[0]) == "Home"


async def test_unreadable_answer_and_unchanged_name_is_not_confirmed():
    from custom_components.bosch_buderus_heating.write_policies import (
        RrcWriteNotConfirmed,
    )

    item = case("ascii_plain")
    client = FakeClient(item, apply=False, put_error=ProtocolError("garbled"))
    with pytest.raises(RrcWriteNotConfirmed):
        await service(client).write("gw", resource(item), "Home")
    assert len(client.puts) == 1


def test_non_printable_name_is_refused_before_encoding():
    from custom_components.bosch_buderus_heating.program_names import encode_name

    config = value_config(resource(case("utf8_base64")))
    with pytest.raises(ContractRejected, match="program_name_charset"):
        encode_name("a\nb", config)
    assert base64.b64decode(encode_name("ab", config)) == b"ab"
