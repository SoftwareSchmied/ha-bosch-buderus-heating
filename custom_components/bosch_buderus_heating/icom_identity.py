"""Recognise an iCom gateway by comparing values when its own ID is unusable.

The normal proof is the gateway's own ID (/gateway/uuid) equal to the cloud's
gateway ID. Some gateways report no usable ID there. Then the readings both
sides deliver for the same paths are compared instead: the local reading just
made against the cloud's last reading. Static values (versions, type) must be
equal; live values must agree within a tolerance and the cloud reading must be
recent. At least two readings must agree, at least one of them live, and none
may clearly contradict. Which readings matched is never stored or shown, only
how many.

Pure logic: no Home Assistant import, no clock, no request.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

FIRMWARE_PATH: Final = "/gateway/versionFirmware"
HARDWARE_PATH: Final = "/gateway/versionHardware"
BRAND_PATH: Final = "/gateway/brand"
SYSTEM_TYPE_PATH: Final = "/system/type"
OUTDOOR_PATH: Final = "/system/sensors/temperatures/outdoor_t1"
PRESSURE_PATH: Final = "/heatSources/systemPressure"
STARTS_PATH: Final = "/heatSources/numberOfStarts"

STATIC_PATHS: Final = (HARDWARE_PATH, FIRMWARE_PATH, BRAND_PATH, SYSTEM_TYPE_PATH)
# Live values: (agreement up to, clear contradiction beyond).
LIVE_LIMITS: Final[Mapping[str, tuple[float, float]]] = {
    OUTDOOR_PATH: (2.0, 5.0),
    PRESSURE_PATH: (0.3, 1.0),
}
# A counter never falls; it may have risen a little since the cloud's reading.
STARTS_AGREE: Final = 3
STARTS_CONTRADICT: Final = 50
FALLBACK_PATHS: Final = (*STATIC_PATHS, *LIVE_LIMITS, STARTS_PATH)
# The reading the stored fingerprint is made of: the one that stays the same
# through a firmware update comes first.
FINGERPRINT_PATHS: Final = (HARDWARE_PATH, FIRMWARE_PATH)
# A cloud reading older than this says nothing about the gateway right now.
MAX_CLOUD_AGE: Final = 900.0
MIN_MATCHES: Final = 2
MAX_TEXT_LENGTH: Final = 200


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    CONTRADICTION = "contradiction"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class CloudReading:
    """The cloud's last reading of a path and its age in seconds."""

    value: object
    age: float | None


@dataclass(frozen=True, slots=True)
class Outcome:
    verdict: Verdict
    matches: int = 0


def text_of(value: object) -> str | None:
    """A version or type text in the form used to compare, None when unusable."""
    if not isinstance(value, str) or len(value) > MAX_TEXT_LENGTH:
        return None
    text = " ".join(value.split()).casefold()
    return text or None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _recent(reading: CloudReading) -> bool:
    return reading.age is not None and -60.0 <= reading.age <= MAX_CLOUD_AGE


def compare(local: Mapping[str, object], cloud: Mapping[str, CloudReading]) -> Outcome:
    """Compare the local readings with the cloud's for the same paths."""
    matches = live = 0
    for path in STATIC_PATHS:
        mine, theirs = text_of(local.get(path)), cloud.get(path)
        other = text_of(theirs.value) if theirs is not None else None
        if mine is None or other is None:
            continue
        if mine != other:
            return Outcome(Verdict.CONTRADICTION)
        matches += 1
    for path, (agree, contradict) in LIVE_LIMITS.items():
        mine_number = _number(local.get(path))
        reading = cloud.get(path)
        other_number = _number(reading.value) if reading is not None else None
        if (
            mine_number is None
            or reading is None
            or other_number is None
            or not _recent(reading)
        ):
            continue
        difference = abs(mine_number - other_number)
        if difference > contradict:
            return Outcome(Verdict.CONTRADICTION)
        if difference <= agree:
            matches += 1
            live += 1
    mine_starts = _number(local.get(STARTS_PATH))
    starts = cloud.get(STARTS_PATH)
    other_starts = _number(starts.value) if starts is not None else None
    if (
        mine_starts is not None
        and starts is not None
        and other_starts is not None
        and _recent(starts)
    ):
        rise = mine_starts - other_starts
        if rise < 0 or rise > STARTS_CONTRADICT:
            return Outcome(Verdict.CONTRADICTION)
        if rise <= STARTS_AGREE:
            matches += 1
            live += 1
    if matches >= MIN_MATCHES and live >= 1:
        return Outcome(Verdict.CONFIRMED, matches)
    return Outcome(Verdict.INSUFFICIENT, matches)


def fingerprint(local: Mapping[str, object]) -> tuple[str, str] | None:
    """The reading that later tells the same gateway: (path, text), or None."""
    for path in FINGERPRINT_PATHS:
        text = text_of(local.get(path))
        if text is not None:
            return path, text
    return None
