"""Pure K40/MX400 setup evidence; no discovery, requests or connection approval."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar

from .gateway_identity import (
    GatewayIdentity,
    GatewayNamespace,
    IdentityContext,
    IdentityError,
    IdentityProblem,
    parse_local_gateway_identity,
)
from .local_api.errors import LocalResourceError
from .local_api.json import JsonLimits, load_json

SERVICE_TYPE = "_hvac-open-api._tcp.local."
# Defensive input bounds, not vendor limits or polling settings.
MAX_BODY_BYTES = 256 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_VALUES = 8192
MAX_DISCOVERY_ADDRESSES = 16
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_NUMERIC_HOST = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*")
_SCOPE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class ProbeProblem(StrEnum):
    """Static codes suitable for later translation without private input text."""

    INVALID_ADDRESS = "invalid_address"
    SCOPE_REQUIRED = "interface_required"
    INVALID_DISCOVERY = "invalid_discovery"
    DISCOVERY_PORT = "unsupported_discovery_port"
    NO_ADDRESS = "no_usable_address"
    WRONG_SOURCE = "wrong_source"
    STALE_OBSERVATION = "stale_observation"
    DUPLICATE_RESPONSE = "duplicate_response"
    MISSING_RESPONSE = "missing_response"
    INVALID_RESPONSE = "invalid_response"
    INVALID_JSON = "invalid_json"
    BODY_TOO_LARGE = "response_too_large"
    WRONG_RESOURCE = "wrong_resource"
    UNKNOWN_BRAND = "unsupported_brand"
    UNKNOWN_FIRMWARE = "firmware_unverified"
    OLD_FIRMWARE = "firmware_update_required"
    INCOMPLETE_SYSTEM = "system_information_incomplete"
    INVALID_IDENTITY = "identity_invalid"
    AUTH_REJECTED = "token_rejected"
    FORBIDDEN = "resource_forbidden"
    NOT_FOUND = "required_resource_missing"
    RATE_LIMITED = "rate_limited"
    SERVICE_UNAVAILABLE = "service_unavailable"
    REDIRECT = "redirect_rejected"
    HTTP_ERROR = "http_error"
    DNS = "name_resolution_failed"
    CONNECT = "connection_failed"
    TIMEOUT = "request_timeout"
    TLS = "certificate_untrusted"
    TLS_CHANGED = "certificate_changed"


class LocalProbeError(ValueError):
    """Reject input without storing an address, response or credentials."""

    def __init__(self, problem: ProbeProblem) -> None:
        self.problem = problem
        super().__init__(problem.value)


def normalize_local_address(value: object) -> str:
    """Normalize a host field, without DNS, URL parsing or proof of LAN access.

    IPv6 scope names refer to the eventual HA host, not the computer entering
    the address. The transport must resolve/validate the interface there.
    """
    if not isinstance(value, str) or not 0 < len(value) <= 320:
        raise LocalProbeError(ProbeProblem.INVALID_ADDRESS)
    host = value.strip(" ")
    if not host or any(c.isspace() or not c.isprintable() for c in host):
        raise LocalProbeError(ProbeProblem.INVALID_ADDRESS)
    bracketed = host.startswith("[") and host.endswith("]")
    if bracketed:
        host = host[1:-1]
        if ":" not in host:
            raise LocalProbeError(ProbeProblem.INVALID_ADDRESS)
    base, separator, scope = host.partition("%")
    try:
        address = ipaddress.ip_address(base)
    except ValueError:
        if bracketed or separator:
            raise LocalProbeError(ProbeProblem.INVALID_ADDRESS) from None
        # ASCII DNS names avoid URL/IDNA reinterpretation. A trailing DNS root
        # dot is accepted, but ambiguous legacy numeric IPv4 forms are not.
        name = host.removesuffix(".").lower()
        if (
            len(name) > 253
            or _NUMERIC_HOST.fullmatch(name)
            or name == "localhost"
            or name.endswith(".localhost")
            or any(not _HOST_LABEL.fullmatch(label) for label in name.split("."))
        ):
            raise LocalProbeError(ProbeProblem.INVALID_ADDRESS) from None
        return name
    effective = (
        address.ipv4_mapped or address
        if isinstance(address, ipaddress.IPv6Address)
        else address
    )
    if (
        effective.is_unspecified
        or effective.is_loopback
        or effective.is_multicast
        or str(effective) == "255.255.255.255"
    ):
        raise LocalProbeError(ProbeProblem.INVALID_ADDRESS)
    if separator and (
        not isinstance(address, ipaddress.IPv6Address)
        or not address.is_link_local
        or not _SCOPE.fullmatch(scope)
        or (scope.isdecimal() and int(scope) == 0)
    ):
        raise LocalProbeError(ProbeProblem.INVALID_ADDRESS)
    if address.is_link_local and isinstance(address, ipaddress.IPv6Address):
        if not separator:
            raise LocalProbeError(ProbeProblem.SCOPE_REQUIRED)
        return f"{address.compressed}%{scope}"
    return address.compressed


@dataclass(frozen=True, slots=True)
class LocalEndpoint:
    """An untrusted candidate address with fixed API roles, not a device ID."""

    host: str = field(repr=False)
    auth_port: ClassVar[int] = 9442
    resource_port: ClassVar[int] = 9443

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", normalize_local_address(self.host))


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """Possible endpoints for explicit selection; never merges devices."""

    endpoints: tuple[LocalEndpoint, ...] = field(repr=False)
    rejected_addresses: int


def parse_discovery_candidate(
    *, service_type: object, hostname: object, addresses: object, port: object
) -> DiscoveryCandidate:
    """Consume a native discovery projection without trusting TXT identities.

    The adapter supplies strings and retains the platform's preferred order.
    Unusable addresses cannot hide other usable addresses from the same hint.
    The advertised port is checked but never changes either fixed API port.
    """
    if (
        not isinstance(service_type, str)
        or service_type.lower() != SERVICE_TYPE
        or not isinstance(addresses, (list, tuple))
        or len(addresses) > MAX_DISCOVERY_ADDRESSES
    ):
        raise LocalProbeError(ProbeProblem.INVALID_DISCOVERY)
    if type(port) is not int or port not in (9442, 9443):
        raise LocalProbeError(ProbeProblem.DISCOVERY_PORT)
    endpoints: list[LocalEndpoint] = []
    rejected = 0
    values = [*addresses]
    if hostname is not None:
        values.append(hostname)
    for value in values:
        try:
            host = normalize_local_address(value)
        except LocalProbeError:
            rejected += 1
            continue
        endpoint = LocalEndpoint(host)
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    if not endpoints:
        raise LocalProbeError(ProbeProblem.NO_ADDRESS)
    return DiscoveryCandidate(tuple(endpoints), rejected)


class ProbePath(StrEnum):
    BRAND = "/gateway/brand"
    FIRMWARE = "/gateway/versionFirmware"
    SYSTEM = "/system/basicInfo"


class TransportFailure(StrEnum):
    """The future transport maps exceptions to these codes, without raw text."""

    DNS = "name_resolution_failed"
    CONNECT = "connection_failed"
    TIMEOUT = "request_timeout"
    TLS = "certificate_untrusted"
    TLS_CHANGED = "certificate_changed"
    REDIRECT = "redirect_rejected"
    BODY_TOO_LARGE = "response_too_large"
    INVALID_RESPONSE = "invalid_response"
    INTERFACE = "interface_required"


@dataclass(frozen=True, slots=True)
class ProbeContext:
    """Captured before I/O; a changed endpoint or generation invalidates data.

    This is provenance, not proof of TLS trust. The future transport must check
    its own trust binding before sending credentials or producing responses.
    """

    identity: IdentityContext = field(repr=False)
    endpoint: LocalEndpoint = field(repr=False)

    def __post_init__(self) -> None:
        if self.identity.namespace != GatewayNamespace.K40_LOCAL:
            raise LocalProbeError(ProbeProblem.WRONG_SOURCE)


@dataclass(frozen=True, slots=True)
class ProbeResponse:
    """One bounded response or transport failure, without request credentials."""

    path: ProbePath
    context: ProbeContext = field(repr=False)
    status: int | None = None
    body: bytes = field(default=b"", repr=False)
    failure: TransportFailure | None = None


class GatewayBrand(StrEnum):
    BOSCH = "bosch"
    BUDERUS = "buderus"
    IVT = "ivt"
    VULCANO = "vulcano"
    WORCESTER = "worcester"


@dataclass(frozen=True, order=True, slots=True)
class FirmwareVersion:
    major: int
    minor: int
    patch: int


MINIMUM_FIRMWARE = FirmwareVersion(15, 0, 1)


def parse_firmware_version(value: object) -> FirmwareVersion:
    """Parse only a known dotted numeric version; never guess suffix semantics."""
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", value
    ):
        raise LocalProbeError(ProbeProblem.UNKNOWN_FIRMWARE)
    major, minor, patch = map(int, value.split("."))
    return FirmwareVersion(major, minor, patch)


def _payload(response: ProbeResponse) -> Mapping[str, object]:
    if not isinstance(response.body, bytes):
        raise LocalProbeError(ProbeProblem.INVALID_RESPONSE)
    if len(response.body) > MAX_BODY_BYTES:
        raise LocalProbeError(ProbeProblem.BODY_TOO_LARGE)
    try:
        payload = load_json(
            response.body,
            exact_numbers=False,
            limits=JsonLimits(MAX_BODY_BYTES, MAX_JSON_DEPTH, MAX_JSON_VALUES),
        )
    except LocalResourceError:
        raise LocalProbeError(ProbeProblem.INVALID_JSON) from None
    if not isinstance(payload, dict):
        raise LocalProbeError(ProbeProblem.INVALID_RESPONSE)
    if payload.get("id") != response.path.value:
        raise LocalProbeError(ProbeProblem.WRONG_RESOURCE)
    return payload


def _string_value(payload: Mapping[str, object]) -> str:
    value = payload.get("value")
    if (
        payload.get("type") != "stringValue"
        or type(payload.get("writeable")) is not int
        or payload["writeable"] != 0
        or not isinstance(value, str)
    ):
        raise LocalProbeError(ProbeProblem.INVALID_RESPONSE)
    return value


def _check_status(response: ProbeResponse) -> None:
    if response.failure is not None:
        if (
            not isinstance(response.failure, TransportFailure)
            or response.status is not None
            or response.body != b""
        ):
            raise LocalProbeError(ProbeProblem.INVALID_RESPONSE)
        raise LocalProbeError(ProbeProblem(response.failure.value))
    status = response.status
    if type(status) is not int or not 100 <= status <= 599:
        raise LocalProbeError(ProbeProblem.INVALID_RESPONSE)
    if status == 200:
        return
    problem = {
        401: ProbeProblem.AUTH_REJECTED,
        403: ProbeProblem.FORBIDDEN,
        404: ProbeProblem.NOT_FOUND,
        429: ProbeProblem.RATE_LIMITED,
    }.get(status)
    if problem is None:
        if 300 <= status < 400:
            problem = ProbeProblem.REDIRECT
        elif status >= 500:
            problem = ProbeProblem.SERVICE_UNAVAILABLE
        else:
            problem = ProbeProblem.HTTP_ERROR
    raise LocalProbeError(problem)


class ProbeStatus(StrEnum):
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    READY_FOR_IDENTITY_COMPARISON = "ready_for_identity_comparison"


@dataclass(frozen=True, slots=True)
class ProbeIssue:
    path: ProbePath | None
    problem: ProbeProblem


@dataclass(frozen=True, slots=True)
class ProbeAssessment:
    """Validated API evidence, never proof of pairing, trust or authorization.

    An identity is released only after every required response succeeds.
    Module count does not establish physical devices or supported functions.
    """

    status: ProbeStatus
    issues: tuple[ProbeIssue, ...] = ()
    brand: GatewayBrand | None = None
    firmware: FirmwareVersion | None = None
    identity: GatewayIdentity | None = field(default=None, repr=False)
    reported_module_count: int | None = None


def assess_local_probe(
    responses: Sequence[ProbeResponse], *, current_context: ProbeContext
) -> ProbeAssessment:
    """Assess collected observations without issuing requests or changing state.

    The caller must never retag old responses with a newer context. Missing
    responses remain incomplete; explicit failures take precedence. Even a
    successful assessment still requires the existing selected-target matcher.
    """
    by_path: dict[ProbePath, ProbeResponse] = {}
    for response in responses:
        problem: ProbeProblem | None = None
        if not isinstance(response.path, ProbePath):
            problem = ProbeProblem.INVALID_RESPONSE
        elif response.context != current_context:
            problem = ProbeProblem.STALE_OBSERVATION
        elif response.path in by_path:
            problem = ProbeProblem.DUPLICATE_RESPONSE
        if problem is not None:
            return ProbeAssessment(ProbeStatus.FAILED, (ProbeIssue(None, problem),))
        by_path[response.path] = response
    issues: list[ProbeIssue] = []
    brand: GatewayBrand | None = None
    firmware: FirmwareVersion | None = None
    identity: GatewayIdentity | None = None
    module_count: int | None = None
    for path in ProbePath:
        if path not in by_path:
            issues.append(ProbeIssue(path, ProbeProblem.MISSING_RESPONSE))
            continue
        response = by_path[path]
        try:
            _check_status(response)
            payload = _payload(response)
            if path == ProbePath.BRAND:
                value = _string_value(payload)
                try:
                    brand = GatewayBrand(value.lower())
                except ValueError:
                    raise LocalProbeError(ProbeProblem.UNKNOWN_BRAND) from None
            elif path == ProbePath.FIRMWARE:
                firmware = parse_firmware_version(_string_value(payload))
                if firmware < MINIMUM_FIRMWARE:
                    raise LocalProbeError(ProbeProblem.OLD_FIRMWARE)
            else:
                identity = parse_local_gateway_identity(
                    payload, context=current_context.identity
                )
                values = payload["values"]
                assert isinstance(values, list)  # Validated by identity parser.
                module_count = len(values)
        except LocalProbeError as error:
            issues.append(ProbeIssue(path, error.problem))
        except IdentityError as error:
            problem = (
                ProbeProblem.INCOMPLETE_SYSTEM
                if error.problem == IdentityProblem.INCOMPLETE_RESPONSE
                else ProbeProblem.INVALID_IDENTITY
            )
            issues.append(ProbeIssue(path, problem))
    if issues:
        status = (
            ProbeStatus.FAILED
            if any(issue.problem != ProbeProblem.MISSING_RESPONSE for issue in issues)
            else ProbeStatus.INCOMPLETE
        )
        return ProbeAssessment(status, tuple(issues), brand, firmware)
    return ProbeAssessment(
        ProbeStatus.READY_FOR_IDENTITY_COMPARISON,
        brand=brand,
        firmware=firmware,
        identity=identity,
        reported_module_count=module_count,
    )
