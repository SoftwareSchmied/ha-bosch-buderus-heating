"""The cloud hold of the coordinators of an air conditioner and a wallbox.

The heating coordinator keeps its own ``CloudHold`` (H-B3-2). The coordinators
of the other families of a PointT gateway follow the same rules through this
base class: a cycle that fails without an answer for it (no connection, a
timeout, a server error, a paused or limited cloud) is held for at most
``GRACE_CYCLES`` failed cycles in a row and ``GRACE_MAX_SECONDS`` since the
outage began, never after a rejected sign-in, a stopped source, an explicit
refusal (403, 404 and other client errors) or an unexpected error. Only the
display is held; a write still reads the setting again and is refused
without a current reading (``control_status`` stays strict).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cloud_hold import CloudHold


def explicit_refusal(status: object) -> bool:
    """A client error of the cloud (403, 404 ...) answers the request; it is
    no blip and never held. 408 and 429 are no refusal."""
    return (
        isinstance(status, int)
        and not isinstance(status, bool)
        and 400 <= status < 500
        and status not in (408, 429)
    )


class CloudHoldCoordinator[DataT](DataUpdateCoordinator[DataT]):
    """A coordinator whose entities show their last values over a blip."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cloud_hold = CloudHold()
        self._hold_displayed = False

    def _hold_blocked(self) -> bool:
        """Whether the source cannot be used at all (stopped or replaced)."""
        return False

    def cloud_holding(self) -> bool:
        """Whether entities show their last values over a failed cycle."""
        return (
            not self.last_update_success
            and self.cloud_hold.holding(monotonic())
            and not self._hold_blocked()
        )

    def hold_refuses(self) -> None:
        """Refuse an explicit answer for the cycle: nothing is held for it."""
        self.cloud_hold.cancel()

    async def hold_cycle[T](self, run: Callable[[], Awaitable[T]]) -> T:
        """Run one cycle and keep the count of failed ones.

        An ``UpdateFailed`` is a blip of the cloud; every other error (a
        rejected sign-in, a stopped source, an unexpected failure) is not.
        """
        try:
            result = await run()
        except UpdateFailed:
            self.cloud_hold.failed(monotonic())
            raise
        except Exception:
            self.cloud_hold.cancel()
            raise
        self.cloud_hold.recovered()
        return result

    def settle_hold(self) -> None:
        """After a cycle: tell the entities when the hold has just ended.

        Home Assistant skips the listeners after consecutive failures, so the
        entities would not learn that their last values are no longer shown.
        """
        if self.cloud_hold.holding(monotonic()) and not self._hold_blocked():
            self._hold_displayed = True
        elif self._hold_displayed:
            self._hold_displayed = False
            if self.cloud_hold.failures > 0:
                self.async_update_listeners()

    def hold_diagnostics(self) -> dict[str, object]:
        return self.cloud_hold.diagnostics(monotonic())
