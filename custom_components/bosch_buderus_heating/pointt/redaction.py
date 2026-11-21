"""Path templates and identifier hashing for diagnostics and logs.

Diagnostics export only allowlisted fields. There is deliberately no generic
helper that tries to remove secrets from arbitrary data or free text: such a
denylist can never be complete.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping


def resource_path_template(path: str) -> str:
    """Remove installation-specific logical IDs from a PointT path.

    Logs use this template. It first hides every identifier the diagnostics
    hide (D31): before, a zone, heat source, solar circuit or ventilation
    child outside the canonical names stayed visible in the log only.
    """
    return _logical_template(diagnostic_resource_path(path))


def _logical_template(path: str) -> str:
    """Replace the canonical logical IDs by their placeholders."""
    path = re.sub(r"^/zones/zn[0-9]+(?=/|$)", "/zones/{zn}", path)
    path = re.sub(r"^/programs/(?!list(?:/|$))[^/]+", "/programs/{pg}", path)
    path = re.sub(r"^/system/holidayModes/[^/]+", "/system/holidayModes/{hm}", path)
    for root, placeholder in (
        ("heatingCircuits", "{hc}"),
        ("dhwCircuits", "{dhw}"),
        ("swCircuits", "{sw}"),
    ):
        path = re.sub(rf"^/{root}/[^/]+", f"/{root}/{placeholder}", path)
    path = re.sub(
        r"^/heatSources/hs\d+(?=/|$)",
        "/heatSources/{hs}",
        path,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"^/devices/[^/]+(?=/|$)",
        "/devices/{device}",
        path,
        flags=re.IGNORECASE,
    )


# These roots mix logical circuits with static resources. Unknown children must
# not become trusted static names merely because they lack a circuit prefix.
_DIAGNOSTIC_STATIC_CHILDREN: dict[str, frozenset[str]] = {
    "heatingCircuits": frozenset({"list"}),
    "dhwCircuits": frozenset({"list", "watertotalconsumption"}),
    "heatSources": frozenset(
        {
            "actualheatdemand",
            "actualmodulation",
            "actualsupplytemperature",
            "additionalheater",
            "chstatus",
            "compressor",
            "currentemergencymode",
            "electricitytotalconsumption",
            "emon",
            "emstatus",
            "flamestatus",
            "gastotalconsumption",
            "hybrid",
            "info",
            "list",
            "numberofrefrigerantcircuitsinstalled",
            "numberofstarts",
            "overallstatus",
            "passivecooling",
            "poolsetpointtemperature",
            "poolstatus",
            "pooltemperature",
            "pvcontactstate",
            "returntemperature",
            "smartfunction",
            "source",
            "standbymode",
            "systempressure",
            "systempressurerange",
            "type",
            "workingtime",
        }
    ),
    "solarCircuits": frozenset({"list"}),
    "ventilation": frozenset({"operationmodes"}),
    "zones": frozenset({"configuration", "list"}),
    "programs": frozenset({"list"}),
    "swCircuits": frozenset({"list"}),
}


def diagnostic_resource_path(path: str) -> str:
    """Keep useful logical paths while hiding unrecognized dynamic identifiers."""
    if match := re.match(r"^/system/holidayModes/([^/]+)(?=/|$)", path):
        if re.fullmatch(r"hm[1-9][0-9]{0,2}", match.group(1)) is None:
            return re.sub(
                r"^/system/holidayModes/[^/]+", "/system/holidayModes/{hm}", path
            )
        return path
    for root, canonical, placeholder in (
        ("heatingCircuits", r"hc\d+", "{hc}"),
        ("dhwCircuits", r"dhw\d+", "{dhw}"),
        ("heatSources", r"hs\d+", "{hs}"),
        ("solarCircuits", r"sc\d+", "{solar}"),
        ("ventilation", r"zone\d+", "{zone}"),
        ("zones", r"(?:zone\d+|zn(?:0|[1-9][0-9]{0,3}))", "{zone}"),
        ("programs", r"pg(?:0|[1-9][0-9]{0,3})", "{pg}"),
        ("swCircuits", r"sw\d+", "{sw}"),
    ):
        match = re.match(rf"^/{root}/([^/]+)(?=/|$)", path, re.IGNORECASE)
        if match is None:
            continue
        identifier = match.group(1)
        if identifier.casefold() in _DIAGNOSTIC_STATIC_CHILDREN[root]:
            return path
        if re.fullmatch(canonical, identifier, re.IGNORECASE):
            return path
        return re.sub(
            rf"^/{root}/[^/]+",
            f"/{root}/{placeholder}",
            path,
            count=1,
            flags=re.IGNORECASE,
        )
    return re.sub(
        r"^/devices/[^/]+(?=/|$)",
        "/devices/{device}",
        path,
        flags=re.IGNORECASE,
    )


def anonymize_identifier(value: object, *, salt: bytes) -> str:
    """Return an installation-specific, non-reversible short identifier."""
    digest = hmac.new(salt, str(value).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"id:{digest[:12]}"


# The text fields a failed bulk item may carry. Only these are read, and only
# in the reduced form below; there is no generic clean-up of arbitrary data.
ERROR_DETAIL_FIELDS = ("code", "description", "details", "errorId", "message")
_ERROR_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_ERROR_NESTED = ("error", "serverResponse", "response", "gatewayResponse")
_TEXT_LIMIT = 120
_TEXT_DISALLOWED = re.compile(r"[^A-Za-z0-9 .,:;()'_-]")
_TEXT_EMAIL = re.compile(r"\S*@\S*")
_TEXT_ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# A word with a digit and six or more characters could be a serial number, a
# gateway ID, a MAC address part or a token; a long run of any kind as well.
_TEXT_IDENTIFIER = re.compile(
    r"(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{6,}|[A-Za-z0-9_\-+/=.]{24,}"
)


def safe_error_text(value: object) -> str | None:
    """A short plain-words form of a free-text error field, or None.

    Anything that could identify an account, a gateway or a device (e-mail
    addresses, network addresses, identifiers with digits, long opaque runs,
    all other characters) is replaced, the text is cut at 120 characters.
    """
    if not isinstance(value, str):
        return None
    text = _TEXT_EMAIL.sub(" [redacted] ", value[:400])
    text = _TEXT_ADDRESS.sub(" [redacted] ", text)
    text = _TEXT_IDENTIFIER.sub(" [redacted] ", text)
    text = _TEXT_DISALLOWED.sub(" ", text)
    text = " ".join(text.split())[:_TEXT_LIMIT].strip()
    return text or None


def _safe_detail(value: object, depth: int = 0) -> object:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if abs(value) < 10**6 else "other"
    if isinstance(value, str):
        return safe_error_text(value)
    if depth >= 2:
        return None
    if isinstance(value, Mapping):
        return {
            key: reduced
            for key, item in list(value.items())[:5]
            if isinstance(key, str)
            and _ERROR_TOKEN.fullmatch(key)
            and (reduced := _safe_detail(item, depth + 1)) is not None
        } or None
    if isinstance(value, list | tuple):
        return [
            reduced
            for item in value[:5]
            if (reduced := _safe_detail(item, depth + 1)) is not None
        ] or None
    return None


def safe_error_details(item: object) -> dict[str, object] | None:
    """The allowlisted error fields of a failed bulk item, reduced and bounded.

    ``code`` and ``errorId`` are kept only as short plain tokens (else
    ``other``); ``message``, ``description`` and ``details`` as redacted
    text. The fields come from the item itself or from one error object in it.
    """
    if not isinstance(item, Mapping):
        return None
    sources = [item]
    sources.extend(
        nested for key in _ERROR_NESTED if isinstance(nested := item.get(key), Mapping)
    )
    details: dict[str, object] = {}
    for source in sources:
        for name in ERROR_DETAIL_FIELDS:
            if name in details or name not in source:
                continue
            raw = source[name]
            if name in ("code", "errorId"):
                if isinstance(raw, bool) or raw is None:
                    continue
                token = str(raw) if isinstance(raw, int | str) else None
                if token is None:
                    continue
                details[name] = token if _ERROR_TOKEN.fullmatch(token) else "other"
            elif (reduced := _safe_detail(raw)) is not None:
                details[name] = reduced
    return details or None
