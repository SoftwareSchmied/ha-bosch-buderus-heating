"""The read client against a simulated gateway (real HTTP, real encryption)."""

import asyncio
import time

import aiohttp
import pytest

from custom_components.bosch_buderus_heating import icom_client
from custom_components.bosch_buderus_heating.icom_client import (
    PATH_PROBLEMS,
    IcomClient,
    IcomError,
    IcomProblem,
)
from custom_components.bosch_buderus_heating.icom_crypto import derive_key
from tests.helpers.icom_gateway import SimulatedIcomGateway

GATEWAY_PASSWORD = "abcd-efgh-ijkl-mnop"
PRIVATE_PASSWORD = "private-secret"
pytestmark = pytest.mark.usefixtures("socket_enabled")


@pytest.fixture
async def gateway():
    simulated = SimulatedIcomGateway(
        gateway_password=GATEWAY_PASSWORD, private_password=PRIVATE_PASSWORD
    )
    await simulated.start()
    yield simulated
    await simulated.stop()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as value:
        yield value


def client_for(gateway, session, *, key=None, **kwargs):
    return IcomClient(
        session,
        "127.0.0.1",
        key or derive_key(GATEWAY_PASSWORD, PRIVATE_PASSWORD),
        port=gateway.port,
        **{"gap": 0.0, **kwargs},
    )


async def test_reads_a_document_with_the_expected_request(gateway, session):
    client = client_for(gateway, session)
    reply = await client.get("/system/sensors/temperatures/outdoor_t1")
    assert reply.document["value"] == 4.5
    assert reply.body.startswith(b'{"id"')
    assert gateway.requests == ["/system/sensors/temperatures/outdoor_t1"]
    seen = gateway.headers_seen[0]
    assert seen["Accept"] == "application/json"
    assert seen["User-Agent"] == icom_client.USER_AGENT
    assert client.metrics.requests == client.metrics.answered == 1


async def test_requests_never_overlap_and_keep_the_gap(gateway, session):
    gateway.delay = 0.02
    client = client_for(gateway, session, gap=0.1)
    started = time.monotonic()
    await asyncio.gather(*(client.get("/gateway/uuid") for _ in range(4)))
    elapsed = time.monotonic() - started
    assert gateway.max_concurrent == 1
    assert elapsed >= 3 * 0.1  # a gap after each of the first three
    assert len(gateway.requests) == 4


async def test_the_gap_is_also_kept_after_a_failure(gateway, session):
    gateway.path_status["/missing"] = 404
    client = client_for(gateway := gateway, session, gap=0.15)
    started = time.monotonic()
    with pytest.raises(IcomError):
        await client.get("/missing")
    await client.get("/gateway/uuid")
    assert time.monotonic() - started >= 0.15


@pytest.mark.parametrize(
    ("status", "problem"),
    [
        (404, IcomProblem.NOT_FOUND),
        (405, IcomProblem.NOT_FOUND),
        (403, IcomProblem.FORBIDDEN),
        (401, IcomProblem.FORBIDDEN),
        (503, IcomProblem.BUSY),
        (429, IcomProblem.BUSY),
        (500, IcomProblem.HTTP_ERROR),
        (502, IcomProblem.HTTP_ERROR),
        (302, IcomProblem.INVALID_RESPONSE),
    ],
)
async def test_http_status_maps_to_a_fixed_problem(gateway, session, status, problem):
    gateway.status = status
    with pytest.raises(IcomError) as raised:
        await client_for(gateway, session).get("/gateway/uuid")
    assert raised.value.problem is problem and raised.value.status == status
    assert (problem in PATH_PROBLEMS) is (
        problem in (IcomProblem.NOT_FOUND, IcomProblem.FORBIDDEN)
    )


async def test_retry_after_is_kept_and_bounded(gateway, session):
    gateway.status = 503
    for header, expected in (
        ("17", 17.0),
        ("999999", icom_client.MAX_RETRY_AFTER),
        ("x", None),
        ("-3", None),
    ):
        gateway.retry_after = header
        with pytest.raises(IcomError) as raised:
            await client_for(gateway, session).get("/gateway/uuid")
        assert raised.value.retry_after == expected


async def test_a_hanging_gateway_times_out(gateway, session):
    gateway.hang = True
    started = time.monotonic()
    with pytest.raises(IcomError) as raised:
        await client_for(gateway, session, timeout=0.3).get("/gateway/uuid")
    assert raised.value.problem is IcomProblem.TIMEOUT
    assert time.monotonic() - started < 3


async def test_a_closed_port_is_a_connection_failure(gateway, session):
    client = client_for(gateway, session)
    await gateway.stop()
    with pytest.raises(IcomError) as raised:
        await client.get("/gateway/uuid")
    assert raised.value.problem is IcomProblem.CONNECT
    assert client.metrics.failures == {"connection_failed": 1}


async def test_a_wrong_password_is_undecryptable_not_invalid(gateway, session):
    client = client_for(gateway, session, key=derive_key(GATEWAY_PASSWORD, "other"))
    with pytest.raises(IcomError) as raised:
        await client.get("/gateway/uuid")
    assert raised.value.problem is IcomProblem.UNDECRYPTABLE


async def test_noise_of_another_key_is_undecryptable(gateway, session):
    gateway.noise = True
    with pytest.raises(IcomError) as raised:
        await client_for(gateway, session).get("/gateway/uuid")
    assert raised.value.problem is IcomProblem.UNDECRYPTABLE


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<html>busy</html>", id="html"),
        pytest.param(b"", id="empty"),
        pytest.param(b"QUJD", id="short"),
        pytest.param(b"A" * 300_000, id="oversized"),
    ],
)
async def test_no_encrypted_text_is_an_invalid_response(gateway, session, body):
    gateway.raw = body
    with pytest.raises(IcomError) as raised:
        await client_for(gateway, session).get("/gateway/uuid")
    assert raised.value.problem is IcomProblem.INVALID_RESPONSE


async def test_an_endless_answer_without_a_length_is_cut_off(gateway, session):
    gateway.stream = 400_000
    client = client_for(gateway, session)
    with pytest.raises(IcomError) as raised:
        await client.get("/gateway/uuid")
    assert raised.value.problem is IcomProblem.INVALID_RESPONSE


async def test_a_json_list_is_not_a_document(gateway, session):
    from custom_components.bosch_buderus_heating.icom_crypto import encrypt

    gateway.raw = encrypt(gateway.key, b"[1,2,3]")
    with pytest.raises(IcomError) as raised:
        await client_for(gateway, session).get("/gateway/uuid")
    assert raised.value.problem is IcomProblem.UNDECRYPTABLE


@pytest.mark.parametrize("path", ["gateway", "/a?refresh=true", "/a#b"])
async def test_only_plain_paths_are_requested(gateway, session, path):
    with pytest.raises(ValueError, match="invalid_icom_path"):
        await client_for(gateway, session).get(path)
    assert gateway.requests == []


async def test_an_unusable_address_is_a_connection_failure(gateway, session):
    client = IcomClient(
        session, "", derive_key(GATEWAY_PASSWORD, PRIVATE_PASSWORD), port=1
    )
    with pytest.raises(IcomError) as raised:
        await client.get("/gateway/uuid")
    assert raised.value.problem is IcomProblem.CONNECT


def test_errors_carry_only_a_fixed_category():
    error = IcomError(IcomProblem.TIMEOUT)
    assert (
        str(error) == "request_timeout" and repr(error.args) == "('request_timeout',)"
    )
