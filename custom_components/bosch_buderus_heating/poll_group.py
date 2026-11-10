"""The cloud polling cadences, apart from the catalog that assigns them.

A module of its own so that the RRC contracts (``rrc_contracts``) can name a
group without importing the resource catalog, which consults them.
"""

from __future__ import annotations

from enum import StrEnum


class PollGroup(StrEnum):
    """Cloud polling cadence for one discovered resource."""

    FAST = "fast"
    NOTIFICATIONS = "notifications"
    CONTROL = "control"
    ENERGY = "energy"
    SLOW = "slow"
    # O50: containers and lists without an entity (new devices or zones).
    HOURLY = "hourly"
    STATIC = "static"
