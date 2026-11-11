"""Z1: selecting the active weekly program (A or B) of an EMS/iCom circuit.

``/heatingCircuits/hcN/activeSwitchProgram`` is offered as a selection only
when the gateway allows at least two of the programs ``A`` and ``B``, the
current one among them, and every allowed program is readable and passes
the strict program check (``switch_programs``). A gateway with one program
gets no selection. The program type (``switchProgramMode``) and the program
names are never written here.

Pure: no Home Assistant import.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from .pointt.models import JsonScalar, Resource
from .switch_programs import assess_switch_program
from .write_policies import EnumWritePolicy

ACTIVE_PATTERN: Final = r"^/heatingCircuits/hc[1-9][0-9]?/activeSwitchProgram$"
PROGRAMS: Final = ("A", "B")
# The refusal token (and error text) of a program that cannot be read.
PROGRAM_NOT_READABLE: Final = "switch_program_not_readable"


def is_active_program_path(path: str) -> bool:
    return re.fullmatch(ACTIVE_PATTERN, path) is not None


def program_path(path: str, program: str) -> str:
    """The program a selection value names, e.g. ``.../switchPrograms/B``."""
    return f"{path.removesuffix('/activeSwitchProgram')}/switchPrograms/{program}"


def name_path(path: str, program: str) -> str:
    return f"{path.removesuffix('/activeSwitchProgram')}/switchPrograms/name{program}"


def program_readable(
    path: str, program: object, readings: Mapping[str, Resource]
) -> bool:
    """Whether the program ``program`` has a reading that passes the check."""
    if not isinstance(program, str) or program not in PROGRAMS:
        return False
    resource = readings.get(program_path(path, program))
    return resource is not None and assess_switch_program(resource).supported


def _value_check(
    resource: Resource, value: JsonScalar, related: Mapping[str, Resource]
) -> str | None:
    """Never select a program without a current, valid reading."""
    if program_readable(resource.path, value, related):
        return None
    return PROGRAM_NOT_READABLE


ACTIVE_SWITCH_PROGRAM_POLICY: Final = EnumWritePolicy(
    ACTIVE_PATTERN,
    frozenset({"stringValue"}),
    frozenset(PROGRAMS),
    known_only=True,
    options_rule="subset",
    min_options=2,
    current_required=True,
    value_check=_value_check,
)


def offered_programs(
    options: tuple[str, ...], path: str, readings: Mapping[str, Resource]
) -> tuple[str, ...]:
    """The advertised programs of an eligible selection, in A/B order, or
    none at all unless every one of them is readable and valid."""
    offered = tuple(program for program in PROGRAMS if program in options)
    if len(offered) < 2 or not all(
        program_readable(path, program, readings) for program in offered
    ):
        return ()
    return offered


def program_name(resource: Resource | None) -> str | None:
    """A program name the gateway reports (``switchPrograms/nameA``), if set."""
    if (
        resource is None
        or resource.metadata.resource_type != "stringValue"
        or not resource.has_value
        or not isinstance(resource.value, str)
    ):
        return None
    name = resource.value.strip()
    return name if name and name.isprintable() and len(name) <= 64 else None
