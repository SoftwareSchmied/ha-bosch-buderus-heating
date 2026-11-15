"""Explicit compatibility fixtures for tests that inject a legacy runtime."""

import ipaddress
from unittest.mock import patch

import aiohttp
import pytest

from custom_components.bosch_buderus_heating.config_flow import BoschBuderusConfigFlow
from custom_components.bosch_buderus_heating.pointt import PointTClient
from tests.helpers.open_patches import close_open_stacks

_MISSING = object()
_CLIENT = dict(vars(PointTClient))
_RESOLVE_HOST = aiohttp.TCPConnector._resolve_host
# Host names a test tried to look up for a real connection.
_LOOKUPS: list[str] = []


def _local(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


async def _no_real_lookup(self, host, port, traces=None):
    """Refuse the name lookup of a real connection (local test servers pass).

    The socket guard of the test plugin does not cover the name lookup,
    which runs outside Python's sockets. A request reaching the real
    network (a client method no fake answers) would otherwise depend on
    the runner's network and on resolver state left by earlier tests: a
    quick error on one machine, a hang until the test timeout on another.
    """
    if _local(host):
        return await _RESOLVE_HOST(self, host, port, traces=traces)
    _LOOKUPS.append(host)
    raise OSError(f"A test tried to reach {host}")


@pytest.fixture(autouse=True, scope="session")
def no_real_network():
    with patch.object(aiohttp.TCPConnector, "_resolve_host", _no_real_lookup):
        yield


@pytest.fixture
def legacy_platform_runtime():
    """Keep injected v3 platform fixtures distinct from actual v4 setup tests."""
    with patch.object(BoschBuderusConfigFlow, "VERSION", 3):
        yield


def _restore_client() -> list[str]:
    """Put back what a test left patched on the PointT client."""
    now = dict(vars(PointTClient))
    left = sorted(
        name
        for name in _CLIENT.keys() | now.keys()
        if _CLIENT.get(name, _MISSING) is not now.get(name, _MISSING)
    )
    for name in left:
        if name in _CLIENT:
            setattr(PointTClient, name, _CLIENT[name])
        else:
            delattr(PointTClient, name)
    return left


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Fail the test that leaves patches behind and undo them.

    Checked after all fixtures of the test are finished. Open fake-cloud
    patches (a test that failed before its own teardown) are closed first;
    whatever is still patched on the PointT client is then put back. Without
    this, every later test of the same run talks to the leftover fake answers
    and waits for sources that never come up. A test that tried to reach
    the real network fails as well.
    """
    try:
        result = yield
    finally:
        try:
            stacks = close_open_stacks()
        finally:
            left = _restore_client()
    lookups = sorted(set(_LOOKUPS))
    _LOOKUPS.clear()
    if lookups:
        pytest.fail(f"The test tried to reach the network: {', '.join(lookups)}")
    if stacks:
        pytest.fail(f"The test left {stacks} fake-cloud patch stack(s) open")
    if left:
        pytest.fail(f"The test left PointTClient patched: {', '.join(left)}")
    return result
