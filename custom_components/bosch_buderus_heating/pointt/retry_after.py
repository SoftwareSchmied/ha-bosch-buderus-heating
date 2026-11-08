"""Shared interpretation of server-requested cloud pauses."""

from __future__ import annotations

import math
from datetime import UTC
from email.utils import parsedate_to_datetime

# A server pause is honored in full up to this bound. The bound only protects
# against nonsensical header values; it is not a client-side shortening.
MAX_CLOUD_PAUSE_SECONDS = 86400.0
# Used when a rate limit carries no usable Retry-After value.
DEFAULT_CLOUD_PAUSE_SECONDS = 300.0
# Never resume earlier than this, even if the server asks for less.
MIN_CLOUD_PAUSE_SECONDS = 60.0


def parse_retry_after(value: str | None, *, now: float) -> float | None:
    """Return the requested delay in seconds from delta-seconds or an HTTP date.

    ``now`` is the current wall-clock time used for HTTP dates. Invalid,
    infinite or missing values return ``None`` so the caller applies the
    documented default. Past dates and negative values become zero.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        delay = float(text)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(text)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            delay = retry_at.timestamp() - now
        except TypeError, ValueError, OverflowError:
            return None
    if not math.isfinite(delay):
        return None
    return max(0.0, delay)


def bounded_cloud_pause(retry_after: float | None) -> float:
    """Apply the default, minimum and safety bound to a requested pause."""
    if retry_after is None or not math.isfinite(retry_after):
        return DEFAULT_CLOUD_PAUSE_SECONDS
    return min(MAX_CLOUD_PAUSE_SECONDS, max(MIN_CLOUD_PAUSE_SECONDS, retry_after))
