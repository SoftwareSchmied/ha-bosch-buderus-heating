"""Scoped local completion evidence, without cloud secrets or replayed writes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from .configuration import (
    LOCAL_KIND,
    ConfigurationError,
    ConfigurationSnapshot,
    require_identifier,
)
from .gateway_identity import GatewayNamespace, NamespacePair


class RecoveryStatus(StrEnum):
    READY = "ready"
    APPLIED = "applied"
    CONFLICT = "conflict"
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"


def local_state_fingerprint(
    snapshot: ConfigurationSnapshot,
    target_id: str,
    connection_id: str,
    binding_id: str,
) -> str | None:
    """Hash only the selected access and relationships, never the whole owner."""
    connection = snapshot.connections.get(connection_id)
    binding = snapshot.bindings.get(binding_id)
    target = snapshot.targets.get(target_id)
    if (
        connection is None
        or connection.kind != LOCAL_KIND
        or target is None
        or binding is None
        or binding.connection_id != connection_id
        or binding.target_id != target_id
        or binding.adapter != LOCAL_KIND
    ):
        return None
    options = snapshot.to_options()
    selected = [
        snapshot.owner_id,
        snapshot.version,
        snapshot.minor_version,
        connection_id,
        connection.to_data(),
        cast(dict[str, object], options["connections"])[connection_id],
        binding_id,
        binding.to_data(),
        cast(dict[str, object], options["bindings"])[binding_id],
        target_id,
        target.to_data(),
        cast(dict[str, object], options["targets"])[target_id],
    ]
    return hashlib.sha256(
        json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CompletionIntent:
    """Persisted before HA acceptance; existence alone proves no completed write."""

    connection_id: str = field(repr=False)
    binding_id: str = field(repr=False)
    expected_sha256: str = field(repr=False)
    process_id: str = field(repr=False)

    def __post_init__(self) -> None:
        require_identifier(self.connection_id)
        require_identifier(self.binding_id)
        require_identifier(self.process_id)
        if (
            not isinstance(self.expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.expected_sha256) is None
        ):
            raise ConfigurationError("invalid_completion_intent")

    def matches(self, snapshot: ConfigurationSnapshot, target_id: str) -> bool:
        return self.expected_sha256 == local_state_fingerprint(
            snapshot,
            target_id,
            self.connection_id,
            self.binding_id,
        )

    def to_data(self) -> dict[str, str]:
        return {
            "connection_id": self.connection_id,
            "binding_id": self.binding_id,
            "expected_sha256": self.expected_sha256,
            "process_id": self.process_id,
        }

    @classmethod
    def from_data(cls, value: object) -> CompletionIntent:
        if not isinstance(value, dict) or set(value) != {
            "connection_id",
            "binding_id",
            "expected_sha256",
            "process_id",
        }:
            raise ConfigurationError("invalid_completion_intent")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ScopedIdentityContract:
    """Caller-verified source evidence scoped to exactly one target and gateway.

    Construction is not evidence acquisition. A trusted identity policy supplies
    this record after validating source semantics for the selected device. The
    setup UI must never construct it from a user-entered contract name alone.
    """

    owner_id: str = field(repr=False)
    target_id: str = field(repr=False)
    gateway_id: str = field(repr=False)
    contract_id: str

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.target_id, self.gateway_id, self.contract_id):
            require_identifier(value)

    def verified_pairs(
        self,
        snapshot: ConfigurationSnapshot,
        target_id: str,
        gateway_id: str,
    ) -> tuple[NamespacePair, ...]:
        if self.contract_id == LOCAL_FIRST:
            return self._local_first(snapshot, target_id, gateway_id)
        target = snapshot.targets.get(target_id)
        if (
            snapshot.owner_id != self.owner_id
            or target_id != self.target_id
            or gateway_id != self.gateway_id
            or target is None
        ):
            raise ConfigurationError("identity_contract_scope_changed")
        identities = cast(list[dict[str, object]], target.to_data()["identities"])
        pointt = [
            i["value"] for i in identities if i["namespace"] == GatewayNamespace.POINTT
        ]
        if pointt != [self.gateway_id]:
            raise ConfigurationError("identity_contract_scope_changed")
        return ((GatewayNamespace.K40_LOCAL, GatewayNamespace.POINTT),)

    def _local_first(
        self, snapshot: ConfigurationSnapshot, target_id: str, gateway_id: str
    ) -> tuple[NamespacePair, ...]:
        """A plant set up from its local access alone (P49b step 7): no
        source is compared; only its own new or local-only target fits."""
        target = snapshot.targets.get(target_id)
        identities = (
            cast(list[dict[str, object]], target.to_data()["identities"])
            if target is not None
            else []
        )
        if (
            snapshot.owner_id != self.owner_id
            or target_id != self.target_id
            or gateway_id != self.gateway_id
            or (
                target is not None
                and (
                    target.kind != "local_installation"
                    or [
                        i["value"]
                        for i in identities
                        if i["namespace"] == GatewayNamespace.K40_LOCAL
                    ]
                    != [gateway_id]
                )
            )
        ):
            raise ConfigurationError("identity_contract_scope_changed")
        return ()


# The contract of a plant set up from its local access alone (P49b step 7).
LOCAL_FIRST = "local_first"
