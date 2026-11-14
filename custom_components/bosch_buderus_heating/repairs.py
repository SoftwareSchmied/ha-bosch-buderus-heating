"""Repair flows for Bosch/Buderus Home Comfort."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import BooleanSelector

if TYPE_CHECKING:
    # Home Assistant 2026.3 has no runtime alias; annotations stay lazy.
    from homeassistant.components.repairs import RepairsFlowResult

from .configuration import CLOUD_KIND, ConfigurationError
from .configuration_access import EntryConfiguration
from .configuration_management import ConfigurationManagement, ConnectionEdit
from .connection_runtime import SourceContext
from .const import (
    DOMAIN,
    FIRMWARE_ISSUE_PREFIX,
    GATEWAY_EXCHANGE_ISSUE_PREFIX,
    LOCAL_ADDRESS_ISSUE_PREFIX,
    LOCAL_START_ISSUE_PREFIX,
    RATE_LIMIT_ISSUE_PREFIX,
    PollingProfile,
)
from .gateway_exchange import (
    ExchangeCheck,
    GatewayExchangeError,
    ModuleEvidence,
    async_check_exchange,
    async_exchange_gateway,
    module_fingerprints,
    short_id,
)
from .ha_schema import validation as vol
from .log_helpers import log_failure
from .repair_scope import repair_id

_LOGGER = logging.getLogger(__name__)


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a repair flow for a supported issue."""
    entry_id = data.get("entry_id") if data else None
    if not isinstance(entry_id, str):
        return InvalidRepairFlow()
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is not None and entry.version == 4:
        connection = data.get("connection_id") if data else None
        binding = data.get("binding_id") if data else None
        if issue_id.startswith(GATEWAY_EXCHANGE_ISSUE_PREFIX):
            new = data.get("new_gateway") if data else None
            if (
                not isinstance(connection, str)
                or not isinstance(binding, str)
                or not isinstance(new, str)
                or issue_id
                != repair_id(GATEWAY_EXCHANGE_ISSUE_PREFIX, entry, connection, binding)
            ):
                return InvalidRepairFlow()
            return GatewayExchangeRepairFlow(
                entry_id, issue_id, connection, binding, new
            )
        if issue_id.startswith(LOCAL_START_ISSUE_PREFIX):
            problem = data.get("problem") if data else None
            if (
                not isinstance(connection, str)
                or not isinstance(binding, str)
                or not isinstance(problem, str)
                or issue_id
                != repair_id(LOCAL_START_ISSUE_PREFIX, entry, connection, binding)
            ):
                return InvalidRepairFlow()
            return LocalStartRepairFlow(entry_id, binding, problem)
        if issue_id.startswith(LOCAL_ADDRESS_ISSUE_PREFIX):
            if (
                not isinstance(connection, str)
                or binding is not None
                or issue_id != repair_id(LOCAL_ADDRESS_ISSUE_PREFIX, entry, connection)
            ):
                return InvalidRepairFlow()
            return LocalAddressRepairFlow(issue_id)
        firmware = issue_id.startswith(FIRMWARE_ISSUE_PREFIX)
        prefix = FIRMWARE_ISSUE_PREFIX if firmware else RATE_LIMIT_ISSUE_PREFIX
        if (
            not isinstance(connection, str)
            or (firmware and not isinstance(binding, str))
            or (not firmware and binding is not None)
            or issue_id
            != repair_id(
                prefix, entry, connection, binding if isinstance(binding, str) else None
            )
        ):
            return InvalidRepairFlow()
        return ManagedRepairFlow(
            entry_id,
            issue_id,
            connection,
            binding if isinstance(binding, str) else None,
        )
    if issue_id.startswith(FIRMWARE_ISSUE_PREFIX):
        return FirmwareCompatibilityRepairFlow(entry_id, issue_id)
    if not issue_id.startswith(RATE_LIMIT_ISSUE_PREFIX):
        return InvalidRepairFlow()
    return RateLimitRepairFlow(entry_id, issue_id)


# Every confirmation starts with its form: Home Assistant hands the issue to
# the first step as input, which is no confirmation.


class InvalidRepairFlow(RepairsFlow):
    """Abort a repair whose config entry no longer exists."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Abort an invalid repair flow."""
        return self.async_abort(reason="entry_not_found")


class ManagedRepairFlow(RepairsFlow):
    """Change only the confirmed access or rediscover only its affected source."""

    def __init__(
        self, entry_id: str, issue_id: str, connection_id: str, binding_id: str | None
    ) -> None:
        self._entry_id, self._issue_id = entry_id, issue_id
        self._connection_id, self._binding_id = connection_id, binding_id
        self._edit: ConnectionEdit | None = None
        self._source: SourceContext | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        from .managed_entry import ManagedEntry

        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        try:
            manager = ConfigurationManagement(self.hass, entry)
            current = manager.edit(self._connection_id)
            if manager.current().connections[self._connection_id].kind != CLOUD_KIND:
                raise ConfigurationError("unsupported_connection")
            if user_input is None:
                self._edit = current
                if self._binding_id is not None:
                    runtime = getattr(entry, "runtime_data", None)
                    self._source = (
                        runtime.connections.source_context(self._binding_id)
                        if isinstance(runtime, ManagedEntry)
                        else None
                    )
                    if (
                        self._source is None
                        or self._source.generation.connection_id != self._connection_id
                    ):
                        raise ConfigurationError("binding_unavailable")
                return self.async_show_form(step_id="confirm")
            if self._edit != current:
                raise ConfigurationError("configuration_changed")
            if self._binding_id is None:
                manager.set_polling_profile(current, PollingProfile.CLOUD_FRIENDLY)
                ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
            elif self._source is not None:
                self._source.runtime.restart_source(self._source)
            else:
                raise ConfigurationError("binding_unavailable")
        except ConfigurationError:
            return self.async_abort(reason="configuration_changed")
        return self.async_create_entry(data={})


class _RetryableStart(Protocol):
    def retry_start(self) -> bool: ...


class LocalStartRepairFlow(RepairsFlow):
    """B01: try the failing start check of a local access again now.

    Home Assistant removes the note when the flow ends; it comes back with
    the next failed check, and a successful one keeps it away.
    """

    def __init__(self, entry_id: str, binding_id: str, problem: str) -> None:
        self._entry_id, self._binding_id = entry_id, binding_id
        self._problem = problem

    def _source(self) -> _RetryableStart | None:
        """The running local source of the note, if it still runs."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        runtime = getattr(getattr(entry, "runtime_data", None), "connections", None)
        lookup = getattr(runtime, "source_driver", None)
        driver = lookup(self._binding_id) if callable(lookup) else None
        if not callable(getattr(driver, "retry_start", None)):
            return None
        return cast(_RetryableStart, driver)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        if self.hass.config_entries.async_get_entry(self._entry_id) is None:
            return self.async_abort(reason="entry_not_found")
        source = self._source()
        if source is None:
            return self.async_abort(reason="configuration_changed")
        if user_input is None:
            return self.async_show_form(
                step_id="confirm", description_placeholders={"code": self._problem}
            )
        if not source.retry_start():
            return self.async_abort(reason="configuration_changed")
        return self.async_create_entry(data={})


class LocalAddressRepairFlow(RepairsFlow):
    """Save the new address of a moved gateway after explicit confirmation.

    The address was proven by the certificate confirmed at setup; it is
    proven once more right before the change. Nothing is sent with a token.
    """

    def __init__(self, issue_id: str) -> None:
        self._issue_id = issue_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        from . import local_address

        item = local_address.candidate(self.hass, self._issue_id)
        if item is None:
            # Kept in memory only, for example gone after a restart.
            local_address.withdraw(self.hass, self._issue_id)
            return self.async_abort(reason="local_address_expired")
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("confirm") is True:
                outcome = await local_address.apply_address(self.hass, self._issue_id)
                if outcome == "applied":
                    return self.async_create_entry(data={})
                return self.async_abort(reason=outcome)
            errors["base"] = "confirmation_required"
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): BooleanSelector()}
            ),
            description_placeholders={
                "previous": item.previous.host,
                "address": item.endpoint.host,
            },
            errors=errors,
        )


class RateLimitRepairFlow(RepairsFlow):
    """Switch an affected entry to the cloud-friendly polling profile."""

    def __init__(self, entry_id: str, issue_id: str) -> None:
        self._entry_id = entry_id
        self._issue_id = issue_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Start the confirmation step."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Apply a lower request frequency after user confirmation."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        if user_input is None:
            return self.async_show_form(step_id="confirm")

        changed = self.hass.config_entries.async_update_entry(
            entry,
            options=EntryConfiguration(entry).polling_options(
                PollingProfile.CLOUD_FRIENDLY
            ),
        )
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        if (
            not changed
            or entry.state is not ConfigEntryState.LOADED
            or not entry.update_listeners
        ):
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
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
        return await self.async_step_confirm()

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
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data={})


_EVIDENCE_STEPS = {
    ModuleEvidence.MATCH: "evidence_match",
    ModuleEvidence.NO_MATCH: "evidence_no_match",
    ModuleEvidence.UNKNOWN: "evidence_unknown",
}


class GatewayExchangeRepairFlow(RepairsFlow):
    """P50, 3C: continue the same system with a new gateway, keep it separate
    or decide later. Without module evidence only after an explicit warning."""

    def __init__(
        self,
        entry_id: str,
        issue_id: str,
        connection_id: str,
        binding_id: str,
        new_gateway: str,
    ) -> None:
        self._entry_id, self._issue_id = entry_id, issue_id
        self._connection_id, self._binding_id = connection_id, binding_id
        self._new = new_gateway
        self._check: ExchangeCheck | None = None

    def _placeholders(self) -> dict[str, str]:
        check = self._check
        return {
            "old": short_id(check.old) if check is not None else "",
            "new": short_id(self._new),
        }

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        try:
            self._check = await async_check_exchange(
                self.hass, entry, self._connection_id, self._binding_id, self._new
            )
        except GatewayExchangeError as error:
            return self.async_abort(reason=error.reason)
        return self.async_show_menu(
            step_id=_EVIDENCE_STEPS[self._check.evidence],
            menu_options=["continue", "separate", "later"],
            description_placeholders=self._placeholders(),
        )

    async def async_step_continue(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        if self._check is None:
            return self.async_abort(reason="configuration_changed")
        if self._check.evidence is ModuleEvidence.MATCH:
            return await self.async_step_continue_match()
        return await self.async_step_continue_unverified()

    async def async_step_continue_match(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        check = self._check
        if check is None:
            return self.async_abort(reason="configuration_changed")
        if check.evidence is not ModuleEvidence.MATCH:
            return await self.async_step_continue_unverified()
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("confirm") is True:
                return await self._exchange()
            errors["base"] = "confirmation_required"
        return self.async_show_form(
            step_id="continue_match",
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): BooleanSelector()}
            ),
            description_placeholders=self._placeholders(),
            errors=errors,
        )

    async def async_step_continue_unverified(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        if self._check is None:
            return self.async_abort(reason="configuration_changed")
        errors: dict[str, str] = {}
        if user_input is not None:
            if (
                user_input.get("confirm") is True
                and user_input.get("confirm_unverified") is True
            ):
                return await self._exchange()
            errors["base"] = "confirmation_required"
        return self.async_show_form(
            step_id="continue_unverified",
            data_schema=vol.Schema(
                {
                    vol.Required("confirm", default=False): BooleanSelector(),
                    vol.Required("confirm_unverified", default=False): (
                        BooleanSelector()
                    ),
                }
            ),
            description_placeholders=self._placeholders(),
            errors=errors,
        )

    async def _exchange(self) -> RepairsFlowResult:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        assert self._check is not None
        try:
            await async_exchange_gateway(self.hass, entry, self._check)
        except GatewayExchangeError as error:
            return self.async_abort(reason=error.reason)
        except Exception as err:
            log_failure(
                _LOGGER, logging.ERROR, "The gateway exchange failed", error=err
            )
            return self.async_abort(reason="exchange_failed")
        return self.async_create_entry(data={})

    async def async_step_separate(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="separate", description_placeholders=self._placeholders()
            )
        await module_fingerprints(self.hass, self._entry_id).async_decline(
            self._binding_id, self._new
        )
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        return self.async_create_entry(data={})

    async def async_step_later(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        return self.async_abort(reason="later")
