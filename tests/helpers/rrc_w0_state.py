"""The RRC contracts as W0 left them, for the tests that pin W0 itself.

The W0 tests check the foundation alone: no package feature, no new entity,
no new request. Once a package fills its contract (W1 to W6), these tests
run against this state instead of the shipped contracts: every contract
keeps its name, only the zone contract keeps the block W0 moved there, and
no package tracker adds entities.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import MappingProxyType
from unittest.mock import patch

from custom_components.bosch_buderus_heating import rrc_contracts, rrc_platforms
from custom_components.bosch_buderus_heating.rrc_contract_base import (
    RRC_ID,
    RrcContract,
)
from custom_components.bosch_buderus_heating.rrc_contracts import Registry

# The only entry W0 shipped: the temporary setpoint block, moved unchanged
# from the write service into the zone contract.
W0_BLOCKED = MappingProxyType(
    {
        rf"^/zones/zn{RRC_ID}/clockOverride/temperatureHeating$": (
            "rrc_override_lifecycle_unverified"
        ),
    }
)


def w0_contracts() -> tuple[RrcContract, ...]:
    """The shipped contracts reduced to what W0 declared."""
    return tuple(
        RrcContract(
            name=contract.name,
            blocked=W0_BLOCKED if contract.name == "zone" else MappingProxyType({}),
        )
        for contract in rrc_contracts.CONTRACTS
    )


@contextmanager
def w0_state(*contracts: RrcContract) -> Iterator[Registry]:
    """Run with the W0 contracts (and ``contracts`` on top), without any
    package tracker."""
    registry = Registry((*w0_contracts(), *contracts))
    original = rrc_contracts._registry
    rrc_contracts._registry = registry
    empty = tuple((name, MappingProxyType({})) for name in rrc_platforms.ENTITY_MODULES)
    try:
        with patch.object(rrc_platforms, "trackers", lambda: empty):
            yield registry
    finally:
        rrc_contracts._registry = original
