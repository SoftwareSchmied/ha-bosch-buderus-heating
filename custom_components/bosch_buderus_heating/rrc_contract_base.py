"""The shape of one RRC 2.0 feature contract (``rrc_contract_*``).

Pure data without Home Assistant imports. Every table maps a path pattern,
matched with ``re.fullmatch`` against a concrete resource path, to what the
contract declares for it. ``rrc_contracts`` collects the contracts, rejects
conflicts and answers the lookups; see ``docs/dev/rrc-packages.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal

from .pointt import Resource
from .poll_group import PollGroup
from .write_policies import EnumWritePolicy, NumberWritePolicy

# A canonical numeric RRC identifier (zones znN, programs pgN, devices deviceN).
RRC_ID: Final = r"(?:0|[1-9][0-9]{0,3})"

type ContractDiagnostics = Callable[[Mapping[str, Resource]], Mapping[str, object]]
type Category = Literal["config", "diagnostic"]


def _empty[K, V]() -> Mapping[K, V]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class RrcContract:
    """What one feature package declares for RRC 2.0 room controllers.

    Everything applies only to a plant the coordinator recognised as an RRC
    controller (S11): the write policies through the write context, the other
    tables through the callers that know the plant. Empty tables change
    nothing.
    """

    # A stable token for diagnostics and write counters ("zone", "dhw" ...).
    name: str
    enum_policies: tuple[EnumWritePolicy, ...] = ()
    number_policies: tuple[NumberWritePolicy, ...] = ()
    # Path pattern -> refusal token; refuses writing on every gateway.
    blocked: Mapping[str, str] = field(default_factory=_empty)
    poll_groups: Mapping[str, PollGroup] = field(default_factory=_empty)
    # Path pattern -> (German, English) name of its entity or control.
    names: Mapping[str, tuple[str, str]] = field(default_factory=_empty)
    # Path pattern -> translation key, or platform -> translation key.
    entity_keys: Mapping[str, str | Mapping[str, str]] = field(default_factory=_empty)
    # Path pattern -> raw wire value -> option key of the select.
    option_keys: Mapping[str, Mapping[str, str]] = field(default_factory=_empty)
    # Paths whose generic sensor becomes a disabled diagnostic one while the
    # contract offers a control for them.
    read_only_mirrors: tuple[str, ...] = ()
    # Additional leaves below ``/zones/znN`` (W1 only).
    zone_fields: tuple[str, ...] = ()
    # Radio device type of ``/devices/list`` -> leaves below
    # ``/devices/deviceN/`` (W6 only).
    device_leaves: Mapping[str, tuple[str, ...]] = field(default_factory=_empty)
    # Roots read only on an RRC controller (W2 ``/events``, W5 ``/energy``).
    extra_roots: tuple[str, ...] = ()
    # Container path pattern -> leaf names probed below it on an RRC plant.
    optional_children: Mapping[str, tuple[str, ...]] = field(default_factory=_empty)
    # Value-free diagnostics of the contract's resources (S12).
    diagnostics: ContractDiagnostics | None = None
    # Path patterns whose capability maturity is "understood".
    understood: tuple[str, ...] = ()
    # Path pattern -> default activation of the generic entity.
    default_enabled: Mapping[str, bool] = field(default_factory=_empty)
    # Path pattern -> entity category of the generic entity or control.
    categories: Mapping[str, Category] = field(default_factory=_empty)
    # Path pattern -> icon of the generic entity or control.
    icons: Mapping[str, str] = field(default_factory=_empty)
    # Path patterns of controls shown in the controls dialog.
    dialog_paths: tuple[str, ...] = ()
    # Further (German, English) texts the package builds with ``localize``
    # from data rather than literals, for the translation checks.
    texts: tuple[tuple[str, str], ...] = ()
