"""Connection settings remain reachable while any or every source is offline."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowContext,
    ConfigFlowResult,
)
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .configuration import CLOUD_KIND, LOCAL_KIND, ConfigurationError
from .configuration_management import ConfigurationManagement, ConnectionEdit, section
from .const import PollingProfile
from .ha_schema import validation as vol
from .local_enrollment import LocalEnrollmentError, enrollment_journal
from .local_support import setup_support
from .options_navigation import BACK, NavigatingOptionsFlow

# The menu entry of the n-th connection (the menu lists them with their
# labels; the entries name no identifier).
_OPEN_PREFIX = "connection_open_"


class ConnectionOptionsFlow(NavigatingOptionsFlow):
    """Edit the explicitly selected connection without replacing the owner."""

    _entry: ConfigEntry[Any]
    _connection_edit: ConnectionEdit | None = None
    _connection_choices: dict[str, str] | None = None

    def _management(self) -> ConfigurationManagement:
        return ConfigurationManagement(self.hass, self._entry)

    def __getattr__(self, name: str) -> Any:
        """The step of one connection entry of the connections menu."""
        step = name.removeprefix("async_step_")
        choices = self.__dict__.get("_connection_choices") or {}
        if name.startswith("async_step_" + _OPEN_PREFIX) and step in choices:
            connection_id = choices[step]

            async def open_connection(
                user_input: dict[str, Any] | None = None,
            ) -> ConfigFlowResult:
                return await self._open_connection(connection_id)

            step_method: Callable[..., Awaitable[ConfigFlowResult]] = open_connection
            return step_method
        raise AttributeError(name)

    async def _open_connection(self, connection_id: str) -> ConfigFlowResult:
        try:
            self._connection_edit = self._management().edit(connection_id)
        except ValueError, KeyError:
            return self.async_abort(reason="configuration_changed")
        return await self.async_step_connection()

    async def async_step_connections(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The accesses as a menu, with a way back to the main menu."""
        management = self._management()
        try:
            snapshot = management.current()
        except ConfigurationError:
            return self.async_abort(reason="configuration_changed")
        if not snapshot.connections:
            return self.async_abort(reason="no_connections")
        from .connection_status import connection_label

        menu: dict[str, str] = {}
        choices: dict[str, str] = {}
        for number, (key, connection) in enumerate(snapshot.connections.items(), 1):
            # Kind, number and current state from existing data; never a brand.
            step = f"{_OPEN_PREFIX}{number}"
            choices[step] = key
            menu[step] = connection_label(
                self.hass, self._entry, key, connection, number
            )
        menu[BACK] = await self.back_label("connections")
        self._connection_choices = choices
        self._connection_edit = None
        self._back_target = "init"
        return self.async_show_menu(
            step_id="connections",
            menu_options=menu,
            description_placeholders={"result": self.take_result_note()},
        )

    async def _connection_saved(self) -> ConfigFlowResult:
        """Back to the connection's menu after a saved change."""
        edit = self._connection_edit
        try:
            if edit is None:
                raise ConfigurationError("configuration_changed")
            self._connection_edit = self._management().edit(edit.connection_id)
        except ConfigurationError, ValueError, KeyError:
            return await self.async_return_to_menu("connections", "connection_saved")
        return await self.async_return_to_menu("connection", "connection_saved")

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        edit = self._connection_edit
        if edit is None:
            return self.async_abort(reason="configuration_changed")
        try:
            current = self._management().edit(edit.connection_id)
        except ConfigurationError:
            return self.async_abort(reason="configuration_changed")
        if current != edit:
            return self.async_abort(reason="configuration_changed")
        current_snapshot = self._management().current()
        connection = current_snapshot.connections[edit.connection_id]
        linked = any(
            binding.connection_id == edit.connection_id
            for binding in current_snapshot.bindings.values()
        )
        from .connection_status import connection_description

        self._back_target = "connections"
        return self.async_show_menu(
            step_id="connection",
            description_placeholders={
                "result": self.take_result_note(),
                "status": connection_description(
                    self.hass, self._entry, edit.connection_id
                ),
            },
            menu_options=[
                "connection_test",
                "connection_enabled",
                "connection_targets",
                *(["local_polling"] if connection.kind == LOCAL_KIND else []),
                *(["local_values"] if connection.kind == LOCAL_KIND and linked else []),
                *(
                    ["local_setup"]
                    if connection.kind == LOCAL_KIND
                    and setup_support(self.hass) is not None
                    else []
                ),
                *(
                    [
                        "connection_discover",
                        "connection_polling",
                        "connection_reauthenticate",
                    ]
                    if connection.kind == CLOUD_KIND
                    else []
                ),
                *(
                    ["connection_forget"]
                    if connection.kind == CLOUD_KIND
                    and self._forgettable(current_snapshot, edit.connection_id)
                    else []
                ),
                "connection_remove",
                BACK,
            ],
        )

    async def async_step_connection_test(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Read once on explicit request; changes nothing (P21)."""
        from .connection_status import test_connection

        edit = self._connection_edit
        if edit is None:
            return self.async_abort(reason="configuration_changed")
        if user_input is not None:
            return await self.async_step_connection()
        result = await test_connection(self.hass, self._entry, edit.connection_id)
        return self.async_show_form(
            step_id="connection_test",
            data_schema=vol.Schema({}),
            description_placeholders={"result": result},
        )

    async def async_step_connection_enabled(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        edit = self._connection_edit
        if edit is None:
            return self.async_abort(reason="configuration_changed")
        try:
            if user_input is not None:
                self._management().set_enabled(edit, user_input["enabled"])
                return await self._connection_saved()
            snapshot = self._management().current()
            enabled = section(snapshot.to_options(), "connections")[edit.connection_id][
                "enabled"
            ]
        except ValueError, KeyError:
            return self.async_abort(reason="configuration_changed")
        return self.async_show_form(
            step_id="connection_enabled",
            data_schema=vol.Schema(
                {vol.Required("enabled", default=enabled): BooleanSelector()}
            ),
        )

    @staticmethod
    def _forgettable(snapshot: Any, connection_id: str) -> dict[str, str]:
        """Deselected systems and devices of a cloud access, by binding (P50, R1)."""
        options = section(snapshot.to_options(), "bindings")
        return {
            key: str(cast(dict[str, Any], binding.to_data()["locator"])["value"])
            for key, binding in snapshot.bindings.items()
            if binding.connection_id == connection_id
            and binding.adapter in ("pointt", "bacon")
            and not options[key]["enabled"]
        }

    async def async_step_connection_forget(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Forget deselected systems, for example an exchanged gateway."""
        edit = self._connection_edit
        if edit is None:
            return self.async_abort(reason="configuration_changed")
        management = self._management()
        try:
            if management.edit(edit.connection_id) != edit:
                return self.async_abort(reason="configuration_changed")
            choices = self._forgettable(management.current(), edit.connection_id)
        except ConfigurationError:
            return self.async_abort(reason="configuration_changed")
        if not choices:
            return self.async_abort(reason="configuration_changed")
        if user_input is not None:
            selected = user_input.get("bindings")
            if not isinstance(selected, list) or not selected:
                return await self.async_step_connection()
            try:
                management.forget_bindings(edit, set(selected))
            except ConfigurationError:
                return self.async_abort(reason="configuration_changed")
            return await self._connection_saved()
        return self.async_show_form(
            step_id="connection_forget",
            data_schema=vol.Schema(
                {
                    vol.Required("bindings", default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=key, label=label)
                                for key, label in choices.items()
                            ],
                            multiple=True,
                        )
                    )
                }
            ),
        )

    async def async_step_connection_targets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        edit = self._connection_edit
        if edit is None:
            return self.async_abort(reason="configuration_changed")
        management = self._management()
        try:
            if management.edit(edit.connection_id) != edit:
                return self.async_abort(reason="configuration_changed")
            snapshot = management.current()
        except ConfigurationError:
            return self.async_abort(reason="configuration_changed")
        bindings = {
            key: binding
            for key, binding in snapshot.bindings.items()
            if binding.connection_id == edit.connection_id
        }
        if user_input is not None:
            selected = user_input.get("targets")
            if not isinstance(selected, list) or any(
                key not in bindings for key in selected
            ):
                return self.async_abort(reason="configuration_changed")
            try:
                management.set_targets_enabled(edit, set(selected))
            except ConfigurationError:
                return self.async_abort(reason="configuration_changed")
            return await self._connection_saved()
        options = section(snapshot.to_options(), "bindings")
        choices = [
            SelectOptionDict(
                value=key,
                label=str(cast(dict[str, Any], item.to_data()["locator"])["value"]),
            )
            for key, item in bindings.items()
        ]
        return self.async_show_form(
            step_id="connection_targets",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "targets",
                        default=[key for key in bindings if options[key]["enabled"]],
                    ): SelectSelector(
                        SelectSelectorConfig(options=choices, multiple=True)
                    )
                }
            ),
        )

    async def async_step_connection_polling(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        edit = self._connection_edit
        try:
            if edit is None or self._management().edit(edit.connection_id) != edit:
                raise ConfigurationError("configuration_changed")
            snapshot = self._management().current()
            if snapshot.connections[edit.connection_id].kind != CLOUD_KIND:
                raise ConfigurationError("unsupported_connection")
            if user_input is not None:
                profile = PollingProfile(user_input["polling_profile"])
                self._management().set_polling_profile(edit, profile)
                return await self._connection_saved()
            current = section(snapshot.to_options(), "connections")[edit.connection_id][
                "polling_profile"
            ]
        except ValueError, KeyError:
            return self.async_abort(reason="configuration_changed")
        return self.async_show_form(
            step_id="connection_polling",
            data_schema=vol.Schema(
                {
                    vol.Required("polling_profile", default=current): SelectSelector(
                        SelectSelectorConfig(
                            options=[item.value for item in PollingProfile],
                            translation_key="polling_profile",
                        )
                    )
                }
            ),
        )

    async def async_step_local_polling(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        edit = self._connection_edit
        errors = {}
        try:
            if edit is None or self._management().edit(edit.connection_id) != edit:
                raise ConfigurationError("configuration_changed")
            snapshot = self._management().current()
            if snapshot.connections[edit.connection_id].kind != LOCAL_KIND:
                raise ConfigurationError("unsupported_connection")
            current = section(snapshot.to_options(), "connections")[
                edit.connection_id
            ].get("local_interval", 60)
            if user_input is not None:
                value = user_input.get("interval")
                if type(value) not in (int, float) or value not in range(60, 901, 60):
                    errors["interval"] = "local_invalid_interval"
                else:
                    self._management().set_local_interval(
                        edit, int(cast(int | float, value))
                    )
                    return await self._connection_saved()
        except ConfigurationError:
            return self.async_abort(reason="configuration_changed")
        from .managed_entry import ManagedEntry

        owner = getattr(self._entry, "runtime_data", None)
        states = (
            [
                owner.connections.local_state(key)
                for key, binding in snapshot.bindings.items()
                if binding.connection_id == edit.connection_id
            ]
            if isinstance(owner, ManagedEntry)
            else []
        )
        known = sum(
            len(state.last_observations) for state in states if state is not None
        )
        available = sum(
            len(state.current_observations) for state in states if state is not None
        )
        return self.async_show_form(
            step_id="local_polling",
            data_schema=vol.Schema(
                {
                    vol.Required("interval", default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=60,
                            max=900,
                            step=60,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={"known": str(known), "available": str(available)},
        )

    async def async_step_local_values(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose whether paired local readings supply existing entities."""
        edit = self._connection_edit
        try:
            if edit is None or self._management().edit(edit.connection_id) != edit:
                raise ConfigurationError("configuration_changed")
            snapshot = self._management().current()
            if snapshot.connections[edit.connection_id].kind != LOCAL_KIND:
                raise ConfigurationError("unsupported_connection")
            targets = {
                binding.target_id
                for binding in snapshot.bindings.values()
                if binding.connection_id == edit.connection_id
            }
            target_options = section(snapshot.to_options(), "targets")
            current = any(
                target_options[target].get("local_values", False) is True
                for target in targets
            )
            if user_input is not None:
                self._management().set_local_values(
                    edit, bool(user_input.get("enabled", False))
                )
                return await self._connection_saved()
        except ConfigurationError:
            return self.async_abort(reason="configuration_changed")
        return self.async_show_form(
            step_id="local_values",
            data_schema=vol.Schema(
                {vol.Required("enabled", default=current): BooleanSelector()}
            ),
        )

    async def async_step_connection_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        edit = self._connection_edit
        if edit is None:
            return self.async_abort(reason="configuration_changed")
        if user_input is not None:
            if not user_input.get("confirm"):
                return await self.async_step_connection()
            try:
                management = self._management()
                if management.edit(edit.connection_id) != edit:
                    raise ConfigurationError("configuration_changed")
                if (
                    management.current().connections[edit.connection_id].kind
                    == LOCAL_KIND
                ):
                    await enrollment_journal(self.hass).remove_access(management, edit)
                else:
                    management.remove(edit)
            except LocalEnrollmentError:
                # The setup record could not be read or saved; nothing was
                # removed (FLOW-3).
                return self.async_abort(reason="local_storage_failed")
            except ConfigurationError:
                return self.async_abort(reason="configuration_changed")
            self._connection_edit = None
            if not self._management().current().connections:
                return await self.async_return_to_menu("init", "connection_removed")
            return await self.async_return_to_menu("connections", "connection_removed")
        stored = self._management().current().connections.get(edit.connection_id)
        return self.async_show_form(
            # A cloud access explains what stays after the removal (R1, P49b).
            # An access removed meanwhile is refused when the form is submitted.
            step_id="connection_remove_cloud"
            if stored is not None and stored.kind == CLOUD_KIND
            else "connection_remove",
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): BooleanSelector()}
            ),
        )

    async def async_step_connection_remove_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_connection_remove(user_input)

    async def async_step_connection_reauthenticate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        edit = self._connection_edit
        try:
            if edit is None or self._management().edit(edit.connection_id) != edit:
                return self.async_abort(reason="configuration_changed")
            kind = self._management().current().connections[edit.connection_id].kind
        except ConfigurationError:
            return self.async_abort(reason="configuration_changed")
        if kind != CLOUD_KIND:
            return self.async_abort(reason="configuration_changed")
        self._entry.async_start_reauth(
            self.hass,
            context=cast(ConfigFlowContext, {"connection_id": edit.connection_id}),
        )
        return self.async_abort(reason="connection_reauth_started")
