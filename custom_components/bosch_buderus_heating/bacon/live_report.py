"""Counters and state of the periodic active-client report."""

from __future__ import annotations

from dataclasses import dataclass, field

# Failure categories of the report grouped for diagnostics, never per request.
_CLASSES = {
    "rate_limited": "4xx",
    "live_rejected": "4xx",
    "live_unsupported": "4xx",
    "service_unavailable": "5xx",
    "connection": "network",
    "paused": "paused",
}
# The service rejects the report itself; stop (see LiveReport.new_connection).
DISABLING = frozenset({"live_unsupported", "live_rejected"})


@dataclass(slots=True)
class LiveReport:
    sent: int = 0
    errors: dict[str, int] = field(
        default_factory=lambda: {
            "4xx": 0,
            "5xx": 0,
            "network": 0,
            "paused": 0,
            "other": 0,
        }
    )
    # Switched off for the current connection after a 404 or 405, and for the
    # whole session once the service rejected the report itself (401 or 403).
    disabled: bool = False
    rejected: bool = False
    disabled_total: int = 0
    last_sent_at: float | None = None

    def failed(self, category: str) -> None:
        self.errors[_CLASSES.get(category, "other")] += 1
        if category in DISABLING:
            self.disabled = True
            self.disabled_total += 1
        if category == "live_rejected":
            self.rejected = True

    def new_connection(self) -> None:
        # A rejection is remembered: reconnecting must not retry it.
        self.disabled = self.rejected

    def diagnostics(self, now: float) -> dict[str, object]:
        return {
            "reports_sent": self.sent,
            "errors": dict(self.errors),
            "disabled_for_connection": self.disabled,
            "disabled_total": self.disabled_total,
            "last_report_age": age_bucket(
                None if self.last_sent_at is None else now - self.last_sent_at
            ),
        }


# Coarse age classes keep the report free of precise timings.
_BUCKETS = (
    (300, "under_5_min"),
    (900, "under_15_min"),
    (3600, "under_1_h"),
    (21600, "under_6_h"),
)


def age_bucket(age: float | None) -> str:
    """Name the age class; None means the event never happened."""
    if age is None:
        return "never"
    for limit, name in _BUCKETS:
        if age < limit:
            return name
    return "6_h_or_more"
