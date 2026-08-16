"""Repair flows for Bosch/Buderus Heating."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_POLLING_PROFILE,
    DOMAIN,
    FIRMWARE_ISSUE_PREFIX,
    RATE_LIMIT_ISSUE_PREFIX,
    PollingProfile,
)


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a repair flow for a supported issue."""
    entry_id = data.get("entry_id") if data else None
    if not isinstance(entry_id, str):
        return InvalidRepairFlow()
    if issue_id.startswith(FIRMWARE_ISSUE_PREFIX):
        return FirmwareCompatibilityRepairFlow(entry_id, issue_id)
    if not issue_id.startswith(RATE_LIMIT_ISSUE_PREFIX):
        return InvalidRepairFlow()
    return RateLimitRepairFlow(entry_id, issue_id)


class InvalidRepairFlow(RepairsFlow):
    """Abort a repair whose config entry no longer exists."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Abort an invalid repair flow."""
        return self.async_abort(reason="entry_not_found")


class RateLimitRepairFlow(RepairsFlow):
    """Switch an affected entry to the cloud-friendly polling profile."""

    def __init__(self, entry_id: str, issue_id: str) -> None:
        self._entry_id = entry_id
        self._issue_id = issue_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Start the confirmation step."""
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Apply a lower request frequency after user confirmation."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        if user_input is None:
            return self.async_show_form(step_id="confirm")

        self.hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_POLLING_PROFILE: PollingProfile.CLOUD_FRIENDLY.value,
            },
        )
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_create_entry(data={})


class FirmwareCompatibilityRepairFlow(RepairsFlow):
    """Rediscover capabilities after a possible PointT firmware schema change."""

    def __init__(self, entry_id: str, issue_id: str) -> None:
        self._entry_id = entry_id
        self._issue_id = issue_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Start the rediscovery confirmation step."""
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Reload and let capability checks decide whether the issue remains."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        if user_input is None:
            return self.async_show_form(step_id="confirm")

        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_create_entry(data={})
