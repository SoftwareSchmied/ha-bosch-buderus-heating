"""A binary sensor that tells whether a gateway answers (X15).

For automations that should alarm when a gateway hangs, without relying on
other entities turning ``unavailable``. It reports each access of the gateway
on its own and combines them:

* Cloud: ``on`` when the last cloud refresh got readings of the gateway;
  ``off`` when the cloud did not answer usably (a gateway or PointT error,
  no answer, or the protection pause that follows repeated errors). A failed
  sign-in, a sign-in that must be renewed, a rate limit pause and a stopped
  cloud access say nothing about the gateway: unknown, not ``off``.
* Local: ``on`` when the local access is ready; ``off`` while it is paused
  or unavailable; unknown while it starts, needs a new token or must confirm a
  changed certificate.
* Together: ``on`` when any access answers, else ``off`` when any access does
  not, else unknown.

The sensor always has a state itself (unknown rather than ``unavailable``) and
sends no request. It shows the strict state of the last cycle: a short cloud
blip held for the display of other entities (the grace of a few cycles) turns
it ``off`` at once, so an automation should wait with ``for:``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .device import device_info_for_resource
from .local_state import LocalCondition

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceInfo

    from .coordinator import BoschBuderusDataUpdateCoordinator
    from .local_selection import LocalSelection

# What an access reports; fixed codes, translated as attribute states.
REACHABLE: Final = "reachable"
NOT_ANSWERING: Final = "not_answering"
SIGN_IN_UNAVAILABLE: Final = "sign_in_unavailable"
SIGN_IN_REQUIRED: Final = "sign_in_required"
PAUSED: Final = "paused"
NOT_IN_USE: Final = "not_in_use"
READY: Final = "ready"
NOT_READY: Final = "not_ready"
STARTING: Final = "starting"
CERTIFICATE_CHANGED: Final = "certificate_changed"

# The protection pause and a failed cycle both follow errors of the cloud
# service; which of them it was comes from the category of the failed refresh.
_CLOUD_UNKNOWN: Final = {
    "auth_required": SIGN_IN_REQUIRED,
    "cloud_stopped": NOT_IN_USE,
    "rate_limited": PAUSED,
}
_LOCAL_ON: Final = frozenset({LocalCondition.READY})
_LOCAL_OFF: Final = frozenset({LocalCondition.PAUSED, LocalCondition.UNAVAILABLE})
_LOCAL_CODES: Final = {
    LocalCondition.READY: READY,
    LocalCondition.PAUSED: PAUSED,
    LocalCondition.UNAVAILABLE: NOT_READY,
    LocalCondition.INITIALIZING: STARTING,
    LocalCondition.AUTH_REQUIRED: SIGN_IN_REQUIRED,
    LocalCondition.CERTIFICATE_CHANGED: CERTIFICATE_CHANGED,
}
# Every category a failed cycle can report (``_pointt_result``,
# ``_result_category`` and the rate limit of the coordinator); each has a
# translated state, anything else shows as unknown.
CLOUD_FAILURE_CATEGORIES: Final = (
    "timeout",
    "service_unavailable",
    "request_failed",
    "sign_in_unavailable",
    "forbidden",
    "not_found",
    "rate_limited",
    "authentication_error",
    "unknown",
)
# The category of a failed refresh that is no fault of the gateway.
_NOT_THE_GATEWAY: Final = frozenset({SIGN_IN_UNAVAILABLE})


def cloud_access(reason: str | None, failure: str | None) -> tuple[bool | None, str]:
    """(answers, code) of the cloud access.

    ``reason`` is why the cloud cannot be used now (None: it can), ``failure``
    the category of the last failed refresh.
    """
    if reason is None:
        return True, REACHABLE
    if reason in _CLOUD_UNKNOWN:
        return None, _CLOUD_UNKNOWN[reason]
    if failure in _NOT_THE_GATEWAY:
        return None, str(failure)
    return False, NOT_ANSWERING


def local_access(
    states: tuple[object, ...] | None,
) -> tuple[bool | None, str]:
    """(answers, code) of the local access; ``None`` states: no local access."""
    if not states:
        return None, NOT_IN_USE
    conditions = [getattr(state, "condition", None) for state in states]
    if any(item in _LOCAL_ON for item in conditions):
        return True, READY
    if any(item in _LOCAL_OFF for item in conditions):
        paused = all(item is LocalCondition.PAUSED for item in conditions)
        return False, PAUSED if paused else NOT_READY
    for item in conditions:
        if item in _LOCAL_CODES:
            return None, _LOCAL_CODES[item]
    return None, STARTING


def combine(cloud: bool | None, local: bool | None) -> bool | None:
    """Any access that answers: on; otherwise any that does not: off."""
    if cloud is True or local is True:
        return True
    if cloud is False or local is False:
        return False
    return None


class GatewayReachableBinarySensor(
    CoordinatorEntity["BoschBuderusDataUpdateCoordinator"], BinarySensorEntity
):
    """Whether the gateway answers, per access and together."""

    _attr_has_entity_name = True
    _attr_translation_key = "gateway_reachable"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BoschBuderusDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.gateway.gateway_id}:gateway_reachable"
        self._state: tuple[bool | None, dict[str, Any]] = (None, {})
        self._selection: LocalSelection | None = None
        self._unsubscribe: Callable[[], None] | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for_resource(self.coordinator, "/gateway")

    @property
    def available(self) -> bool:
        # The state itself says "unknown" when no access can tell.
        return True

    @property
    def is_on(self) -> bool | None:
        return self._state[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._state[1]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._evaluate()

    async def async_will_remove_from_hass(self) -> None:
        self._stop_listening()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._evaluate()
        super()._handle_coordinator_update()

    @callback
    def _stop_listening(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = None
        self._selection = None

    @callback
    def _listen(self) -> None:
        """Follow the state of the local access, which the coordinator update
        alone does not announce (a pause with unchanged readings)."""
        selection = self.coordinator.local_selection
        if selection is self._selection:
            return
        self._stop_listening()
        if selection is not None:
            self._selection = selection
            self._unsubscribe = selection.subscribe_states(self._local_changed)

    @callback
    def _local_changed(self) -> None:
        before = self._state
        self._evaluate()
        if self.hass is not None and before != self._state:
            self.async_write_ha_state()

    @callback
    def _evaluate(self) -> None:
        self._listen()
        coordinator = self.coordinator
        report = coordinator.last_failed_refresh.diagnostics()
        failure = (
            report.get("category")
            if isinstance(report, Mapping) and report.get("cycle_failed")
            else None
        )
        reason = coordinator.cloud_down_reason()
        if reason is not None and coordinator.sign_in_rejected:
            # A rejected sign-in says nothing about the gateway.
            reason = "auth_required"
        cloud, cloud_code = cloud_access(
            reason,
            failure if isinstance(failure, str) else None,
        )
        selection = coordinator.local_selection
        local, local_code = local_access(
            selection.local_states() if selection is not None else None
        )
        attributes: dict[str, Any] = {"cloud": cloud_code, "local": local_code}
        if cloud_code in (NOT_ANSWERING, SIGN_IN_UNAVAILABLE) and isinstance(
            failure, str
        ):
            attributes["cloud_failure"] = (
                failure if failure in CLOUD_FAILURE_CATEGORIES else "unknown"
            )
        self._state = (combine(cloud, local), attributes)
