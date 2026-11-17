"""RRC 2.0 contract of the further room controller settings: automatic
absence, notification light, presence sensor, solar yield, hot water
system and hydraulic balancing.

Expected, untested on a real plant beyond the leaf list and the JSON types
of a TC100.2 (issue 122); see ``docs/dev/rrc-packages.md`` for what each
table does.

- Automatic absence and the notification light are string flags
  "true"/"false" without ``allowedValues`` (S2, P38); they become switches
  with exactly these wire values, disabled by default. A plant whose flag
  reads differently gets no switch.
- The list of users of the automatic absence is never requested (it holds
  personal data, see ``resource_catalog``); nothing here reads it.
- The coming-home flag and its prediction, the presence sensitivity, the
  total solar yield, the hot water system and the hydraulic balancing are
  read only. The hydraulic balancing and the presence sensitivity are
  writable on the controller but deliberately offered as readings only.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from .poll_group import PollGroup
from .rrc_contract_base import RRC_ID, RrcContract
from .write_policies import EnumWritePolicy

AUTO_AWAY: Final = r"^/system/autoAway/enabled$"
NOTIFICATION_LIGHT: Final = r"^/gateway/notificationLight/enabled$"
COMING_HOME: Final = "/system/autoAway/comingHome/active"
COMING_HOME_PREDICTION: Final = "/system/autoAway/comingHome/prediction"
PRESENCE_SENSITIVITY: Final = "/gateway/pirSensitivity"
HYDRAULIC_BALANCING: Final = "/system/hydraulicBalancing/enabled"
SOLAR_YIELD: Final = rf"^/solarCircuits/sc{RRC_ID}/totalSolarGain$"
HOT_WATER_SYSTEM: Final = rf"^/dhwCircuits/dhw{RRC_ID}/hotWaterSystem$"


def _flag(pattern: str) -> EnumWritePolicy:
    """S2/P38: a switch over the strings "true"/"false" and nothing else."""
    return EnumWritePolicy(
        pattern,
        frozenset({"stringValue"}),
        frozenset({"true", "false"}),
        known_only=True,
        enabled_by_default=False,
        options_rule="exact",
        fallback_options=("true", "false"),
        on_value="true",
        off_value="false",
    )


AUTO_AWAY_POLICY: Final = _flag(AUTO_AWAY)
NOTIFICATION_LIGHT_POLICY: Final = _flag(NOTIFICATION_LIGHT)

CONTRACT = RrcContract(
    name="extras",
    enum_policies=(AUTO_AWAY_POLICY, NOTIFICATION_LIGHT_POLICY),
    poll_groups=MappingProxyType(
        {
            AUTO_AWAY: PollGroup.CONTROL,
            NOTIFICATION_LIGHT: PollGroup.CONTROL,
            # Writable on the controller, shown as a reading only.
            rf"^{PRESENCE_SENSITIVITY}$": PollGroup.SLOW,
        }
    ),
    names=MappingProxyType(
        {
            AUTO_AWAY: ("Auto-Abwesenheit", "Automatic absence"),
            NOTIFICATION_LIGHT: ("Benachrichtigungslicht", "Notification light"),
        }
    ),
    entity_keys=MappingProxyType(
        {
            AUTO_AWAY: MappingProxyType({"switch": "rrc_auto_away"}),
            NOTIFICATION_LIGHT: MappingProxyType({"switch": "rrc_notification_light"}),
        }
    ),
    read_only_mirrors=(AUTO_AWAY, NOTIFICATION_LIGHT),
    categories=MappingProxyType({AUTO_AWAY: "config", NOTIFICATION_LIGHT: "config"}),
    icons=MappingProxyType(
        {
            AUTO_AWAY: "mdi:map-marker-radius-outline",
            NOTIFICATION_LIGHT: "mdi:led-on",
        }
    ),
)
