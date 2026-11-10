"""RRC 2.0 entities: hot water mode, extra hot water, disinfection.

Owned by work package W3. ``rrc_platforms.track`` calls the tracker of each
platform listed in ``TRACKERS`` once per coordinator; see
``docs/dev/rrc-packages.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from homeassistant.const import Platform

from .rrc_platforms import Tracker

TRACKERS: Mapping[Platform, Tracker] = MappingProxyType({})
