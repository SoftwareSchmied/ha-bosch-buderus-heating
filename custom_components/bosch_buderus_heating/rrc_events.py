"""Events of an RRC 2.0 room controller (``/events``): read, check, write.

The controller keeps whole-day events in up to three read-only lists
(``/events/list1`` to ``list3``): absences (type ``away``) and days run like
another weekday (types ``Mo`` to ``Su``). An event is created with
``POST /events``, changed with ``PUT /events/evN`` and deleted with
``DELETE /events/evN``, always for an ID the lists name; no ID is guessed.
Every body is ``[{"name", "start", "end", "type"}]`` with a Base64 name and
inclusive ``yyyy-MM-dd`` dates.

Pure: no Home Assistant import. ``rrc_event_calendar`` reads and writes
through these functions; all of it is expected, untested on hardware.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from . import rrc
from .pointt import Resource
from .pointt.models import JsonValue
from .write_policies import ContractRejected

EVENTS_ROOT: Final = "/events"
LIST_PATHS: Final = ("/events/list1", "/events/list2", "/events/list3")
LIST_TYPE: Final = "eventArray"
EVENT_PATH: Final = re.compile(r"/events/ev(0|[1-9][0-9]{0,3})")
TYPE_AWAY: Final = "away"
WEEKDAYS: Final = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
TYPES: Final = frozenset({TYPE_AWAY, *WEEKDAYS})
# The controller's limit of a name, in bytes of its UTF-8 text.
MAX_NAME_BYTES: Final = 32
MAX_LIST_ENTRIES: Final = 128
_DATE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ENTRY_KEYS: Final = frozenset({"id", "name", "start", "end", "type"})

# The refusal tokens, which are also the translation keys of the errors.
INVALID_NAME: Final = "rrc_event_invalid_name"
INVALID_DATES: Final = "rrc_event_invalid_dates"
OVERLAP: Final = "rrc_event_overlap"
TYPE_READ_ONLY: Final = "rrc_event_type_read_only"
REJECTED: Final = "rrc_event_rejected"
PENDING: Final = "rrc_event_pending"
UNSUPPORTED_FIELDS: Final = "rrc_event_unsupported_fields"
# The guard key shared by every event write (C36): while the outcome of one
# is unknown, no other event is written.
GUARD: Final = "events"


@dataclass(frozen=True, slots=True)
class EventFields:
    """What an event write sends: the wire name and the inclusive dates."""

    name: str
    start: date
    end: date
    kind: str

    def wire(self) -> JsonValue:
        """The body of a POST or PUT."""
        return [
            {
                "name": self.name,
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
                "type": self.kind,
            }
        ]

    def overlaps(self, other: EventFields) -> bool:
        """Whether two events share a day (a shared first or last day too)."""
        return self.start <= other.end and other.start <= self.end


@dataclass(frozen=True, slots=True)
class RrcEvent:
    """One valid event of a list."""

    identifier: int
    source: str
    fields: EventFields

    @property
    def path(self) -> str:
        return f"/events/ev{self.identifier}"

    @property
    def editable(self) -> bool:
        """Only absences are changed; weekday events are only deleted."""
        return self.fields.kind == TYPE_AWAY


@dataclass(frozen=True, slots=True)
class EventState:
    """The events of the readable lists and why writing is not possible."""

    events: tuple[RrcEvent, ...] = ()
    lists: tuple[str, ...] = ()
    # Known lists without a valid reading now.
    unreadable: tuple[str, ...] = ()
    # Entries left out: invalid or placeholder dates, unknown types.
    skipped: int = 0
    duplicates: bool = False

    @property
    def issue(self) -> str | None:
        """A value-free token of why no event may be written, or ``None``.

        An event the calendar cannot see could overlap a new one, so an
        unreadable list or a skipped entry stops writing as well.
        """
        if not self.lists:
            return "no_readable_list"
        if self.unreadable:
            return "list_unreadable"
        if self.duplicates:
            return "duplicate_ids"
        if self.skipped:
            return "invalid_entries"
        return None

    @property
    def writable(self) -> bool:
        return self.issue is None

    def identifiers(self) -> frozenset[int]:
        return frozenset(event.identifier for event in self.events)

    def find(self, identifier: int) -> RrcEvent | None:
        return next(
            (event for event in self.events if event.identifier == identifier), None
        )


def is_list_path(path: str) -> bool:
    return path in LIST_PATHS


def list_issue(resource: Resource) -> str | None:
    """Why a list reading supplies no events, or ``None`` when it does.

    Its ``writeable`` flag does not matter: the lists are read-only and
    events are written below ``/events``. P01: a list ``used`` "false" is
    not read.
    """
    if not is_list_path(resource.path):
        return "not_a_list"
    if resource.metadata.resource_type != LIST_TYPE:
        return "type"
    if not rrc.available(resource):
        return "flags"
    if (
        not resource.has_value
        or not isinstance(resource.value, list)
        or len(resource.value) > MAX_LIST_ENTRIES
    ):
        return "value"
    return None


def parse_date(value: object) -> date | None:
    """A ``yyyy-MM-dd`` date; placeholders and other forms are none."""
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_entry(item: object, source: str) -> RrcEvent | None:
    """One list entry, or ``None`` if any field is missing or invalid."""
    if not isinstance(item, dict) or not set(item) >= _ENTRY_KEYS:
        return None
    identifier, name, kind = item["id"], item["name"], item["type"]
    if (
        type(identifier) is not int
        or not 0 <= identifier <= 9999
        or not isinstance(name, str)
        or kind not in TYPES
    ):
        return None
    start, end = parse_date(item["start"]), parse_date(item["end"])
    if start is None or end is None or end < start:
        return None
    return RrcEvent(identifier, source, EventFields(name, start, end, str(kind)))


def parse_state(
    readings: Mapping[str, Resource | None], known: Iterable[str]
) -> EventState:
    """The events of every known list; ``None`` marks a list not read now."""
    events: list[RrcEvent] = []
    lists: list[str] = []
    unreadable: list[str] = []
    skipped = 0
    for path in sorted(set(known) & set(LIST_PATHS)):
        resource = readings.get(path)
        if resource is None or list_issue(resource) is not None:
            unreadable.append(path)
            continue
        lists.append(path)
        assert isinstance(resource.value, list)  # checked by list_issue
        for item in resource.value:
            event = parse_entry(item, path)
            if event is None:
                skipped += 1
            else:
                events.append(event)
    identifiers = [event.identifier for event in events]
    ordered = sorted(
        events, key=lambda event: (event.fields.start, event.fields.end, event.path)
    )
    return EventState(
        tuple(ordered),
        tuple(lists),
        tuple(unreadable),
        skipped,
        len(set(identifiers)) != len(identifiers),
    )


def known_lists(paths: Iterable[str]) -> tuple[str, ...]:
    """The list paths among the discovered resources."""
    present = set(paths)
    return tuple(path for path in LIST_PATHS if path in present)


def display_name(fields: EventFields) -> str | None:
    """The readable name: decoded Base64, else the text, else none (P05)."""
    return rrc.display_name(fields.name)


def encode_name(summary: object) -> str:
    """Trim, check and Base64-encode a new name.

    The controller takes 1 to 32 bytes of printable UTF-8 text.
    """
    if not isinstance(summary, str):
        raise ContractRejected(INVALID_NAME)
    text = summary.strip()
    encoded = text.encode("utf-8")
    if not text or len(encoded) > MAX_NAME_BYTES or not text.isprintable():
        raise ContractRejected(INVALID_NAME)
    return base64.b64encode(encoded).decode("ascii")


def check_dates(start: date, end: date, today: date) -> None:
    """Inclusive dates: the start not after the end, the end not before today."""
    if end < start or end < today:
        raise ContractRejected(INVALID_DATES)


def check_overlap(
    fields: EventFields, state: EventState, *, ignore: int | None = None
) -> None:
    """No two events share a day, the first and last days included."""
    for event in state.events:
        if event.identifier != ignore and event.fields.overlaps(fields):
            raise ContractRejected(OVERLAP)


def same_fields(left: EventFields, right: EventFields) -> bool:
    """Whether a reading shows the written fields; a name is equal also
    when only its Base64 form differs."""
    return (
        left.start == right.start
        and left.end == right.end
        and left.kind == right.kind
        and (
            left.name == right.name
            or (
                (decoded := rrc.display_name(left.name)) is not None
                and decoded == rrc.display_name(right.name)
            )
        )
    )


def created(
    before: frozenset[int], fields: EventFields, state: EventState
) -> RrcEvent | None:
    """The new event of a creation: the written fields under a new ID."""
    return next(
        (
            event
            for event in state.events
            if event.identifier not in before and same_fields(event.fields, fields)
        ),
        None,
    )


def changed(original: RrcEvent, fields: EventFields, state: EventState) -> bool:
    """Whether a change shows: the new fields are listed and the old entry
    is gone (the ID may stay or change)."""
    return any(same_fields(event.fields, fields) for event in state.events) and not any(
        event.identifier == original.identifier
        and same_fields(event.fields, original.fields)
        for event in state.events
    )


def deleted(original: RrcEvent, state: EventState) -> bool:
    """Whether a deletion shows: the entry is no longer listed."""
    return not any(
        event.identifier == original.identifier
        and same_fields(event.fields, original.fields)
        for event in state.events
    )


def diagnostics(resources: Mapping[str, Resource]) -> dict[str, object]:
    """S12: counts and tokens only; no names, dates or IDs."""
    known = known_lists(resources)
    if not known:
        return {"lists": 0}
    state = parse_state(resources, known)
    return {
        "lists": len(known),
        "lists_readable": len(state.lists),
        "events_away": sum(event.fields.kind == TYPE_AWAY for event in state.events),
        "events_weekday": sum(event.fields.kind != TYPE_AWAY for event in state.events),
        "entries_skipped": state.skipped,
        "write_issue": state.issue,
    }
