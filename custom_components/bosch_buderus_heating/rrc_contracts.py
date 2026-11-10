"""The RRC 2.0 feature contracts, collected and checked for conflicts.

Pure: no Home Assistant import. Each work package owns one contract module
(``rrc_contract_zone`` ... ``rrc_contract_devices``); this module only
aggregates them. Two contracts that declare the same path pattern in one
table, or two policies of one contract with the same pattern, are an import
error. Patterns that overlap without being equal are found at run time: a
path matched by policies of two contracts gets no control at all
(``ContractConflict``), and ``Registry.check_paths`` finds them in tests.

S11: the write policies apply only with an RRC write context
(``policy_for``). The other lookups are path-only; their callers consult them
only for a plant the coordinator recognised as an RRC controller.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from . import (
    rrc_contract_absence,
    rrc_contract_circuit,
    rrc_contract_devices,
    rrc_contract_dhw,
    rrc_contract_system,
    rrc_contract_zone,
)
from .i18n import localize
from .pointt import Resource
from .poll_group import PollGroup
from .rrc_contract_base import Category, RrcContract
from .write_policies import (
    ControlContext,
    EnumWritePolicy,
    NumberWritePolicy,
    WritePolicy,
)

CONTRACTS: Final[tuple[RrcContract, ...]] = (
    rrc_contract_zone.CONTRACT,
    rrc_contract_absence.CONTRACT,
    rrc_contract_dhw.CONTRACT,
    rrc_contract_circuit.CONTRACT,
    rrc_contract_system.CONTRACT,
    rrc_contract_devices.CONTRACT,
)
_LEAF = re.compile(r"[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*")
_ROOT = re.compile(r"/[A-Za-z0-9_]+")


class ContractConflict(ValueError):
    """Two contracts claim the same path, so neither may decide it."""


@dataclass(frozen=True, slots=True)
class _Entry[T]:
    pattern: re.Pattern[str]
    contract: str
    value: T


def _entries[T](
    table: str,
    items: Iterable[tuple[RrcContract, str, T]],
) -> tuple[_Entry[T], ...]:
    """Compile one table; an equal pattern in two places is a conflict."""
    seen: dict[str, str] = {}
    entries: list[_Entry[T]] = []
    for contract, pattern, value in items:
        if pattern in seen:
            raise ContractConflict(
                f"{table}: {seen[pattern]} and {contract.name} declare {pattern}"
            )
        seen[pattern] = contract.name
        try:
            compiled = re.compile(pattern)
        except re.error as err:
            raise ContractConflict(f"{table}: invalid pattern {pattern}") from err
        entries.append(_Entry(compiled, contract.name, value))
    return tuple(entries)


def _pairs[T](
    contracts: Sequence[RrcContract], table: str
) -> Iterator[tuple[RrcContract, str, T]]:
    for contract in contracts:
        mapping: Mapping[str, T] = getattr(contract, table)
        for pattern, value in mapping.items():
            yield contract, pattern, value


def _patterns(
    contracts: Sequence[RrcContract], table: str
) -> Iterator[tuple[RrcContract, str, bool]]:
    for contract in contracts:
        patterns: tuple[str, ...] = getattr(contract, table)
        for pattern in patterns:
            yield contract, pattern, True


class Registry:
    """Answer every contract lookup for one set of contracts."""

    def __init__(self, contracts: Sequence[RrcContract]) -> None:
        names = [contract.name for contract in contracts]
        if len(set(names)) != len(names) or not all(
            re.fullmatch(r"[a-z][a-z_]*", name) for name in names
        ):
            raise ContractConflict("Contract names must be unique tokens")
        self.contracts = tuple(contracts)
        policies: list[tuple[RrcContract, str, WritePolicy]] = []
        for contract in contracts:
            declared: tuple[WritePolicy, ...] = (
                *contract.enum_policies,
                *contract.number_policies,
            )
            policies.extend(
                (contract, policy.path_pattern, policy) for policy in declared
            )
        self._policies: tuple[_Entry[WritePolicy], ...] = _entries("policies", policies)
        self._policy_ids = frozenset(id(entry.value) for entry in self._policies)
        self._blocked: tuple[_Entry[str], ...] = _entries(
            "blocked", _pairs(contracts, "blocked")
        )
        self._poll_groups: tuple[_Entry[PollGroup], ...] = _entries(
            "poll_groups", _pairs(contracts, "poll_groups")
        )
        self._names: tuple[_Entry[tuple[str, str]], ...] = _entries(
            "names", _pairs(contracts, "names")
        )
        self._entity_keys: tuple[_Entry[str | Mapping[str, str]], ...] = _entries(
            "entity_keys", _pairs(contracts, "entity_keys")
        )
        self._option_keys: tuple[_Entry[Mapping[str, str]], ...] = _entries(
            "option_keys", _pairs(contracts, "option_keys")
        )
        self._mirrors: tuple[_Entry[bool], ...] = _entries(
            "read_only_mirrors", _patterns(contracts, "read_only_mirrors")
        )
        self._understood: tuple[_Entry[bool], ...] = _entries(
            "understood", _patterns(contracts, "understood")
        )
        self._default_enabled: tuple[_Entry[bool], ...] = _entries(
            "default_enabled", _pairs(contracts, "default_enabled")
        )
        self._categories: tuple[_Entry[Category], ...] = _entries(
            "categories", _pairs(contracts, "categories")
        )
        self._icons: tuple[_Entry[str], ...] = _entries(
            "icons", _pairs(contracts, "icons")
        )
        self._dialog: tuple[_Entry[bool], ...] = _entries(
            "dialog_paths", _patterns(contracts, "dialog_paths")
        )
        self._children: tuple[_Entry[tuple[str, ...]], ...] = _entries(
            "optional_children", _pairs(contracts, "optional_children")
        )
        self._zone_fields = self._unique_leaves(
            "zone_fields",
            (
                (contract, field)
                for contract in contracts
                for field in contract.zone_fields
            ),
        )
        self._roots = self._unique_leaves(
            "extra_roots",
            (
                (contract, root)
                for contract in contracts
                for root in contract.extra_roots
            ),
            pattern=_ROOT,
        )
        self._device_leaves: dict[str, tuple[str, ...]] = {}
        for kind in dict.fromkeys(
            kind for contract in contracts for kind in contract.device_leaves
        ):
            self._device_leaves[kind] = self._unique_leaves(
                f"device_leaves[{kind}]",
                (
                    (contract, leaf)
                    for contract in contracts
                    for leaf in contract.device_leaves.get(kind, ())
                ),
            )
        for entry in self._children:
            for leaf in entry.value:
                if not _LEAF.fullmatch(leaf):
                    raise ContractConflict(f"optional_children: invalid leaf {leaf}")
        self._lookup = lru_cache(maxsize=4096)(self._matching_policies)

    @staticmethod
    def _unique_leaves(
        table: str,
        items: Iterable[tuple[RrcContract, str]],
        *,
        pattern: re.Pattern[str] = _LEAF,
    ) -> tuple[str, ...]:
        seen: dict[str, str] = {}
        for contract, leaf in items:
            if not pattern.fullmatch(leaf):
                raise ContractConflict(f"{table}: invalid entry {leaf}")
            if leaf in seen:
                raise ContractConflict(
                    f"{table}: {seen[leaf]} and {contract.name} declare {leaf}"
                )
            seen[leaf] = contract.name
        return tuple(seen)

    def _matching_policies(self, path: str) -> tuple[_Entry[WritePolicy], ...]:
        return tuple(entry for entry in self._policies if entry.pattern.fullmatch(path))

    def policy_for(
        self, resource: Resource, context: ControlContext | None
    ) -> EnumWritePolicy | NumberWritePolicy | None:
        """S11: the contract policy of a resource, only on an RRC plant.

        Raises ``ContractConflict`` when policies of two contracts match.
        """
        if context is None or not context.rrc_plant:
            return None
        matches = self._lookup(resource.path)
        if len(matches) > 1:
            raise ContractConflict("Several contract policies match one path")
        return matches[0].value if matches else None

    def owns(self, policy: object) -> bool:
        """Whether a policy object is one of the contract policies."""
        return id(policy) in self._policy_ids

    def check_paths(self, paths: Iterable[str]) -> None:
        """Raise ``ContractConflict`` for a path that two entries of one
        table match (for tests with the paths of a fixture)."""
        tables = (
            self._policies,
            self._blocked,
            self._poll_groups,
            self._names,
            self._entity_keys,
            self._option_keys,
            self._mirrors,
            self._understood,
            self._default_enabled,
            self._categories,
            self._icons,
            self._dialog,
            self._children,
        )
        for path in paths:
            for table in tables:
                if sum(1 for entry in table if entry.pattern.fullmatch(path)) > 1:
                    raise ContractConflict(f"Two contract entries match {path}")

    def contract_for(self, path: str) -> str | None:
        """The contract whose write policy or block covers a path."""
        for entry in (*self._lookup(path), *self._blocked):
            if entry.pattern.fullmatch(path):
                return entry.contract
        return None

    @staticmethod
    def _first[T](entries: tuple[_Entry[T], ...], path: str) -> T | None:
        for entry in entries:
            if entry.pattern.fullmatch(path):
                return entry.value
        return None

    def blocked_reason(self, path: str) -> str | None:
        return self._first(self._blocked, path)

    def poll_group(self, path: str) -> PollGroup | None:
        return self._first(self._poll_groups, path)

    def name(self, path: str, language: str) -> str | None:
        names = self._first(self._names, path)
        return localize(language, *names) if names is not None else None

    def entity_key(self, path: str, platform: str) -> str | None:
        key = self._first(self._entity_keys, path)
        if key is None or isinstance(key, str):
            return key
        return key.get(platform)

    def option_keys(self, path: str) -> Mapping[str, str]:
        return self._first(self._option_keys, path) or {}

    def option_key(self, path: str, raw: str) -> str | None:
        return self.option_keys(path).get(raw)

    def is_mirror(self, path: str) -> bool:
        return self._first(self._mirrors, path) is not None

    def is_understood(self, path: str) -> bool:
        return self._first(self._understood, path) is not None

    def default_enabled(self, path: str) -> bool | None:
        return self._first(self._default_enabled, path)

    def category(self, path: str) -> Category | None:
        return self._first(self._categories, path)

    def icon(self, path: str) -> str | None:
        return self._first(self._icons, path)

    def in_dialog(self, path: str) -> bool:
        return self._first(self._dialog, path) is not None

    def zone_fields(self) -> tuple[str, ...]:
        return self._zone_fields

    def device_leaves(self, kind: str) -> tuple[str, ...]:
        return self._device_leaves.get(kind, ())

    def extra_roots(self) -> tuple[str, ...]:
        return self._roots

    def optional_children(self, path: str) -> tuple[str, ...]:
        leaves: list[str] = []
        for entry in self._children:
            if entry.pattern.fullmatch(path):
                leaves.extend(f"{path}/{leaf}" for leaf in entry.value)
        return tuple(dict.fromkeys(leaves))

    def diagnostics(self, resources: Mapping[str, Resource]) -> dict[str, object]:
        """S12/E04: each contract's value-free report, never confirmed."""
        report: dict[str, object] = {}
        for contract in self.contracts:
            features = (
                dict(contract.diagnostics(resources))
                if contract.diagnostics is not None
                else {}
            )
            report[contract.name] = {"confirmation": "unconfirmed", **features}
        return report

    def texts(self) -> tuple[tuple[str, str], ...]:
        """Every (German, English) pair the contracts build texts from."""
        return (
            *(entry.value for entry in self._names),
            *(text for contract in self.contracts for text in contract.texts),
        )


_registry = Registry(CONTRACTS)


def registry() -> Registry:
    """The registry of the installed contracts."""
    return _registry


def policy_for(
    resource: Resource, context: ControlContext | None
) -> EnumWritePolicy | NumberWritePolicy | None:
    return _registry.policy_for(resource, context)


def owns(policy: object) -> bool:
    return _registry.owns(policy)


def contract_for(path: str) -> str | None:
    return _registry.contract_for(path)


def blocked_reason(path: str) -> str | None:
    return _registry.blocked_reason(path)


def poll_group(path: str) -> PollGroup | None:
    return _registry.poll_group(path)


def name(path: str, language: str) -> str | None:
    return _registry.name(path, language)


def entity_key(path: str, platform: str) -> str | None:
    return _registry.entity_key(path, platform)


def option_keys(path: str) -> Mapping[str, str]:
    return _registry.option_keys(path)


def option_key(path: str, raw: str) -> str | None:
    return _registry.option_key(path, raw)


def is_mirror(path: str) -> bool:
    return _registry.is_mirror(path)


def is_understood(path: str) -> bool:
    return _registry.is_understood(path)


def default_enabled(path: str) -> bool | None:
    return _registry.default_enabled(path)


def category(path: str) -> Category | None:
    return _registry.category(path)


def icon(path: str) -> str | None:
    return _registry.icon(path)


def in_dialog(path: str) -> bool:
    return _registry.in_dialog(path)


def zone_fields() -> tuple[str, ...]:
    return _registry.zone_fields()


def device_leaves(kind: str) -> tuple[str, ...]:
    return _registry.device_leaves(kind)


def extra_roots() -> tuple[str, ...]:
    return _registry.extra_roots()


def optional_children(path: str) -> tuple[str, ...]:
    return _registry.optional_children(path)


def diagnostics(resources: Mapping[str, Resource]) -> dict[str, object]:
    return _registry.diagnostics(resources)


def texts() -> tuple[tuple[str, str], ...]:
    return _registry.texts()
