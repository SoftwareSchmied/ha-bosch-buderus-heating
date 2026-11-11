"""Native local setup dialogs on a retained owner, behind runtime readiness."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any, cast

from homeassistant.config_entries import (
    ConfigEntryBaseFlow,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .cloud_configuration import cloud_snapshots
from .configuration import LOCAL_KIND, ConfigurationError
from .configuration_management import ConfigurationManagement
from .const import DOMAIN
from .ha_schema import validation as vol
from .i18n import localize
from .local_auth import AuthState, LabelCredentials
from .local_budget import LocalReadBudget, local_budget_manager
from .local_completion import LOCAL_FIRST, RecoveryStatus, ScopedIdentityContract
from .local_credentials import LocalCredentialError, load_local_access
from .local_discovery import (
    configured_discovery,
    discovery_endpoints,
    dismiss_configured_discoveries,
    forget_discovery_flow,
    remember_discovery,
    track_discovery_flow,
)
from .local_enrollment import PendingEnrollment, enrollment_journal
from .local_handover import complete_local_setup
from .local_probe import LocalEndpoint, LocalProbeError, ProbeContext
from .local_setup import (
    LocalSetupEdit,
    LocalSetupProbeError,
    begin_local_setup,
    is_new_local_plant,
    new_local_plant_id,
    prepare_local_configuration,
    verify_local_setup,
)
from .local_support import setup_support
from .local_transport import (
    CertificateBinding,
    CertificateObservation,
    PortRole,
    inspect_local_certificate,
)
from .markdown_text import markdown_literal

_LOGGER = logging.getLogger(__name__)

# P49b step 7: the choice of a new plant set up from its local access alone.
NEW_PLANT = "new_plant"
# The unique ID of a discovery notice (never of an entry that holds an access).
DISCOVERY_UNIQUE_ID_PREFIX = "local_discovery_"


def local_error(error: object) -> str:
    """Render known categories only; exception text never reaches the frontend.

    Only this integration's own errors carry a code (ERR-8): the text of any
    other error, e.g. a ValueError("timeout") from a library, is no code.
    """
    if isinstance(error, BaseException) and not type(error).__module__.startswith(
        __package__ or __name__
    ):
        return "local_unexpected"
    return {
        "invalid_address": "local_invalid_address",
        "interface_required": "local_interface_required",
        "invalid_local_token": "local_invalid_token",
        "invalid_label_credentials": "local_invalid_credentials",
        "invalid_credentials": "local_invalid_credentials",
        "physical_confirmation_required": "local_unlock_required",
        "token_database_full": "local_token_database_full",
        "rate_limited": "local_rate_limited",
        "enrollment_retry_later": "local_rate_limited",
        "local_budget_deferred": "local_busy",
        "local_budget_capacity": "local_storage_failed",
        "local_budget_storage_failed": "local_storage_failed",
        "local_budget_closed": "local_storage_failed",
        "service_unavailable": "local_service_unavailable",
        "request_timeout": "local_timeout",
        "timeout": "local_timeout",
        "name_resolution_failed": "local_connection_failed",
        "dns": "local_connection_failed",
        "connection_failed": "local_connection_failed",
        "connection": "local_connection_failed",
        "certificate_untrusted": "local_certificate_changed",
        "tls": "local_certificate_changed",
        "certificate_changed": "local_certificate_changed",
        "token_rejected": "local_token_rejected",
        # The sign-in service (9442) answers 401/403 itself; the data port's
        # rejections arrive as token_rejected/resource_forbidden (P39).
        "unauthorized": "local_invalid_credentials",
        "resource_forbidden": "local_resource_forbidden",
        "forbidden": "local_auth_forbidden",
        "interface": "local_interface_required",
        "token_creation_uncertain": "local_creation_uncertain",
        "enrollment_completion_uncertain": "local_completion_uncertain",
        "redirect_rejected": "local_invalid_response",
        "http_error": "local_invalid_response",
        "identity_invalid": "local_invalid_response",
        "required_resource_missing": "local_missing_resource",
        "not_found": "local_missing_resource",
        "firmware_unverified": "local_unknown_firmware",
        "firmware_update_required": "local_old_firmware",
        "unsupported_brand": "local_unsupported_brand",
        "system_information_incomplete": "local_incomplete_system",
        "invalid_response": "local_invalid_response",
        "invalid_json": "local_invalid_response",
        "identity_contract_required": "local_identity_unverified",
        "local_cloud_auth_required": "local_cloud_auth_required",
        "local_cloud_rate_limited": "local_cloud_rate_limited",
        "local_cloud_unavailable": "local_cloud_unavailable",
        "identity_contract_scope_changed": "local_identity_conflict",
        "local_plant_known": "local_plant_known",
        "local_identity_mismatch": "local_identity_conflict",
        "local_identity_conflict": "local_identity_conflict",
        "local_owner_conflict": "local_identity_conflict",
        "local_ambiguous": "local_identity_conflict",
        "configuration_changed": "local_configuration_changed",
        "entry_removed": "local_configuration_changed",
        "local_verification_expired": "local_configuration_changed",
        "enrollment_context_changed": "local_configuration_changed",
        "enrollment_already_pending": "local_pending_exists",
        "local_enrollment_storage_failed": "local_storage_failed",
        "local_save_outcome_uncertain": "local_completion_uncertain",
        "local_runtime_restart_failed": "local_completion_uncertain",
    }.get(str(error), "local_unexpected")


def reported_local_error(error: BaseException) -> str:
    """The code of a failed step; an unexpected error is logged, without text."""
    code = local_error(error)
    if code == "local_unexpected":
        _LOGGER.warning(
            "A local setup step failed unexpectedly (%s)", type(error).__name__
        )
        _LOGGER.debug("Local setup step failure", exc_info=error)
    return code


def discovery_unique_id(info: ZeroconfServiceInfo) -> str:
    """A stable flow ID of one announced gateway service, without identifiers.

    Derived from the announced service name (or host name), never shown. Only
    lets HA ignore the notice; it does not establish a gateway identity.
    """
    announced = (info.name or info.hostname or "").strip().lower().rstrip(".")
    digest = hashlib.sha256(("zeroconf\0" + announced).encode()).hexdigest()
    return DISCOVERY_UNIQUE_ID_PREFIX + digest[:32]


def selection(choices: list[SelectOptionDict]) -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(options=choices))


def translated_selection(values: list[str], key: str) -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(options=values, translation_key=key))


class LocalSetupFlow(ConfigEntryBaseFlow):
    """Shared state machine for options and discovery, never a second owner."""

    _local_phase = ""
    _local_closed = False
    _local_owner_id: str | None = None
    _local_target: str | None = None
    _local_edit: LocalSetupEdit | None = None
    _local_record: PendingEnrollment | None = None
    _local_observation: CertificateObservation | None = None
    _local_resource_pin: CertificateBinding | None = None
    _local_auth_pin: CertificateBinding | None = None
    _local_task: asyncio.Task[None] | None = None
    _local_retry: Callable[[], Awaitable[None]] | None = None
    _local_error: str | None = None
    _local_address = ""
    _local_choices: tuple[LocalEndpoint, ...] = ()
    _local_gateway = ""
    # FLOW-5: fingerprints confirmed in this dialog, by entry, address and port.
    _local_confirmed: dict[tuple[str, LocalEndpoint, PortRole], bytes] | None = None

    def local_available(self) -> bool:
        return setup_support(self.hass) is not None

    def _local_management(self) -> ConfigurationManagement:
        entry = self.hass.config_entries.async_get_entry(self._local_owner_id or "")
        if entry is None:
            raise ConfigurationError("entry_removed")
        return ConfigurationManagement(self.hass, entry)

    def _local_active(self) -> bool:
        if self._local_closed or not self.local_available():
            return False
        try:
            current = self._local_management().current()
            if self._local_edit is not None:
                self._local_edit.check(current)
        except ValueError:
            return False
        return True

    def _local_guard(self, phase: str) -> ConfigFlowResult | None:
        if not self.local_available():
            return self.async_abort(reason="local_setup_unavailable")
        if self._local_closed or self._local_phase != phase:
            return self.async_abort(reason="local_session_changed")
        return None

    def _local_form(
        self,
        step: str,
        schema: vol.Schema | None = None,
        *,
        errors: dict[str, str] | None = None,
        placeholders: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        self._local_phase = step
        return self.async_show_form(
            step_id=step,
            data_schema=schema or vol.Schema({}),
            errors=errors or {},
            description_placeholders=placeholders,
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        if not self.local_available():
            return self.async_abort(reason="local_setup_unavailable")
        try:
            self._local_choices = remember_discovery(self.hass, discovery_info)
        except LocalProbeError, AttributeError:
            return self.async_abort(reason="local_discovery_invalid")
        if configured_discovery(self.hass, self._local_choices):
            return self.async_abort(reason="already_configured")
        # Lets the notice be ignored; an ignored announcement stays ignored.
        if await cast(ConfigFlow, self).async_set_unique_id(
            discovery_unique_id(discovery_info)
        ):
            return self.async_abort(reason="already_configured")
        if not track_discovery_flow(self.hass, self.flow_id, self._local_choices):
            return self.async_abort(reason="already_in_progress")
        self.context["title_placeholders"] = {"host": self._local_choices[0].host}
        return await self.async_step_local_setup()

    async def async_step_local_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self.local_available():
            return self.async_abort(reason="local_setup_unavailable")
        if self._local_phase:
            return self.async_abort(reason="local_session_changed")
        entry = getattr(self, "_entry", None)
        if entry is not None:
            self._local_owner_id = entry.entry_id
            self._local_phase = "local_target"
            return await self.async_step_local_target()
        self._local_phase = "local_owner"
        return await self.async_step_local_owner()

    async def async_step_local_owner(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if denied := self._local_guard("local_owner"):
            return denied
        entries = {
            entry.entry_id: entry
            for entry in self.hass.config_entries.async_entries(
                DOMAIN, include_ignore=False
            )
            if entry.version == 4
        }
        if not entries:
            return self.async_abort(reason="local_existing_owner_required")
        if user_input is not None:
            owner = user_input.get("owner")
            if owner not in entries:
                return self.async_abort(reason="local_session_changed")
            self._local_owner_id = owner
            self._local_phase = "local_target"
            return await self.async_step_local_target()
        return self._local_form(
            "local_owner",
            vol.Schema(
                {
                    vol.Required("owner"): selection(
                        [
                            SelectOptionDict(value=key, label=entry.title)
                            for key, entry in entries.items()
                        ]
                    )
                }
            ),
        )

    async def async_step_local_target(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if denied := self._local_guard("local_target"):
            return denied
        try:
            current = self._local_management().current()
        except ValueError:
            return self.async_abort(reason="local_session_changed")
        targets = {
            key: target
            for key, target in current.targets.items()
            if target.kind in ("pointt_object", "local_installation")
        }
        selected_connection = getattr(self, "_connection_edit", None)
        if selected_connection is not None:
            targets = {
                key: target
                for key, target in targets.items()
                if any(
                    b.connection_id == selected_connection.connection_id
                    and b.target_id == key
                    for b in current.bindings.values()
                )
            }
        offer_new = selected_connection is None
        if not targets and not offer_new:
            return self.async_abort(reason="local_target_required")
        if not targets:
            # P49b step 7: an entry without any plant sets one up locally.
            user_input = {"target": NEW_PLANT}
        if user_input is None:
            return self._local_form(
                "local_target",
                vol.Schema(
                    {
                        vol.Required("target"): selection(
                            [
                                *(
                                    SelectOptionDict(
                                        value=key,
                                        label=str(
                                            cast(
                                                list[dict[str, Any]],
                                                target.to_data()["identities"],
                                            )[0]["value"]
                                        ),
                                    )
                                    for key, target in targets.items()
                                ),
                                *(
                                    [
                                        SelectOptionDict(
                                            value=NEW_PLANT,
                                            label=localize(
                                                self.hass.config.language,
                                                "Neue Anlage (nur lokal)",
                                                "New system (local only)",
                                            ),
                                        )
                                    ]
                                    if offer_new
                                    else []
                                ),
                            ]
                        )
                    }
                ),
            )
        if offer_new and user_input.get("target") == NEW_PLANT:
            try:
                records = await enrollment_journal(self.hass).records()
            except ValueError:
                return self.async_abort(reason="local_storage_failed")
            # An interrupted attempt for a new plant is resumed or discarded
            # first; otherwise a reserved target, created with the identity.
            self._local_record = next(
                (
                    r
                    for r in records
                    if r.owner_id == current.owner_id
                    and not r.resolved
                    and is_new_local_plant(current, r.target_id)
                ),
                None,
            )
            if self._local_record is not None:
                self._local_target = self._local_record.target_id
                self._local_phase = "local_pending"
                return await self.async_step_local_pending()
            self._local_target = new_local_plant_id()
            self._local_phase = "local_address"
            return await self.async_step_local_address()
        if user_input.get("target") not in targets:
            return self.async_abort(reason="local_session_changed")
        self._local_target = user_input["target"]
        try:
            records = await enrollment_journal(self.hass).records()
            self._local_record = next(
                (
                    r
                    for r in records
                    if r.owner_id == current.owner_id
                    and r.target_id == self._local_target
                    and not r.resolved
                ),
                None,
            )
        except ValueError:
            return self.async_abort(reason="local_storage_failed")
        if self._local_record is not None:
            self._local_phase = "local_pending"
            return await self.async_step_local_pending()
        self._local_phase = "local_address"
        return await self.async_step_local_address()

    async def async_step_local_address(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if denied := self._local_guard("local_address"):
            return denied
        errors = {}
        if user_input is not None:
            try:
                endpoint = LocalEndpoint(user_input.get("address", ""))
                current = self._local_management().current()
                bindings = [
                    b
                    for b in current.bindings.values()
                    if b.target_id == self._local_target and b.adapter == LOCAL_KIND
                ]
                self._local_edit = begin_local_setup(
                    current,
                    self._local_target or "",
                    endpoint,
                    connection_id=bindings[0].connection_id
                    if len(bindings) == 1
                    else None,
                )
                self._local_address = endpoint.host
            except ValueError as error:
                errors["address"] = reported_local_error(error)
            else:
                self._local_phase = "local_method"
                return await self.async_step_local_method()
        choices = tuple(
            dict.fromkeys((*self._local_choices, *discovery_endpoints(self.hass)))
        )
        # Custom values keep manual entry available even with no mDNS answers.
        return self._local_form(
            "local_address",
            vol.Schema(
                {
                    vol.Required("address"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=e.host, label=e.host)
                                for e in choices
                            ],
                            custom_value=True,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_local_method(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if denied := self._local_guard("local_method"):
            return denied
        stored = self._stored_local_token()
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("method") == "label":
                return await self._local_start(
                    lambda: self._local_inspect(PortRole.AUTHENTICATION)
                )
            if user_input.get("method") == "token":
                self._local_phase = "local_token"
                return await self.async_step_local_token()
            if user_input.get("method") == "keep" and stored is not None:
                # P39: after a certificate change the working token stays; it
                # is checked again against the newly confirmed certificate.
                assert self._local_edit is not None
                try:
                    self._local_record = await enrollment_journal(
                        self.hass
                    ).import_token(
                        self._local_edit,
                        stored,
                        current=self._local_management().current,
                        is_active=self._local_active,
                    )
                except (LocalCredentialError, ValueError) as error:
                    errors["base"] = reported_local_error(error)
                else:
                    return await self._local_start(
                        lambda: self._local_inspect(PortRole.RESOURCES)
                    )
            else:
                return self.async_abort(reason="local_session_changed")
        # A new sign-in with the device label comes first and is preselected.
        # Keeping the token trusts the newly confirmed certificate on first
        # use, so it is offered after it, with a warning (P39 review).
        methods = ["label", "keep", "token"] if stored else ["label", "token"]
        return self._local_form(
            "local_method_keep" if stored else "local_method",
            vol.Schema(
                {
                    vol.Required("method", default="label"): translated_selection(
                        methods, "local_method"
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_local_method_keep(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The method choice with the warning before keeping the stored token."""
        if self._local_phase == "local_method_keep":
            self._local_phase = "local_method"
        return await self.async_step_local_method(user_input)

    def _stored_local_token(self) -> str | None:
        """The working token of the local access being changed, if any."""
        edit = self._local_edit
        if edit is None:
            return None
        try:
            connection = (
                self._local_management()
                .current()
                .connections.get(edit.context.identity.connection_id)
            )
            if connection is None or connection.kind != LOCAL_KIND:
                return None
            access = load_local_access(connection.to_data())
            if access.endpoint != edit.context.endpoint:
                # Never hand this gateway's token to another address.
                return None
            return access.credentials.token
        except LocalCredentialError, ValueError, KeyError:
            return None

    async def _local_start(
        self, operation: Callable[[], Awaitable[None]]
    ) -> ConfigFlowResult:
        self._local_retry = operation
        self._local_phase = "local_progress"
        self._local_error = None

        async def run() -> None:
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._local_error = reported_local_error(error)
                self._local_phase = "local_retry"

        self._local_task = self.hass.async_create_task(run(), "local setup stage")
        try:
            await self._local_task
        except asyncio.CancelledError:
            if not self._local_closed:
                raise
        if self._local_closed or not self.local_available():
            return self.async_abort(reason="local_setup_unavailable")
        # Keep bounded I/O in the native form request. HA displays its loading
        # state; a fast result cannot precede a progress-event subscription.
        # Render the next form without input, so a certificate confirmation
        # can never become permission to save the connection.
        step = cast(
            Callable[[dict[str, Any] | None], Awaitable[ConfigFlowResult]],
            getattr(self, f"async_step_{self._local_phase}"),
        )
        return await step(None)

    def _local_previous_pin(self, role: PortRole) -> str | None:
        if self._local_edit is None:
            return None
        connection = (
            self._local_management()
            .current()
            .connections.get(self._local_edit.context.identity.connection_id)
        )
        if connection is None:
            return None
        try:
            access = load_local_access(connection.to_data())
        except ValueError:
            return None
        if access.endpoint != self._local_edit.context.endpoint:
            return None
        return (
            access.authentication_sha256
            if role == PortRole.AUTHENTICATION
            else access.resource_sha256
        )

    def _local_pin_key(
        self, context: ProbeContext, role: PortRole
    ) -> tuple[str, LocalEndpoint, PortRole]:
        # Each new attempt gets a new generation and possibly a new access
        # ID; the trust decision of the user is about this address and port.
        return context.identity.owner_id, context.endpoint, role

    def _local_session_pin(self, role: PortRole) -> bytes | None:
        """The fingerprint confirmed in this dialog for this address and port."""
        if self._local_edit is None or self._local_confirmed is None:
            return None
        return self._local_confirmed.get(
            self._local_pin_key(self._local_edit.context, role)
        )

    async def _local_pause(self) -> None:
        if self._local_record is not None:
            records = await enrollment_journal(self.hass).records()
            record = next(
                (
                    r
                    for r in records
                    if r.attempt_id == self._local_record.attempt_id and not r.resolved
                ),
                None,
            )
            if record is None:
                raise ConfigurationError("enrollment_context_changed")
            self._local_record = record
            if await enrollment_journal(self.hass).remaining_pause(record):
                raise ConfigurationError("enrollment_retry_later")

    async def _local_budget(self) -> LocalReadBudget:
        assert self._local_edit is not None
        return await local_budget_manager(self.hass).budget(
            self._local_edit.context, target_id=self._local_edit.target_id
        )

    async def _local_inspect(self, role: PortRole) -> None:
        await self._local_pause()
        assert self._local_edit is not None
        observation = await inspect_local_certificate(
            self._local_edit.context,
            role,
            is_current=self._local_active,
            budget=await self._local_budget(),
        )
        self._local_observation = observation
        if observation.sha256.hex() == self._local_previous_pin(
            role
        ) or observation.sha256 == self._local_session_pin(role):
            await self._local_accept_certificate()
        else:
            self._local_phase = "local_certificate"

    async def async_step_local_certificate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if denied := self._local_guard("local_certificate"):
            return denied
        observation = self._local_observation
        if observation is None or not self._local_active():
            return self.async_abort(reason="local_session_changed")
        if user_input is not None and user_input.get("confirm") is True:
            return await self._local_start(self._local_accept_certificate)
        return self._local_form(
            "local_certificate",
            vol.Schema({vol.Required("confirm", default=False): BooleanSelector()}),
            placeholders={
                "host": markdown_literal(self._local_address),
                "port": str(observation.role.port),
                "fingerprint": observation.sha256.hex(),
                "previous": self._local_previous_pin(observation.role) or "?",
            },
        )

    async def _local_accept_certificate(self) -> None:
        observation = self._local_observation
        assert observation is not None and self._local_edit is not None
        if observation.context != self._local_edit.context or not self._local_active():
            raise ConfigurationError("configuration_changed")
        binding = CertificateBinding(
            observation.context, observation.role, observation.sha256
        )
        if self._local_confirmed is None:
            self._local_confirmed = {}
        self._local_confirmed[
            self._local_pin_key(observation.context, observation.role)
        ] = observation.sha256
        if observation.role == PortRole.AUTHENTICATION:
            self._local_auth_pin = binding
            self._local_phase = "local_auth"
        else:
            self._local_resource_pin = binding
            await self._local_verify()

    async def async_step_local_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if denied := self._local_guard("local_auth"):
            return denied
        errors = {}
        if user_input is None and self._local_error:
            # The reason of the rejected attempt before (FLOW-5).
            errors["base"] = self._local_error
            self._local_error = None
        if user_input is not None:
            if user_input.get("unlocked") is not True:
                errors["unlocked"] = "local_unlock_required"
            else:
                try:
                    label = LabelCredentials(
                        user_input.get("login", ""), user_input.get("password", "")
                    )
                except ValueError:
                    errors["base"] = "local_invalid_credentials"
                else:
                    # The retry callback never retains device-label credentials.
                    result = await self._local_start(lambda: self._local_create(label))
                    self._local_retry = self._local_resume_pending
                    return result
        secret = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
        return self._local_form(
            "local_auth",
            vol.Schema(
                {
                    vol.Required("login"): secret,
                    vol.Required("password"): secret,
                    vol.Required("unlocked", default=False): BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def _local_create(self, label: LabelCredentials) -> None:
        assert self._local_edit is not None and self._local_auth_pin is not None
        journal = enrollment_journal(self.hass)
        self._local_record = await journal.create(
            self._local_edit,
            label,
            self._local_auth_pin,
            current=self._local_management().current,
            is_active=self._local_active,
        )
        if self._local_record.state != AuthState.RECEIVED:
            self._local_error = local_error(self._local_record.problem)
            if self._local_record.resolved:
                # FLOW-5: certainly no token; sign in again right away with
                # the certificate already confirmed.
                self._local_record = None
                self._local_phase = "local_auth"
                return
            self._local_phase = "local_pending"
            return
        await self._local_inspect(PortRole.RESOURCES)

    async def async_step_local_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if denied := self._local_guard("local_token"):
            return denied
        errors = {}
        if user_input is not None:
            assert self._local_edit is not None
            try:
                self._local_record = await enrollment_journal(self.hass).import_token(
                    self._local_edit,
                    user_input.get("token"),
                    current=self._local_management().current,
                    is_active=self._local_active,
                )
            except LocalCredentialError:
                errors["token"] = "local_invalid_token"
            except ValueError as error:
                errors["base"] = reported_local_error(error)
            else:
                return await self._local_start(
                    lambda: self._local_inspect(PortRole.RESOURCES)
                )
        return self._local_form(
            "local_token",
            vol.Schema(
                {
                    vol.Required("token"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    async def _local_resume_pending(self) -> None:
        records = await enrollment_journal(self.hass).records()
        self._local_record = next(
            (
                r
                for r in records
                if r.owner_id == self._local_owner_id
                and r.target_id == self._local_target
                and not r.resolved
            ),
            None,
        )
        self._local_phase = "local_pending" if self._local_record else "local_auth"

    async def async_step_local_pending(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if denied := self._local_guard("local_pending"):
            return denied
        record = self._local_record
        if record is None:
            return self.async_abort(reason="local_session_changed")
        journal = enrollment_journal(self.hass)
        try:
            current = self._local_management().current()
            status = await journal.recovery_status(record.attempt_id, current)
            if status == RecoveryStatus.APPLIED:
                dismiss_configured_discoveries(self.hass)
                return self.async_abort(reason="local_setup_complete")
            actions = (
                ["continue", "discard", "later"]
                if status == RecoveryStatus.READY
                else ["discard", "later"]
            )
            errors = {"base": self._local_error} if self._local_error else {}
            if user_input is not None:
                action = user_input.get("action")
                if action == "later":
                    return self.async_abort(reason="local_pending_saved")
                if action == "discard":
                    if user_input.get("confirm") is not True:
                        errors["confirm"] = "local_confirmation_required"
                    else:
                        await journal.discard(
                            record.attempt_id, current.owner_id, confirmed=True
                        )
                        self._local_record = None
                        self._local_edit = None
                        self._local_resource_pin = self._local_auth_pin = None
                        self._local_phase = "local_address"
                        return await self.async_step_local_address()
                elif action == "continue" and status == RecoveryStatus.READY:
                    resumed = await journal.resume(record.attempt_id, current)
                    self._local_edit = resumed.edit
                    self._local_address = resumed.edit.context.endpoint.host
                    self._local_auth_pin = resumed.authentication_binding
                    return await self._local_start(
                        lambda: self._local_inspect(PortRole.RESOURCES)
                    )
                elif action not in actions:
                    return self.async_abort(reason="local_session_changed")
        except ValueError as error:
            return self.async_abort(
                reason="local_storage_failed"
                if local_error(error) == "local_storage_failed"
                else "local_session_changed"
            )
        if status == RecoveryStatus.CONFLICT:
            errors["base"] = "local_configuration_changed"
        return self._local_form(
            "local_pending",
            vol.Schema(
                {
                    vol.Required("action"): translated_selection(
                        actions, "local_pending_action"
                    ),
                    vol.Optional("confirm", default=False): BooleanSelector(),
                }
            ),
            errors=errors,
            placeholders={
                "seconds": str(math.ceil(await journal.remaining_pause(record)))
            },
        )

    async def _local_verify(self, *, final: bool = False) -> None:
        await self._local_pause()
        assert (
            self._local_edit is not None
            and self._local_resource_pin is not None
            and self._local_record is not None
            and self._local_record.credentials is not None
        )
        management = self._local_management()
        try:
            proof = await verify_local_setup(
                self._local_edit,
                self._local_record.credentials,
                self._local_resource_pin,
                authentication_binding=self._local_auth_pin,
                current=management.current,
                is_active=self._local_active,
                budget=await self._local_budget(),
            )
        except LocalSetupProbeError as error:
            if error.retry_after_seconds is not None or str(error) in (
                "rate_limited",
                "service_unavailable",
            ):
                await enrollment_journal(self.hass).defer(
                    self._local_record.attempt_id,
                    management.current().owner_id,
                    error.retry_after_seconds
                    if error.retry_after_seconds is not None
                    else 60,
                )
            raise
        support = setup_support(self.hass)
        if support is None:
            raise ConfigurationError("configuration_changed")
        current = management.current()
        contract: ScopedIdentityContract | None
        target = current.targets.get(self._local_edit.target_id)
        if is_new_local_plant(current, self._local_edit.target_id) or (
            target is not None and target.kind == "local_installation"
        ):
            # P49b step 7: a plant without a cloud (new or kept); no source is
            # compared, a kept one must report its own local identity.
            contract = ScopedIdentityContract(
                current.owner_id,
                self._local_edit.target_id,
                proof.access.gateway_id,
                LOCAL_FIRST,
            )
        elif support.verify_identity is not None:
            contract = await support.verify_identity(
                current, self._local_edit.target_id, proof
            )
        else:
            contract = support.identity_contract(
                current, self._local_edit.target_id, proof.access.gateway_id
            )
        if contract is None:
            raise ConfigurationError("identity_contract_required")
        pairs = contract.verified_pairs(
            management.current(), self._local_edit.target_id, proof.access.gateway_id
        )
        try:
            prepare_local_configuration(
                self._local_edit,
                proof,
                cloud_snapshots(self.hass),
                verified_namespace_pairs=pairs,
                contract_id=contract.contract_id,
            )
        except ConfigurationError as error:
            if str(error) == "local_plant_known":
                # Never a new plant: this attempt can never complete.
                await enrollment_journal(self.hass).discard(
                    self._local_record.attempt_id, current.owner_id, confirmed=True
                )
                self._local_record = None
            raise
        self._local_gateway = proof.access.gateway_id
        if final:
            await complete_local_setup(
                management,
                enrollment_journal(self.hass),
                self._local_record.attempt_id,
                proof,
                contract=contract,
            )
            self._local_phase = "local_done"
            dismiss_configured_discoveries(self.hass)
        else:
            self._local_phase = "local_result"

    async def async_step_local_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if denied := self._local_guard("local_result"):
            return denied
        if user_input is not None and user_input.get("confirm") is True:
            return await self._local_start(lambda: self._local_verify(final=True))
        return self._local_form(
            "local_result",
            vol.Schema({vol.Required("confirm", default=False): BooleanSelector()}),
            placeholders={
                "host": markdown_literal(self._local_address),
                "gateway": markdown_literal(self._local_gateway),
            },
        )

    async def async_step_local_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if denied := self._local_guard("local_retry"):
            return denied
        if self._local_record is not None:
            self._local_phase = "local_pending"
            return await self.async_step_local_pending(user_input)
        if user_input is not None and self._local_retry is not None:
            return await self._local_start(self._local_retry)
        return self._local_form(
            "local_retry", errors={"base": self._local_error or "local_unexpected"}
        )

    async def async_step_local_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if denied := self._local_guard("local_done"):
            return denied
        return self.async_abort(reason="local_setup_complete")

    @callback
    def async_remove(self) -> None:
        if (
            self._local_phase
            and self._local_owner_id
            and not self.context.get("handed_off")
        ):
            # P49b step 7: a closed local setup of an entry still without
            # any access asks to finish it.
            from .managed_entry import sync_setup_issue

            entry = self.hass.config_entries.async_get_entry(self._local_owner_id)
            if entry is not None:
                sync_setup_issue(self.hass, entry)
        forget_discovery_flow(self.hass, self.flow_id)
        self._local_closed = True
        if self._local_task is not None and not self._local_task.done():
            self._local_task.cancel()
        self._local_record = self._local_observation = None
        self._local_edit = None
        self._local_auth_pin = self._local_resource_pin = None
        self._local_confirmed = None
        self._local_retry = None
        super().async_remove()
