"""Strictness package A2: the local access reads a value its pinned schema
does not list yet, and local units in the spellings the cloud reads too.

Only readable resources pass an unlisted value; a setting keeps the strict
list. The local access never writes. Tested with constructed answers; not yet
confirmed on a real gateway.
"""

from __future__ import annotations

import json

import pytest

from custom_components.bosch_buderus_heating.local_api.catalog import (
    request_for_path,
)
from custom_components.bosch_buderus_heating.local_api.errors import (
    LocalResourceError,
    Problem,
)
from custom_components.bosch_buderus_heating.local_api.models import DataIssue
from custom_components.bosch_buderus_heating.local_read import ReadState
from custom_components.bosch_buderus_heating.observations import ValueStatus
from tests.test_local_api import read, scalar
from tests.test_local_read import client
from tests.test_local_read import clock as clock
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import gateways as gateways
from tests.test_local_transport import wire_reply
from tests.test_observations import local

# The reader test opens a loopback gateway.
pytestmark = pytest.mark.usefixtures("socket_enabled")

SYSTEM_TYPE = "/system/type"
STATUS = "/heatingCircuits/hc1/overallStatus"


def _text(path: str, value: object, *, writeable: int = 0) -> dict[str, object]:
    return {"id": path, "type": "stringValue", "writeable": writeable, "value": value}


def test_a_listed_value_reads_without_a_mark() -> None:
    data = read(_text(SYSTEM_TYPE, "heatpump_single")).data
    assert data.value == "heatpump_single"
    assert DataIssue.UNDECLARED_VALUE not in data.issues


@pytest.mark.parametrize("path", [SYSTEM_TYPE, STATUS])
def test_an_unlisted_value_of_a_readable_resource_is_read_and_marked(
    path: str,
) -> None:
    observation = read(_text(path, "newFirmwareValue"))
    assert observation.data.value == "newFirmwareValue"
    assert observation.data.issues == (DataIssue.UNDECLARED_VALUE,)
    # Shown like any other reading: an enum entity without the option shows
    # unknown and counts it, as with the cloud.
    reading = local(_text(path, "newFirmwareValue"))
    assert reading.status is ValueStatus.VALID
    assert reading.value == "newFirmwareValue"


@pytest.mark.parametrize(
    "raw",
    [
        _text(SYSTEM_TYPE, 7),
        {"id": SYSTEM_TYPE, "type": "stringValue", "writeable": 0},
        {**_text(SYSTEM_TYPE, "newFirmwareValue"), "writeable": 2},
    ],
)
def test_only_the_enum_of_the_value_is_open(raw: dict[str, object]) -> None:
    # Another type, a missing value or a faulty flag still break the form.
    with pytest.raises(LocalResourceError) as error:
        read(raw)
    assert error.value.problem is Problem.INVALID_SCHEMA


def test_a_setting_keeps_its_strict_list() -> None:
    path = "/system/variableTariff/ch/optimization"
    assert read(_text(path, "on", writeable=1)).data.value == "on"
    with pytest.raises(LocalResourceError) as error:
        read(_text(path, "auto", writeable=1))
    assert error.value.problem is Problem.INVALID_SCHEMA


# --------------------------------------------------------------------- units


@pytest.mark.parametrize("unit", ["degC", "ºC", "°C", "C"])
def test_temperature_spellings_of_the_cloud_are_known(unit: str) -> None:
    data = read(scalar(unitOfMeasure=unit)).data
    assert DataIssue.UNKNOWN_UNIT not in data.issues
    assert local(scalar(unitOfMeasure=unit)).unit == unit


@pytest.mark.parametrize("unit", ["KWh", "kWh", "Wh", "wh"])
def test_energy_spellings_of_the_cloud_are_known(unit: str) -> None:
    raw = {
        "id": "/heatSources/emon/totalConsumption",
        "type": "emonValue",
        "writeable": 0,
        "unit": unit,
        "values": [{"total": 100}],
    }
    assert DataIssue.UNKNOWN_UNIT not in read(raw).data.issues


@pytest.mark.parametrize("unit", ["parsec", "MWh", "kwH"])
def test_other_units_stay_unknown(unit: str) -> None:
    raw = {
        "id": "/heatSources/emon/totalConsumption",
        "type": "emonValue",
        "writeable": 0,
        "unit": unit,
        "values": [{"total": 100}],
    }
    assert read(raw).data.issues == (DataIssue.UNKNOWN_UNIT,)


# ------------------------------------------------------- counted by the reader


async def test_the_reader_counts_unlisted_values(gateways, clock) -> None:
    replies = iter(("newFirmwareValue", "heatpump_single", "newFirmwareValue"))

    async def reply(raw, writer):
        writer.write(wire_reply(json.dumps(_text(SYSTEM_TYPE, next(replies))).encode()))
        await writer.drain()

    _, _, _, binding = await gateways(reply)
    transport_client, budget = client(binding, clock)
    request = request_for_path(SYSTEM_TYPE)
    async with transport_client:
        assert "undeclared_values" not in transport_client.health()
        for _ in range(3):
            result = await transport_client.read(request)
            assert result.state is ReadState.SUCCESS
            clock.advance(budget.remaining_seconds or 1)
        assert transport_client.health()["undeclared_values"] == 2
        assert transport_client.health()["failures_by_class"] == {}
