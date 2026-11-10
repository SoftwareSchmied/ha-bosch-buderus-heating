"""RRC 2.0 entities: system status, maintenance, sensors, energy.

Owned by work package W5. ``rrc_platforms.track`` calls the tracker of each
platform listed in ``TRACKERS`` once per coordinator; see
``docs/dev/rrc-packages.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from homeassistant.const import Platform

from .rrc_platforms import Tracker

TRACKERS: Mapping[Platform, Tracker] = MappingProxyType({})
