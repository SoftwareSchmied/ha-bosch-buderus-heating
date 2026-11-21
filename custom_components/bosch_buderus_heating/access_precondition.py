"""A gateway whose cloud data the cloud refuses with HTTP 428 (#146).

The cloud answers "precondition required" for every path with content when a
condition on the account or gateway side is not met, for example an incomplete
cloud pairing. Asking again every minute cannot change that, so the retries
grow: five minutes, then 15, 30 and from the fourth refusal on once an hour.
Any answer with data ends it, and a reload of the integration starts anew.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .item_errors import ItemErrorLog

if TYPE_CHECKING:
    from .pointt import Gateway

_DATA_KEY = "bosch_buderus_heating_gateway_access"
ISSUE_TRANSLATION_KEY = "gateway_precondition"

PRECONDITION_BACKOFF = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=30),
    timedelta(minutes=60),
)


class AccessPrecondition:
    """The refusals in a row of one coordinator and the pause that follows."""

    __slots__ = ("failures", "until")

    def __init__(self) -> None:
        self.failures = 0
        self.until = 0.0

    @property
    def active(self) -> bool:
        """Whether the last cycle with an answer ended in a refusal."""
        return self.failures > 0

    def refused(self, now: float) -> float:
        """Count one more refusal; return the seconds until the next request."""
        self.failures += 1
        pause = PRECONDITION_BACKOFF[
            min(self.failures, len(PRECONDITION_BACKOFF)) - 1
        ].total_seconds()
        self.until = now + pause
        return pause

    def remaining(self, now: float) -> float:
        """Seconds the pause still holds requests back."""
        return max(0.0, self.until - now) if self.failures else 0.0

    def cleared(self) -> bool:
        """Data arrived: end the state; whether there was one."""
        was_active = self.active
        self.failures = 0
        self.until = 0.0
        return was_active

    def diagnostics(self, now: float) -> dict[str, object]:
        return {
            "active": self.active,
            "refusals_in_a_row": self.failures,
            "next_request_in_seconds": round(self.remaining(now)),
        }


class GatewayAccess:
    """What one gateway's cloud access has shown, kept over source restarts.

    A managed source builds a new coordinator with every start attempt; the
    refusals, the failed bulk items and the password flags belong to the
    gateway, so they live here and not in the coordinator.
    """

    __slots__ = (
        "gateway_password_set",
        "item_errors",
        "precondition",
        "user_password_set",
    )

    def __init__(self) -> None:
        self.precondition = AccessPrecondition()
        self.item_errors = ItemErrorLog()
        self.user_password_set: bool | None = None
        self.gateway_password_set: bool | None = None

    def note_gateway(self, gateway: Gateway) -> None:
        """Keep what the gateway list says about the passwords (flags only)."""
        if gateway.user_password_set is not None:
            self.user_password_set = gateway.user_password_set
        if gateway.gateway_password_set is not None:
            self.gateway_password_set = gateway.gateway_password_set

    @property
    def has_content(self) -> bool:
        """Whether there is anything to report (views of other kinds have not)."""
        return bool(
            self.precondition.active
            or self.item_errors.counts
            or self.user_password_set is not None
            or self.gateway_password_set is not None
        )

    def diagnostics(self, now: float) -> dict[str, object]:
        return {
            "precondition": self.precondition.diagnostics(now),
            "bulk_item_errors": self.item_errors.diagnostics(),
            # Only whether the gateway list reports the fields as set.
            "gateway_list": {
                "user_password_set": self.user_password_set,
                "gateway_password_set": self.gateway_password_set,
            },
        }


def gateway_access(
    hass: HomeAssistant, entry_id: str, gateway_id: str
) -> GatewayAccess:
    """The shared access record of one gateway of one entry."""
    records: dict[tuple[str, str], GatewayAccess] = hass.data.setdefault(_DATA_KEY, {})
    return records.setdefault((entry_id, gateway_id), GatewayAccess())


def forget_entry_access(hass: HomeAssistant, entry_id: str) -> None:
    """An unloaded entry starts anew: its pauses, counts and notes end."""
    records: dict[tuple[str, str], GatewayAccess] = hass.data.get(_DATA_KEY, {})
    for key in [key for key in records if key[0] == entry_id]:
        del records[key]
    registry = ir.async_get(hass)
    for domain, issue_id in tuple(registry.issues):
        issue = registry.issues[(domain, issue_id)]
        if (
            domain == DOMAIN
            and issue.translation_key == ISSUE_TRANSLATION_KEY
            and (issue.data or {}).get("entry_id") == entry_id
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def access_diagnostics(
    hass: HomeAssistant, entry_id: str, now: float
) -> list[dict[str, object]]:
    """The records of an entry's gateways, numbered by their order here."""
    records: dict[tuple[str, str], GatewayAccess] = hass.data.get(_DATA_KEY, {})
    return [
        {"gateway": number, **record.diagnostics(now)}
        for number, (_, record) in enumerate(
            sorted(
                (
                    (key, value)
                    for key, value in records.items()
                    if key[0] == entry_id and value.has_content
                ),
                key=lambda item: item[0][1],
            ),
            1,
        )
    ]
