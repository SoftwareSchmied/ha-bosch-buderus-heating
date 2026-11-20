"""Leaves that exist only while a circuit's season mode allows them.

A room controller answers HTTP 404 for the heating limit of the
summer/winter switching (``suWiThreshold``) while the switching is ``off``.
That 404 is a state of the plant, not proof that the leaf does not exist:
the coordinator keeps reading such a leaf with its regular group instead of
pausing it for a day, so that it returns when the mode changes.
"""

from __future__ import annotations

import re
from typing import Final

_SEASON_DEPENDENT: Final = re.compile(
    r"^/heatingCircuits/[^/]+/suWi(?:Cooling)?Threshold$"
)


def is_season_dependent(path: str) -> bool:
    return _SEASON_DEPENDENT.fullmatch(path) is not None
