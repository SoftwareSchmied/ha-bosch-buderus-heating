"""RRC 2.0 contract of absence: away mode, events, boost, fireplace.

Owned by work package W2; see ``docs/dev/rrc-packages.md`` for what each
table does. Everything here is expected, untested on hardware:

- Away mode and the fireplace function are string flags "true"/"false"
  without ``allowedValues`` (S2, P38); they become switches with exactly
  these wire values. The away temperature becomes a number.
- The events lists are read for the calendar (``rrc_event_calendar``);
  ``/events/evN`` and ``/events/activeEvent`` are not polled.
- Boost is written only through ``boostShortcut`` (W2-E1, ``rrc_boost``);
  its single leaves stay readings.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from .pointt import Resource
from .poll_group import PollGroup
from .rrc_contract_base import RRC_ID, RrcContract
from .write_policies import EnumWritePolicy, NumberWritePolicy

AWAY: Final = r"^/system/awayMode/enabled$"
AWAY_TEMPERATURE: Final = r"^/system/awayMode/temperature$"
FIREPLACE: Final = r"^/system/fireplace/enabled$"
EVENTS: Final = r"^/events(?:/(?:list[123]|activeEvent|ev" + RRC_ID + r"))?$"
EVENT_LISTS: Final = r"^/events/list[123]$"
EVENT_ENTRIES: Final = rf"^/events/(?:ev{RRC_ID}|activeEvent)$"
BOOST: Final = r"^/heatingCircuits/hc1/boostShortcut$"
BOOST_REMAINING: Final = r"^/heatingCircuits/hc1/boostRemainingTime$"
# A structure written only by this package's own entities (S12 counts it
# for this contract); never as a scalar control.
STRUCTURED: Final = "rrc_structured_write"

# Weekday events: the title and the description of a day run like another
# weekday (German, English).
WEEKDAY_TITLES: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "Mo": ("Wie Montag", "Like Monday"),
        "Tu": ("Wie Dienstag", "Like Tuesday"),
        "We": ("Wie Mittwoch", "Like Wednesday"),
        "Th": ("Wie Donnerstag", "Like Thursday"),
        "Fr": ("Wie Freitag", "Like Friday"),
        "Sa": ("Wie Samstag", "Like Saturday"),
        "Su": ("Wie Sonntag", "Like Sunday"),
    }
)
WEEKDAY_DESCRIPTIONS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "Mo": (
            "Der Tag wird wie ein Montag geregelt",
            "The day is controlled like a Monday",
        ),
        "Tu": (
            "Der Tag wird wie ein Dienstag geregelt",
            "The day is controlled like a Tuesday",
        ),
        "We": (
            "Der Tag wird wie ein Mittwoch geregelt",
            "The day is controlled like a Wednesday",
        ),
        "Th": (
            "Der Tag wird wie ein Donnerstag geregelt",
            "The day is controlled like a Thursday",
        ),
        "Fr": (
            "Der Tag wird wie ein Freitag geregelt",
            "The day is controlled like a Friday",
        ),
        "Sa": (
            "Der Tag wird wie ein Samstag geregelt",
            "The day is controlled like a Saturday",
        ),
        "Su": (
            "Der Tag wird wie ein Sonntag geregelt",
            "The day is controlled like a Sunday",
        ),
    }
)


def _flag(pattern: str, *, enabled_by_default: bool = True) -> EnumWritePolicy:
    """S2/P38: a switch over the strings "true"/"false" and nothing else."""
    return EnumWritePolicy(
        pattern,
        frozenset({"stringValue"}),
        frozenset({"true", "false"}),
        known_only=True,
        enabled_by_default=enabled_by_default,
        options_rule="exact",
        fallback_options=("true", "false"),
        on_value="true",
        off_value="false",
    )


AWAY_POLICY: Final = _flag(AWAY)
FIREPLACE_POLICY: Final = _flag(FIREPLACE, enabled_by_default=False)
AWAY_TEMPERATURE_POLICY: Final = NumberWritePolicy(
    AWAY_TEMPERATURE, "C", 0.5, strict_grid=True
)


def _diagnostics(resources: Mapping[str, Resource]) -> Mapping[str, object]:
    # Imported late: they import ``rrc``, which imports the contracts.
    from . import rrc_boost, rrc_events

    return {
        "events": rrc_events.diagnostics(resources),
        **rrc_boost.diagnostics(resources),
    }


CONTRACT = RrcContract(
    name="absence",
    enum_policies=(AWAY_POLICY, FIREPLACE_POLICY),
    number_policies=(AWAY_TEMPERATURE_POLICY,),
    blocked=MappingProxyType({EVENTS: STRUCTURED, BOOST: STRUCTURED}),
    poll_groups=MappingProxyType(
        {
            AWAY: PollGroup.CONTROL,
            AWAY_TEMPERATURE: PollGroup.CONTROL,
            FIREPLACE: PollGroup.CONTROL,
            EVENT_LISTS: PollGroup.CONTROL,
            # Write targets only; their existence comes from the lists.
            EVENT_ENTRIES: PollGroup.STATIC,
            BOOST: PollGroup.CONTROL,
            BOOST_REMAINING: PollGroup.FAST,
        }
    ),
    names=MappingProxyType(
        {
            AWAY: ("Abwesenheit", "Away mode"),
            AWAY_TEMPERATURE: ("Abwesenheitstemperatur", "Away temperature"),
            FIREPLACE: ("Kaminfunktion", "Fireplace function"),
        }
    ),
    entity_keys=MappingProxyType(
        {
            AWAY: MappingProxyType({"switch": "away_mode"}),
            AWAY_TEMPERATURE: MappingProxyType({"number": "rrc_away_temperature"}),
            FIREPLACE: MappingProxyType({"switch": "rrc_fireplace"}),
        }
    ),
    # The boost structure's generic sensor, which ``understood`` brings,
    # stays a disabled diagnostic one: the boost entities show it.
    read_only_mirrors=(AWAY, AWAY_TEMPERATURE, FIREPLACE, BOOST),
    extra_roots=("/events",),
    # Read even when ``/events`` does not name its lists.
    optional_children=MappingProxyType({r"^/events$": ("list1", "list2", "list3")}),
    diagnostics=_diagnostics,
    # Its switch and numbers are entities of their own (``…:switch``).
    understood=(BOOST,),
    categories=MappingProxyType({AWAY_TEMPERATURE: "config", FIREPLACE: "config"}),
    icons=MappingProxyType(
        {
            AWAY: "mdi:home-export-outline",
            AWAY_TEMPERATURE: "mdi:home-thermometer-outline",
            FIREPLACE: "mdi:fireplace",
        }
    ),
    texts=(*WEEKDAY_TITLES.values(), *WEEKDAY_DESCRIPTIONS.values()),
)
