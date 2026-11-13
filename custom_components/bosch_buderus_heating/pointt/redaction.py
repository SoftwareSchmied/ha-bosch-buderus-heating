"""Path templates and identifier hashing for diagnostics and logs.

Diagnostics export only allowlisted fields. There is deliberately no generic
helper that tries to remove secrets from arbitrary data or free text: such a
denylist can never be complete.
"""

from __future__ import annotations

import hashlib
import hmac
import re


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
