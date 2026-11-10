"""RRC 2.0 contract of the zones: climate, temporary setpoint, details.

Owned by work package W1. Empty tables change nothing; see
``docs/dev/rrc-packages.md`` for what each table does.
"""

from __future__ import annotations

from types import MappingProxyType

from .rrc_contract_base import RRC_ID, RrcContract

CONTRACT = RrcContract(
    name="zone",
    # The temporary setpoint stays read-only until its lifecycle is known
    # (moved here unchanged from the write service; W1 lifts it).
    blocked=MappingProxyType(
        {
            rf"^/zones/zn{RRC_ID}/clockOverride/temperatureHeating$": (
                "rrc_override_lifecycle_unverified"
            ),
        }
    ),
)
