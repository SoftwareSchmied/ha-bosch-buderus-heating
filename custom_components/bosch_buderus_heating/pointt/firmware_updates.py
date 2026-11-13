"""Read-only contract of the PointT firmware update list (F1).

``GET firmwareUpdates?gatewayId=<id>`` is no resource path. The answer is a
list with one entry per gateway: the installed version, an offered update and
the gateway's own automatic update setting. The update's ``state`` decides
what is shown: ``WAITING`` and ``DENIED`` offer an update and ``APPROVED``
means it is being installed, each only with a version other than the installed
one. ``NONE``, ``UNKNOWN``, no state or any other value keep the gateway
current; the state is then only shown as reported. The integration never
confirms an update or changes the automatic setting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .exceptions import InvalidPayload
from .models import JsonValue

# The known update states; any other value is shown, never interpreted
# (F1: "unknown state stays current"). ``NONE`` is neutral: it neither offers
# nor installs anything and is only shown as the reported state.
OFFERED_STATES: Final = frozenset({"WAITING", "DENIED"})
INSTALLING_STATES: Final = frozenset({"APPROVED"})
SHOWING_STATES: Final = OFFERED_STATES | INSTALLING_STATES
KNOWN_STATES: Final = SHOWING_STATES | {"NONE", "UNKNOWN"}
_MAX_ENTRIES = 32
_MAX_NOTES = 32
_MAX_VERSION_LENGTH = 64
_MAX_URL_LENGTH = 512
_STATE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?")
_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/[^\s\"'<>\\]*")


class FirmwareUpdatesSchemaError(InvalidPayload):
    """The answer does not have the shape of the firmware update list."""


@dataclass(frozen=True, slots=True)
class ReleaseNote:
    """One release notes link; only ``https`` links are kept."""

    language: str
    url: str


@dataclass(frozen=True, slots=True)
class FirmwareVersion:
    """``currentVersion`` or ``updateInformation`` of one gateway."""

    version: str | None
    state: str | None
    release_notes: tuple[ReleaseNote, ...] = ()


@dataclass(frozen=True, slots=True)
class FirmwareUpdates:
    """The firmware update entry of one gateway."""

    current: FirmwareVersion | None
    update: FirmwareVersion | None
    auto_update_enabled: bool | None
    auto_update_changeable: bool | None

    @property
    def update_state(self) -> str | None:
        """The reported update state as a stable token, ``None`` without one.

        A state outside the known set is returned as reported (if it is a
        plain token, else ``other``); it never counts as an offered update.
        """
        if self.update is None or self.update.state is None:
            return None
        return self.update.state.lower()

    @property
    def offered_version(self) -> str | None:
        """The version an update offers or installs; ``None`` when current.

        Only ``WAITING``, ``DENIED`` and ``APPROVED`` count, and only with a
        version other than the installed one. ``NONE``, an unknown state or
        an update without a version keep the installed version (nothing is
        guessed).
        """
        update = self.update
        if (
            update is None
            or update.state not in SHOWING_STATES
            or update.version is None
        ):
            return None
        current = self.current
        if current is not None and current.version == update.version:
            return None
        return update.version


def parse_firmware_updates(
    payload: JsonValue, gateway_id: str
) -> FirmwareUpdates | None:
    """The entry of ``gateway_id``; ``None`` when the list does not name it.

    Raises ``FirmwareUpdatesSchemaError`` for an unexpected shape: no list,
    an entry that is no object, a gateway listed twice or a field of the
    wrong type. Release notes links that are not plain ``https`` links are
    left out, not counted as a shape problem.
    """
    if payload is None:
        return None  # 204: nothing listed
    if not isinstance(payload, list) or len(payload) > _MAX_ENTRIES:
        raise FirmwareUpdatesSchemaError("Firmware update list is not a list")
    found: FirmwareUpdates | None = None
    for item in payload:
        if not isinstance(item, dict):
            raise FirmwareUpdatesSchemaError("Firmware update entry is no object")
        listed = item.get("gatewayId")
        if not isinstance(listed, str):
            raise FirmwareUpdatesSchemaError("Firmware update entry has no gateway")
        if listed.strip() != gateway_id.strip():
            continue
        if found is not None:
            raise FirmwareUpdatesSchemaError("Gateway is listed twice")
        enabled, changeable = _auto_update(item.get("autoUpdateInformation"))
        found = FirmwareUpdates(
            current=_version(item.get("currentVersion")),
            update=_version(item.get("updateInformation")),
            auto_update_enabled=enabled,
            auto_update_changeable=changeable,
        )
    if (
        found is not None
        and found.update is not None
        and found.update.state in SHOWING_STATES
        and found.update.version is None
    ):
        # An offered update without a version cannot be shown.
        raise FirmwareUpdatesSchemaError("Offered update has no version")
    return found


def _version(value: JsonValue) -> FirmwareVersion | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FirmwareUpdatesSchemaError("Firmware version is no object")
    version = value.get("version")
    if version is not None and not (
        isinstance(version, str)
        and 0 < len(version.strip()) <= _MAX_VERSION_LENGTH
        and version.isprintable()
    ):
        raise FirmwareUpdatesSchemaError("Firmware version is invalid")
    state = value.get("state")
    if state is not None and not isinstance(state, str):
        raise FirmwareUpdatesSchemaError("Firmware update state is invalid")
    return FirmwareVersion(
        version=version.strip() if isinstance(version, str) else None,
        state=_state_token(state) if isinstance(state, str) else None,
        release_notes=_release_notes(value.get("releaseNotes")),
    )


def _state_token(value: str) -> str:
    """A known state as reported (upper case), another plain token kept
    as it is, anything else ``other``."""
    if value.upper() in KNOWN_STATES:
        return value.upper()
    return value if _STATE.fullmatch(value) else "other"


def _release_notes(value: JsonValue) -> tuple[ReleaseNote, ...]:
    if not isinstance(value, list):
        return ()
    notes: list[ReleaseNote] = []
    for item in value[:_MAX_NOTES]:
        if not isinstance(item, dict):
            continue
        language, url = item.get("language"), item.get("url")
        if (
            isinstance(language, str)
            and _LANGUAGE.fullmatch(language)
            and isinstance(url, str)
            and len(url) <= _MAX_URL_LENGTH
            and _URL.fullmatch(url)
        ):
            notes.append(ReleaseNote(language.replace("_", "-").lower(), url))
    return tuple(notes)


def _auto_update(value: JsonValue) -> tuple[bool | None, bool | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise FirmwareUpdatesSchemaError("Automatic update setting is no object")
    enabled, changeable = value.get("enabled"), value.get("changeable")
    return (
        enabled if isinstance(enabled, bool) else None,
        changeable if isinstance(changeable, bool) else None,
    )


def release_url(updates: FirmwareUpdates, language: str | None) -> str | None:
    """The release notes link of an offered update, else of the installed
    version: in ``language``, else English, else the first link."""
    wanted = (language or "").replace("_", "-").lower()
    primary = wanted.split("-", 1)[0]
    offered = updates.update if updates.offered_version is not None else None
    for version in (offered, updates.current):
        if version is None or not version.release_notes:
            continue
        notes = version.release_notes
        for candidate in (
            next((note for note in notes if note.language == wanted), None),
            next((note for note in notes if _primary(note) == primary), None),
            next((note for note in notes if _primary(note) == "en"), None),
            notes[0],
        ):
            if candidate is not None:
                return candidate.url
    return None


def _primary(note: ReleaseNote) -> str:
    return note.language.split("-", 1)[0]


# The stored entry carries no gateway ID; it is read back under this name.
_STORED_GATEWAY: Final = "stored"


def stored_entry(updates: FirmwareUpdates) -> dict[str, JsonValue]:
    """``updates`` in the shape of a list entry, without the gateway ID.

    It keeps the last answer across a restart; ``restored_updates`` reads it
    back with the same checks as an answer of the cloud.
    """
    entry: dict[str, JsonValue] = {}
    if updates.current is not None:
        entry["currentVersion"] = _stored_version(updates.current)
    if updates.update is not None:
        entry["updateInformation"] = _stored_version(updates.update)
    if (
        updates.auto_update_enabled is not None
        or updates.auto_update_changeable is not None
    ):
        entry["autoUpdateInformation"] = {
            "enabled": updates.auto_update_enabled,
            "changeable": updates.auto_update_changeable,
        }
    return entry


def _stored_version(version: FirmwareVersion) -> dict[str, JsonValue]:
    return {
        "version": version.version,
        "state": version.state,
        "releaseNotes": [
            {"language": note.language, "url": note.url}
            for note in version.release_notes
        ],
    }


def restored_updates(value: object) -> FirmwareUpdates | None:
    """A stored entry read back; ``None`` if it is missing or not valid."""
    if not isinstance(value, dict) or "gatewayId" in value:
        return None
    try:
        return parse_firmware_updates(
            [{**value, "gatewayId": _STORED_GATEWAY}], _STORED_GATEWAY
        )
    except InvalidPayload:
        return None
