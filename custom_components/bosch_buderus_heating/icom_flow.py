"""The setup dialog of the local access of an iCom gateway (connections menu).

Two steps: the plant of the cloud the gateway belongs to (skipped when only
one fits, or when the dialog was opened from an existing access), then the
address and the two passwords. The check reads the gateway's own ID once; only
when it is the cloud's gateway is the access saved. A failed check names its
cause on the form and keeps the address.
"""

from __future__ import annotations

from typing import Any, cast

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .configuration import ConfigurationError
from .configuration_management import ConfigurationManagement, ConnectionEdit
from .gateway_identity import GatewayNamespace
from .ha_schema import validation as vol
from .icom_access import IcomAccessError, is_icom_connection, load_icom_access
from .icom_setup import (
    cloud_gateway_ids,
    cloud_readings,
    commit_icom_setup,
    verify_icom_access,
)
from .local_setup_history import local_setup_history
from .local_support import setup_support
from .options_navigation import NavigatingOptionsFlow
from .plant_labels import plant_choices

# The errors of the address and password form, each with its own text.
_FORM_ERRORS = frozenset(
    {
        "icom_cannot_connect",
        "icom_invalid_auth",
        "icom_no_private_password",
        "icom_busy",
        "icom_no_identity",
        "icom_other_gateway",
        "icom_invalid_host",
        "invalid_icom_identity",
        "invalid_gateway_password",
        "invalid_private_password",
    }
)
# Failures of the save that end the dialog with an abort text of their own.
_ABORTS = frozenset(
    {
        "configuration_changed",
        "local_already_connected",
        "local_target_unavailable",
        "local_identity_conflict",
        "identity_contract_scope_changed",
    }
)


class IcomSetupFlow(NavigatingOptionsFlow):
    """Set up (or change) the local access of an iCom gateway."""

    _entry: ConfigEntry[Any]
    _connection_edit: ConnectionEdit | None
    _icom_target: str | None = None
    _icom_host = ""

    def _icom_snapshot(self) -> Any:
        return ConfigurationManagement(self.hass, self._entry).current()

    def _icom_gateways(self, snapshot: Any) -> dict[str, str]:
        """The plants of the cloud this dialog may set an access up for."""
        edit = getattr(self, "_connection_edit", None)
        result: dict[str, str] = {}
        for key, target in snapshot.targets.items():
            if target.kind != "pointt_object":
                continue
            if edit is not None and not any(
                binding.connection_id == edit.connection_id and binding.target_id == key
                for binding in snapshot.bindings.values()
            ):
                continue
            identities = cast(list[dict[str, Any]], target.to_data()["identities"])
            gateway = next(
                (
                    item["value"]
                    for item in identities
                    if item["namespace"] == GatewayNamespace.POINTT
                ),
                None,
            )
            if isinstance(gateway, str):
                result[key] = gateway
        return result

    async def async_step_icom_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if setup_support(self.hass) is None:
            return self.async_abort(reason="local_setup_unavailable")
        try:
            snapshot = self._icom_snapshot()
        except ConfigurationError:
            return self.async_abort(reason="configuration_changed")
        gateways = self._icom_gateways(snapshot)
        if not gateways:
            return self.async_abort(reason="icom_target_required")
        if user_input is not None:
            if user_input.get("target") not in gateways:
                return self.async_abort(reason="configuration_changed")
            self._icom_target = user_input["target"]
        elif len(gateways) == 1:
            self._icom_target = next(iter(gateways))
        else:
            return self.async_show_form(
                step_id="icom_setup",
                data_schema=vol.Schema(
                    {
                        vol.Required("target"): SelectSelector(
                            SelectSelectorConfig(
                                options=plant_choices(
                                    self.hass, snapshot.owner_id, gateways
                                )
                            )
                        )
                    }
                ),
            )
        self._icom_host = self._icom_stored_host(snapshot, self._icom_target)
        return await self.async_step_icom_access()

    @staticmethod
    def _icom_stored_host(snapshot: Any, target_id: str | None) -> str:
        """The address of an iCom access this plant already has, if any."""
        for binding in snapshot.bindings.values():
            if binding.target_id != target_id:
                continue
            data = snapshot.connections[binding.connection_id].to_data()
            if not is_icom_connection(data):
                continue
            try:
                return load_icom_access(data).host
            except IcomAccessError:
                return ""
        return ""

    async def async_step_icom_access(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        target = self._icom_target
        if target is None or setup_support(self.hass) is None:
            return self.async_abort(reason="configuration_changed")
        errors: dict[str, str] = {}
        if user_input is not None:
            self._icom_host = str(user_input.get("host", "")).strip()
            try:
                result = await self._icom_save(target, user_input)
            except ConfigurationError as error:
                code = str(error)
                self._icom_attempt(code)
                if code in _ABORTS:
                    return self.async_abort(reason=code)
                errors["base"] = code if code in _FORM_ERRORS else "icom_cannot_connect"
            else:
                self._icom_attempt(None)
                return result
        return self.async_show_form(
            step_id="icom_access",
            data_schema=vol.Schema(
                {
                    vol.Required("host", default=self._icom_host): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    ),
                    vol.Required("gateway_password"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Required("private_password"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    def _icom_attempt(self, error: str | None) -> None:
        """Note the check in the entry's history: codes only, never data."""
        try:
            local_setup_history(self.hass, self._entry.entry_id).add(
                "icom_access", error
            )
        except Exception:  # the history only explains; it never stops a dialog
            return

    async def _icom_save(
        self, target_id: str, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        snapshot = self._icom_snapshot()
        gateway = self._icom_gateways(snapshot).get(target_id)
        if gateway is None:
            raise ConfigurationError("configuration_changed")
        owner = getattr(self._entry, "runtime_data", None)
        verification = await verify_icom_access(
            async_get_clientsession(self.hass),
            target_id=target_id,
            gateway_id=gateway,
            host=str(user_input.get("host", "")),
            gateway_password=str(user_input.get("gateway_password", "")),
            private_password=str(user_input.get("private_password", "")),
            also_accepted=cloud_gateway_ids(owner, snapshot, target_id),
            cloud_readings=cloud_readings(owner, snapshot, target_id),
        )
        commit_icom_setup(ConfigurationManagement(self.hass, self._entry), verification)
        self._icom_target = None
        return await self.async_return_to_menu("connections", "icom_connected")
