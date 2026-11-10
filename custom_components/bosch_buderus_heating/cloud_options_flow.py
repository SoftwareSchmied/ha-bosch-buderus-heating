"""Cloud access and discovery dialogs on the existing configuration owner."""

from __future__ import annotations

import logging
from typing import Any, ClassVar, cast, override

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth_lifecycle import async_wait_for_entry_refresh
from .bacon import flow as bacon_flow
from .bacon.models import BaconError
from .cloud_auth_flow import CloudAuthorizationFlow
from .cloud_configuration import (
    CLAIM_ABORTS,
    CloudDiscovery,
    check_cloud_selection,
    check_new_cloud,
    claim_cloud_setup,
    discover_connection,
    ensure_active_cloud_flow,
    new_cloud_configuration,
    renew_cloud_claim,
    selection_client,
    update_cloud_selection,
)
from .configuration import (
    CLOUD_KIND,
    ConfigurationError,
    ConfigurationSnapshot,
    JsonObject,
)
from .configuration_access import EntryConfiguration
from .connection_flow import ConnectionOptionsFlow
from .local_identity import verify_local_first_merges
from .pointt import (
    AuthenticationError,
    AuthTokens,
    Brand,
    Gateway,
    PointTClient,
    PointTError,
    RateLimited,
)
from .pointt.transport import RateLimitBackoff

_LOGGER = logging.getLogger(__name__)

# P49b step 8: shown on the selection, which stays open; nothing is saved and
# deselecting the system keeps it as it is.
LOCAL_FIRST_ERRORS = frozenset(
    {
        "cloud_local_first_other_entry",
        "cloud_local_first_unverified",
        "cloud_local_first_local_unavailable",
        "cloud_local_first_local_busy",
        "cloud_local_first_conflict",
    }
)


class CloudOptionsFlow(CloudAuthorizationFlow, ConnectionOptionsFlow):
    """Use one bounded sign-in flow and retain the entry throughout it."""

    _AUTH_STEP_ID: ClassVar[str] = "cloud_auth"
    _AUTH_MISSING_REASON: ClassVar[str] = "configuration_changed"
    _cloud_brand: Brand | None = None
    _cloud_tokens: AuthTokens | None = None
    _cloud_before: ConfigurationSnapshot | None = None
    _cloud_discovery: CloudDiscovery | None = None
    _selection_discovery: CloudDiscovery | None = None
    _cloud_backoff: RateLimitBackoff | None = None

    def cloud_add_available(self) -> bool:
        try:
            return not any(
                v.kind == CLOUD_KIND
                for v in self._management().current().connections.values()
            )
        except ConfigurationError:
            return False

    async def async_step_connection_add_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        from .config_flow import BRAND_SCHEMA

        try:
            claim_cloud_setup(
                self.hass, self.flow_id, cast(dict[str, Any], self.context)
            )
            self._cloud_before = self._management().current()
        except ConfigurationError as error:
            return self._claim_abort(error)
        if user_input is None:
            return self.async_show_form(
                step_id="connection_add_cloud", data_schema=BRAND_SCHEMA
            )
        self._cloud_brand = Brand(user_input["brand"])
        self._cloud_backoff = RateLimitBackoff()
        return self._start_authorization()

    def _claim_abort(self, error: ConfigurationError) -> ConfigFlowResult:
        return self.async_abort(
            reason=str(error) if str(error) in CLAIM_ABORTS else "configuration_changed"
        )

    def _cloud_taken_over(self) -> ConfigFlowResult | None:
        if renew_cloud_claim(cast(dict[str, Any], self.context)):
            return None
        return self.async_abort(reason="cloud_setup_taken_over")

    async def async_step_cloud_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The shared sign-in step under the name of this dialog."""
        return await self.async_step_auth(user_input)

    @override
    def _auth_brand(self) -> Brand | None:
        return self._cloud_brand

    @override
    async def _auth_blocked(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult | None:
        if self._cloud_backoff is None:
            return self.async_abort(reason=self._AUTH_MISSING_REASON)
        if user_input is None:
            return None
        try:
            check_new_cloud(self.hass, self.flow_id)
            self._cloud_backoff.raise_if_active()
        except ConfigurationError as error:
            return self._claim_abort(error)
        except RateLimited:
            return self.async_show_form(
                step_id="cloud_retry", errors={"base": "cloud_rate_limited"}
            )
        return None

    @override
    def _auth_service_failed(self, error: PointTError) -> ConfigFlowResult:
        assert self._cloud_backoff is not None
        # The code is spent: the retry step issues a new link.
        self._oauth_flow = None
        if isinstance(error, RateLimited):
            self._cloud_backoff.activate(error.retry_after or 300)
            return self.async_show_form(
                step_id="cloud_retry", errors={"base": "cloud_rate_limited"}
            )
        return self.async_show_form(
            step_id="cloud_retry", errors={"base": "cannot_connect"}
        )

    @override
    async def _auth_succeeded(self, tokens: AuthTokens) -> ConfigFlowResult:
        self._cloud_tokens = tokens
        return await self._discover_new_cloud()

    async def async_step_cloud_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if taken_over := self._cloud_taken_over():
            return taken_over
        if self._cloud_backoff is None or self._cloud_brand is None:
            return self.async_abort(reason="configuration_changed")
        try:
            self._cloud_backoff.raise_if_active()
        except RateLimited:
            return self.async_show_form(
                step_id="cloud_retry", errors={"base": "cloud_rate_limited"}
            )
        if self._cloud_tokens is None:
            return self._show_or_start_authorization()
        return await self._discover_new_cloud()

    async def _discover_new_cloud(self) -> ConfigFlowResult:
        assert self._cloud_tokens is not None and self._cloud_backoff is not None
        try:
            check_new_cloud(self.hass, self.flow_id)
            self._cloud_backoff.raise_if_active()
            session = async_get_clientsession(self.hass)
            gateways = await PointTClient(
                session, self._cloud_tokens.access_token, backoff=self._cloud_backoff
            ).get_gateways()
            devices, account = await bacon_flow.discover(session, self._cloud_tokens)
            self._cloud_discovery = CloudDiscovery(gateways, devices, account)
        except ConfigurationError as error:
            return self._claim_abort(error)
        except AuthenticationError:
            self._cloud_tokens = None
            return self._restart_authorization(error="invalid_auth")
        except BaconError as error:
            if error.category in {"authentication", "account_changed"}:
                self._cloud_tokens = None
                return self._restart_authorization(error="invalid_auth")
            if error.category == "rate_limited":
                self._cloud_backoff.activate(error.retry_after)
            return self.async_show_form(
                step_id="cloud_retry", errors={"base": "cannot_connect"}
            )
        except PointTError:
            return self.async_show_form(
                step_id="cloud_retry", errors={"base": "cannot_connect"}
            )
        if not gateways and not devices:
            return self.async_show_form(
                step_id="cloud_retry", errors={"base": "no_gateways"}
            )
        return await self.async_step_cloud_select()

    async def async_step_cloud_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        from .config_flow import _gateway_schema

        if taken_over := self._cloud_taken_over():
            return taken_over
        if (
            self._cloud_discovery is None
            or self._cloud_tokens is None
            or self._cloud_brand is None
            or self._cloud_before is None
        ):
            return self.async_abort(reason="configuration_changed")
        errors = {}
        if user_input is not None:
            gateways = set(user_input.get("gateway_ids", []))
            devices = set(user_input.get("bacon_device_ids", []))
            try:
                current = self._management().current()
                # Preserve unrelated notification edits, but reject changed ownership.
                if current.to_data() != self._cloud_before.to_data():
                    raise ConfigurationError("configuration_changed")
                check_new_cloud(self.hass, self.flow_id)
                self._cloud_discovery.validate(gateways, devices)
                # 8A-a: a plant of this entry joins the cloud only after a
                # fresh comparison of both systems.
                proofs = await verify_local_first_merges(
                    self.hass,
                    current,
                    gateways,
                    lambda: PointTClient(
                        async_get_clientsession(self.hass),
                        cast(AuthTokens, self._cloud_tokens).access_token,
                        backoff=self._cloud_backoff,
                        request_guard=lambda: ensure_active_cloud_flow(
                            self.hass, self.flow_id
                        ),
                    ),
                    "new_cloud",
                )
                current = self._management().current()
                if current.to_data() != self._cloud_before.to_data():
                    raise ConfigurationError("configuration_changed")
                proposed = new_cloud_configuration(
                    self.hass,
                    self._entry.entry_id,
                    self.flow_id,
                    self._cloud_brand,
                    self._cloud_tokens,
                    self._cloud_discovery,
                    gateways,
                    devices,
                    current,
                    proofs=proofs,
                )
                self._management().connect_cloud(current, proposed)
            except ConfigurationError as error:
                if str(error) == "cloud_selection_expired":
                    self._cloud_discovery = None
                    return self.async_show_form(
                        step_id="cloud_retry", errors={"base": str(error)}
                    )
                if str(error) not in {
                    "select_gateway",
                    "cloud_target_owned",
                    "cloud_selection_expired",
                    "cloud_selection_changed",
                    "cannot_connect",
                    *LOCAL_FIRST_ERRORS,
                }:
                    return self._claim_abort(error)
                errors = {"base": str(error)}
            except BaconError:
                errors = {"base": "invalid_auth"}
            except AuthenticationError:
                self._cloud_tokens = None
                return self._restart_authorization(error="invalid_auth")
            except RateLimited:
                errors = {"base": "cloud_rate_limited"}
            except PointTError:
                errors = {"base": "cannot_connect"}
            else:
                return self.async_create_entry(title="", data=dict(self._entry.options))
        return self.async_show_form(
            step_id="cloud_select",
            data_schema=bacon_flow.extend_schema(
                _gateway_schema(self._cloud_discovery.gateways),
                self._cloud_discovery.devices,
                has_gateways=bool(self._cloud_discovery.gateways),
            ),
            errors=errors,
        )

    async def async_step_connection_discover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        from .config_flow import _gateway_schema

        edit = self._connection_edit
        if edit is None:
            return self.async_abort(reason="configuration_changed")
        errors: dict[str, str] = {}
        try:
            if self._management().edit(edit.connection_id) != edit:
                raise ConfigurationError("configuration_changed")
            config = EntryConfiguration(self._entry, edit.connection_id)
            if self._selection_discovery is None:
                self._selection_discovery = await discover_connection(
                    self._management(),
                    edit,
                    request_guard=lambda: ensure_active_cloud_flow(
                        self.hass, self.flow_id
                    ),
                )
            discovery = self._selection_discovery
            if user_input is not None:
                chosen = set(user_input.get("gateway_ids", []))
                devices = set(user_input.get("bacon_device_ids", []))
                check_cloud_selection(
                    self._management(), edit, discovery, chosen, devices
                )
                try:
                    # 8A-a: a plant of this entry joins the cloud only after
                    # a fresh comparison of both systems. The cloud client is
                    # only created when such a plant is selected.
                    proofs = await verify_local_first_merges(
                        self.hass,
                        self._management().current(),
                        chosen,
                        lambda: selection_client(
                            self._management(),
                            edit.connection_id,
                            request_guard=lambda: ensure_active_cloud_flow(
                                self.hass, self.flow_id
                            ),
                        ),
                        edit.connection_id,
                    )
                    # A token rotation running right now is saved first; the
                    # selection is then built on the saved tokens (AUTH-3).
                    try:
                        await async_wait_for_entry_refresh(
                            self.hass, self._entry.entry_id
                        )
                    except Exception as err:  # the saved tokens are used anyway
                        _LOGGER.debug(
                            "Waiting for a running token renewal failed (%s)",
                            type(err).__name__,
                        )
                    update_cloud_selection(
                        self._management(),
                        edit,
                        discovery,
                        chosen,
                        devices,
                        proofs=proofs,
                    )
                except ConfigurationError as error:
                    if str(error) not in LOCAL_FIRST_ERRORS:
                        raise
                    errors = {"base": str(error)}
                else:
                    return self.async_create_entry(
                        title="", data=dict(self._entry.options)
                    )
            old = config.cloud_data()
        except ConfigurationError as error:
            if str(error) == "cloud_connection_disabled":
                return self.async_abort(reason="cloud_connection_disabled")
            if str(error) in {
                "cloud_selection_expired",
                "cloud_selection_changed",
                "cloud_target_owned",
                "cannot_connect",
            }:
                self._selection_discovery = None
                return self.async_show_form(
                    step_id="connection_discover_retry", errors={"base": str(error)}
                )
            return self.async_abort(reason="configuration_changed")
        except AuthenticationError:
            return await self.async_step_connection_reauthenticate()
        except RateLimited as error:
            return self._discover_paused(error)
        except BaconError as error:
            if error.category in {"authentication", "account_changed"}:
                return await self.async_step_connection_reauthenticate()
            return self.async_show_form(
                step_id="connection_discover_retry", errors={"base": "cannot_connect"}
            )
        except PointTError:
            return self.async_show_form(
                step_id="connection_discover_retry", errors={"base": "cannot_connect"}
            )
        gateways = {g.gateway_id: g for g in discovery.gateways}
        for gateway in old["gateway_ids"]:
            gateways.setdefault(gateway, Gateway(gateway))
        selected = {
            (b.adapter, str(cast(JsonObject, b.to_data()["locator"])["value"]))
            for key, b in self._management().current().bindings.items()
            if b.connection_id == edit.connection_id
            and self._entry.options["bindings"][key]["enabled"]
        }
        schema = _gateway_schema(
            tuple(gateways.values()),
            selected=[v for v in old["gateway_ids"] if ("pointt", v) in selected],
        )
        return self.async_show_form(
            step_id="connection_discover",
            data_schema=bacon_flow.extend_schema(
                schema,
                tuple(sorted(set(discovery.devices) | set(old["bacon_device_ids"]))),
                selected=[
                    v for v in old["bacon_device_ids"] if ("bacon", v) in selected
                ],
                has_gateways=bool(gateways),
            ),
            errors=errors,
        )

    def _discover_paused(self, error: RateLimited) -> ConfigFlowResult:
        """A cloud pause, with its end when known; nothing was saved."""
        from datetime import UTC, datetime, timedelta

        from homeassistant.util import dt as dt_util

        self._selection_discovery = None
        seconds = error.retry_after
        if seconds is None or seconds <= 0:
            return self.async_show_form(
                step_id="connection_discover_retry",
                errors={"base": "cloud_rate_limited"},
            )
        until = dt_util.as_local(datetime.now(UTC) + timedelta(seconds=seconds))
        return self.async_show_form(
            step_id="connection_discover_retry",
            errors={"base": "cloud_rate_limited_until"},
            description_placeholders={
                "until": until.strftime(
                    "%H:%M"
                    if until.date() == dt_util.now().date()
                    else "%Y-%m-%d %H:%M"
                )
            },
        )

    async def async_step_connection_discover_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._selection_discovery = None
        return await self.async_step_connection_discover()
