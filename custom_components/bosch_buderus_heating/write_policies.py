"""Write policies, the write context and the RRC read-back schedule.

Pure data without Home Assistant imports, so that the RRC contracts
(``rrc_contract_*``) can declare policies without importing the write
service. ``writes`` re-exports every name here.

Every field added for the RRC contracts has a default that keeps the
behaviour of the existing policies unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, TypedDict

from .pointt import Resource, WriteNotConfirmed, WriteValidationError
from .pointt.models import JsonScalar

# S7: an RRC controller applies a change late. Read back after 1, 2, 4 and
# 8 seconds (15 s in all, well inside the 60 s write lock), never write twice.
RRC_READ_BACK_DELAYS: Final[tuple[float, ...]] = (1.0, 2.0, 4.0, 8.0)
# The schedule of every other gateway: 0.5, 1 and 2 seconds.
DEFAULT_READ_BACK_DELAY: Final = 0.5
DEFAULT_READ_BACK_ATTEMPTS: Final = 3

# A contract rule: the resource to write, the requested wire value and the
# current readings the coordinator holds (fresh snapshots and the live reads
# of ``expected``). Returns a refusal token, which is also the translation key
# of the error shown, or ``None``.
type ValueCheck = Callable[[Resource, JsonScalar, Mapping[str, Resource]], str | None]
# A read-back rule: a valid reading of the written path and the requested
# value. Returns a token (the translation key) that ends the read-back as
# not confirmed, or ``None`` to go on.
type ReadBackCheck = Callable[[Resource, JsonScalar], str | None]


def _no_equivalents() -> Mapping[str, frozenset[str]]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ControlContext:
    """S11: what the coordinator knows of the plant a control belongs to.

    RRC contract variants apply only with ``rrc_plant``; the same paths exist
    on other gateways. ``None`` in place of a context means a plain one.
    """

    rrc_plant: bool = False


GENERIC_CONTEXT: Final = ControlContext()
RRC_CONTEXT: Final = ControlContext(rrc_plant=True)


class RrcFlag(TypedDict, total=False):
    """The ``rrc_plant`` keyword of the catalog and entity builders."""

    rrc_plant: bool


def rrc_kwargs(rrc_plant: bool) -> RrcFlag:
    """``rrc_plant=True`` only for an RRC controller; other gateways call
    the builders exactly as before."""
    return {"rrc_plant": True} if rrc_plant else {}


def context_of(owner: object) -> ControlContext:
    """The write context of a coordinator; a plain one for any other owner
    (a local view, or a test double without ``control_context``)."""
    method = getattr(owner, "control_context", None)
    context = method() if callable(method) else None
    return context if isinstance(context, ControlContext) else GENERIC_CONTEXT


@dataclass(frozen=True, slots=True)
class EnumWritePolicy:
    """Identify a supported enum function and its known presentation codes."""

    path_pattern: str
    resource_types: frozenset[str]
    known_values: frozenset[str]
    known_only: bool = False
    action: bool = False
    enabled_by_default: bool = True
    # "subset": every advertised option must be known; "exact": the advertised
    # options must be exactly the known values. Anything else rejects the control.
    options_rule: str = "any"
    # The options offered only when the resource has no ``allowedValues`` key
    # at all and its current value is one of them (#113: ``userMode``).
    fallback_options: tuple[str, ...] = ()
    # Offered with ``fallback_options`` under the same condition, but only
    # while the current value is one of them (for example modes that only
    # some heat sources have); the options are then both tuples together.
    fallback_current_options: tuple[str, ...] = ()
    # Advertised placeholders left out of the options, such as "" (never
    # written); fewer than ``min_options`` usable options leave no control.
    ignored_options: frozenset[str] = frozenset()
    min_options: int = 1
    # A switch over two exact wire values, such as "true"/"false" (S2).
    on_value: str | None = None
    off_value: str | None = None
    # Readings that confirm a written value, such as "off" for "Off".
    equivalent_reads: Mapping[str, frozenset[str]] = field(
        default_factory=_no_equivalents, hash=False
    )
    value_check: ValueCheck | None = None
    read_back_check: ReadBackCheck | None = None
    # The flags of a contract setting (``writes._contract_availability_issue``):
    # ``available`` false gives no control (an existing entity goes
    # unavailable); ``available`` true with ``used`` false keeps the control
    # and refuses every write unsent (``ControlUnused``); missing flags count
    # as true, malformed ones give no control. ``unused_writable`` keeps the
    # earlier rule for settings whose ``used`` flag the controller does not
    # tie to the control (the heating circuit): ``used`` false stays
    # writable, ``available`` false with ``used`` true is shown and locked
    # (S8, ``ControlLocked``).
    unused_writable: bool = False

    @property
    def switch_values(self) -> tuple[str, str] | None:
        """The on and off wire values of a two-value switch, if declared."""
        if self.on_value is None or self.off_value is None:
            return None
        return self.on_value, self.off_value

    def confirms(self, read: object, requested: object) -> bool:
        """Whether a read value shows the requested one (exactly or alike)."""
        return read == requested or (
            isinstance(requested, str)
            and isinstance(read, str)
            and read in self.equivalent_reads.get(requested, frozenset())
        )


@dataclass(frozen=True, slots=True)
class NumberWritePolicy:
    """Allowlist and UI constraints for one numeric control family."""

    path_pattern: str
    unit: str
    step: float
    enabled_by_default: bool = True
    # Use the advertised step (when valid) for the UI and the value grid.
    live_step: bool = False
    # C31: the gateway rounds these Celsius setpoints to ``step``; a value off
    # that grid would be stored differently and never confirmed. Reject it.
    write_grid: bool = False
    # S2: send the value as an integer (the contract says ``int``).
    wire_integer: bool = False
    # A contract may write a path without a number entity of its own (for
    # example a climate entity's temporary setpoint).
    create_entity: bool = True
    # The unit the number entity shows instead of the resource's; "" shows
    # none. ``None`` keeps the resource's unit.
    ui_unit_override: str | None = None
    # The metadata units accepted instead of the family of ``unit`` (for
    # example {"", "other"} for a dimensionless setting).
    accepted_units: frozenset[str] | None = None
    # S4: values must lie on the grid of the advertised step (or ``step``
    # without one), counted from ``minValue``; others are refused, not rounded.
    strict_grid: bool = False
    value_check: ValueCheck | None = None
    read_back_check: ReadBackCheck | None = None
    # As ``EnumWritePolicy.unused_writable``.
    unused_writable: bool = False


type WritePolicy = EnumWritePolicy | NumberWritePolicy


class ContractRejected(WriteValidationError):
    """A contract rule refused the value before anything was sent.

    ``reason`` is a stable token and the translation key of the error.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.translation_key = reason
        super().__init__(f"A contract rule refused the value: {reason}")


class RrcWriteNotConfirmed(WriteNotConfirmed):
    """S7: an RRC change was sent but not (yet) confirmed by a reading.

    The controller may still apply it; ``translation_key`` names the error
    shown, by default the shared "pending confirmation" text.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "rrc_write_pending_confirmation",
        reading: Resource | None = None,
    ) -> None:
        self.reason = reason
        self.translation_key = reason
        # ZONE-03: a valid reading that ended the wait (``read_back_check``);
        # it is current and is published as such.
        self.reading = reading
        super().__init__(message)
