"""Tabbed appliance controls inside the integration's options dialog."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any, override

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import (
    ObjectSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from . import rrc_contracts
from .const import DOMAIN
from .control_presentation import control_category, control_icon, control_label
from .coordinator import BoschBuderusDataUpdateCoordinator
from .entity_resources import fresh_resource
from .entry_sources import pointt_coordinators
from .enum_translation import enum_value_to_ha
from .ha_schema import validation as vol
from .i18n import LANGUAGES, dialog_config
from .notifications import gateway_notification_key, installation_name
from .pointt import (
    AuthenticationError,
    PointTError,
    RateLimited,
    Resource,
    WriteNotConfirmed,
    WriteValidationError,
)
from .resource_catalog import resource_name
from .rrc_contract_circuit import (
    control_path,
    guard_paths,
    limit_role,
    weather_control_off,
)
from .unit_contracts import DURATION_UNITS, TEMPERATURE_UNITS
from .writes import (
    ContractRejected,
    ControlContext,
    ControlLocked,
    ControlUnused,
    EnumWritePolicy,
    NumberWritePolicy,
    WriteBusy,
    assess_control,
    context_of,
    validate_control_value,
)


class ControlsSelector(ObjectSelector):
    """A draft, with presentation metadata supplied only by the server."""

    selector_type = "bosch-buderus-controls"

    def __init__(
        self,
        rows: list[dict[str, Any]],
        status: str = "",
        *,
        active: str = "",
        invalid: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.rows = rows
        self.status = status
        # HA recreates the selector after every step, so the open tab and the
        # rejected fields must come with the step itself.
        self.active = active
        self.invalid = invalid or []

    @override
    def serialize(self) -> dict[str, Any]:
        return {
            "selector": {
                self.selector_type: {
                    "rows": self.rows,
                    "status": self.status,
                    "active": self.active,
                    "invalid": self.invalid,
                    **dialog_config("controls"),
                }
            }
        }


# Tabs: heating circuits, then hot water, then the remaining system settings.
_GROUP_RANK = {"heatingCircuits": 0, "dhwCircuits": 1}


def control_group(path: str) -> str:
    """The dialog tab of a control path."""
    parts = path.strip("/").split("/")
    if parts[0] in _GROUP_RANK and len(parts) > 1:
        return "/".join(parts[:2])
    return "system"


def _row_order(path: str) -> tuple[int, int, str, str]:
    """Order circuits naturally, so hc2 precedes hc10."""
    family, _, circuit = control_group(path).partition("/")
    number = re.search(r"\d+$", circuit)
    return (
        _GROUP_RANK.get(family, len(_GROUP_RANK)),
        int(number.group()) if number else 0,
        circuit,
        path,
    )


def controls(coordinator: BoschBuderusDataUpdateCoordinator) -> dict[str, Resource]:
    """Use only discovered scalar controls from the approved heating functions.

    On an RRC controller the contracts' policies apply, and their
    ``dialog_paths`` join (S11).
    """
    context = context_of(coordinator)
    result = {}
    for path in coordinator.data or {}:
        if context.rrc_plant and _weather_control_off(coordinator, path):
            # Offered only while the circuit uses weather-compensated control.
            continue
        if not (
            path.startswith(("/heatingCircuits/", "/dhwCircuits/"))
            or path
            in {
                "/system/awayMode/enabled",
                "/system/silentMode/enabled",
                "/system/seasonOptimizer/mode",
                "/heatSources/additionalHeater/operationMode",
            }
            or (context.rrc_plant and rrc_contracts.in_dialog(path))
        ):
            continue
        resource = fresh_resource(coordinator, path)
        if resource is None:
            continue
        assessment = assess_control(resource, context)
        if (
            assessment.eligible
            and assessment.current_value_issue is None
            and assessment.platform != "button"
        ):
            result[path] = resource
    return result


def _weather_control_off(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> bool:
    control = control_path(path)
    return control is not None and weather_control_off(
        path, fresh_resource(coordinator, control)
    )


# Settings precede activation: a mode, a charge, extra hot water (after its
# duration).
_ACTIVATIONS = ("/operationMode", "/charge", "/extraDhw")
# A heating curve point or supply limit: lowered lower limits first, then
# raised upper ones, lowered upper ones and raised lower ones, so that each
# write keeps every lower limit at or below every upper one.
_LIMIT_PHASES = {
    ("lower", True): 0,
    ("upper", False): 1,
    ("upper", True): 2,
    ("lower", False): 3,
}


def _write_order(path: str, value: object, before: Resource) -> tuple[bool, int, str]:
    """The position of a draft entry among the writes."""
    role = limit_role(path)
    phase = 0
    if (
        role is not None
        and isinstance(value, int | float)
        and isinstance(before.value, int | float)
    ):
        phase = _LIMIT_PHASES[role, value < before.value]
    return path.endswith(_ACTIVATIONS), phase, path


# The contracts whose rules are checked against the whole draft first.
_DRAFT_CHECKED = frozenset({"circuit", "dhw"})
# A contract refusal -> the message of the dialog; others get a general one.
_REJECTED_ERRORS = {
    "rrc_curve_order": "controls_curve_order",
    "rrc_weather_control_off": "controls_weather_control_off",
    "rrc_extra_dhw_duration_zero": "controls_extra_dhw_duration_zero",
}


def _refusal_error(err: WriteValidationError) -> str | None:
    """The message of the dialog for a refusal that names its reason."""
    if isinstance(err, ControlUnused):
        return "controls_unused"
    if isinstance(err, ControlLocked):
        return "controls_locked"
    if isinstance(err, ContractRejected):
        return _REJECTED_ERRORS.get(err.reason, "controls_rejected")
    return None


def _guards(
    coordinator: BoschBuderusDataUpdateCoordinator, path: str
) -> tuple[Resource, ...]:
    """S5: the readings a heating circuit setting is checked against (the
    limits it is ordered against, the control strategy), read again with it
    before the write; a change there sends nothing."""
    if not context_of(coordinator).rrc_plant:
        return ()
    return tuple(
        resource
        for item in guard_paths(path)
        if (resource := fresh_resource(coordinator, item)) is not None
    )


def _draft_readings(
    coordinator: BoschBuderusDataUpdateCoordinator,
    displayed: Mapping[str, Resource],
    draft: Mapping[str, Any],
) -> dict[str, Resource]:
    """The current readings with the draft applied: the state after every
    write, for the contract rules that compare settings."""
    readings = {
        path: resource
        for path in coordinator.data or {}
        if (resource := fresh_resource(coordinator, path)) is not None
    }
    for path, value in draft.items():
        if path in displayed:
            readings[path] = replace(displayed[path], value=value, has_value=True)
    return readings


def _offered_options(
    path: str, options: tuple[str, ...], *, contract: bool
) -> tuple[str, ...]:
    """The options of a row; of two spellings a contract gives one option key
    ("Off" and "off"), only the first."""
    if not contract:
        return options
    keys: set[str] = set()
    offered: list[str] = []
    for raw in options:
        key = rrc_contracts.option_key(path, raw)
        if key is not None and key in keys:
            continue
        if key is not None:
            keys.add(key)
        offered.append(raw)
    return tuple(offered)


def _until_text(until: datetime | None) -> str:
    """Local end of a pause; with the date when it is not today."""
    if until is None:
        return ""
    local = dt_util.as_local(until)
    if local.date() == dt_util.now().date():
        return local.strftime("%H:%M")
    return local.strftime("%Y-%m-%d %H:%M")


def _missing_cloud_reason(entry: ConfigEntry) -> str:
    """Why no cloud appliance is loaded: sign-in, unreachable, starting or none."""
    from .configuration import CLOUD_KIND
    from .connection_runtime import ConnectionPhase
    from .managed_entry import ManagedEntry

    runtime = getattr(entry, "runtime_data", None)
    if not isinstance(runtime, ManagedEntry):
        return "controls_not_loaded"
    connections = runtime.connections
    snapshot = connections.snapshot
    clouds = {
        key for key, item in snapshot.connections.items() if item.kind == CLOUD_KIND
    }
    # First the access itself, then its appliances: a ready access may still
    # discover an appliance, or the appliance may not answer. A deliberately
    # disabled appliance is not "loading".
    for phases in (
        {connections.connection_phase(key) for key in clouds},
        {
            connections.source_phase(key)
            for key, binding in snapshot.bindings.items()
            if binding.connection_id in clouds
        },
    ):
        if ConnectionPhase.AUTH_REQUIRED in phases:
            return "controls_auth_required"
        if ConnectionPhase.UNAVAILABLE in phases:
            return "controls_cloud_unreachable"
        if ConnectionPhase.CONNECTING in phases:
            return "controls_not_current"
    if ConnectionPhase.READY in phases:
        return "controls_not_current"
    # No cloud access, a stopped one or only disabled appliances.
    return "controls_not_loaded"


def _translation_key(path: str) -> str:
    if path.endswith("/operationMode"):
        if path.startswith("/heatingCircuits/"):
            return "heating_circuit_operation_mode"
        if path.startswith("/dhwCircuits/"):
            return "hot_water_operation_mode"
        return "auxiliary_heater_operation_mode"
    if "/silentMode/" in path:
        return "silent_mode"
    if "/seasonOptimizer/" in path:
        return "season_switching"
    return ""


class ControlsOptionsFlow(OptionsFlow):
    """Keep drafts per flow and never store appliance values in entry options."""

    _entry: ConfigEntry
    _control_gateway: str | None = None
    _control_displayed: dict[str, Resource] | None = None
    _control_source: BoschBuderusDataUpdateCoordinator | None = None

    def _control_gateways(self) -> dict[str, BoschBuderusDataUpdateCoordinator]:
        coordinators = pointt_coordinators(self._entry)
        if coordinators is None:
            return {}
        return {
            gateway_notification_key(item.gateway.gateway_id): item
            for item in coordinators
        }

    async def async_step_controls(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        gateways = self._control_gateways()
        if not gateways:
            return self.async_abort(reason=_missing_cloud_reason(self._entry))
        if len(gateways) == 1:
            self._control_gateway = next(iter(gateways))
            self._control_displayed = None
            return await self.async_step_operate()
        if user_input is not None and user_input.get("installation") in gateways:
            self._control_gateway = user_input["installation"]
            self._control_displayed = None
            return await self.async_step_operate()
        choices = [
            SelectOptionDict(
                value=key,
                label=installation_name(
                    self.hass, self._entry.entry_id, item.gateway.gateway_id, index
                )
                + f" ({index})",
            )
            for index, (key, item) in enumerate(gateways.items(), 1)
        ]
        return self.async_show_form(
            step_id="controls",
            data_schema=vol.Schema(
                {
                    vol.Required("installation"): SelectSelector(
                        SelectSelectorConfig(options=choices)
                    )
                }
            ),
        )

    async def async_step_operate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        coordinator = self._control_gateways().get(self._control_gateway or "")
        if coordinator is None:
            return self.async_abort(reason=_missing_cloud_reason(self._entry))
        if user_input is not None and coordinator is not self._control_source:
            # The appliance was reloaded while the dialog was open.
            return self.async_abort(reason="controls_not_current")
        self._control_source = coordinator
        current = controls(coordinator)
        reason, until = coordinator.control_status()
        if user_input is None and (reason is not None or not current):
            # Say why nothing can be operated instead of an empty dialog or
            # controls whose writes would be rejected right away.
            return self.async_abort(
                reason=f"controls_{reason or 'none'}",
                description_placeholders={"until": _until_text(until)},
            )
        if self._control_displayed is None:
            self._control_displayed = current
        errors: dict[str, str] = {}
        draft: dict[str, Any] = {}
        status = ""
        # Rejected values, the path a write stopped at and the last written
        # path select the tab shown with the next step.
        invalid: list[str] = []
        stopped: str | None = None
        written: str | None = None
        shown = ""
        if user_input is not None:
            payload = user_input.get("controls")
            if isinstance(payload, dict) and isinstance(payload.get("active"), str):
                # The tab open in the browser; only a tab name, never written.
                shown = payload["active"]
            if not isinstance(payload, dict) or set(payload) - {
                "changes",
                "action",
                "active",
            }:
                errors["base"] = "controls_invalid"
            elif payload.get("action") == "refresh":
                self._control_displayed = current
            elif payload.get("action", "apply") != "apply" or not isinstance(
                payload.get("changes", {}), dict
            ):
                errors["base"] = "controls_invalid"
            else:
                draft = dict(payload.get("changes", {}))
                for path, value in draft.items():
                    before = self._control_displayed.get(path)
                    if before is None or current.get(path) != before:
                        # The displayed control changed, not the entered value.
                        errors["base"] = "controls_invalid"
                        stopped = stopped or path
                        continue
                    try:
                        validate_control_value(before, value, context_of(coordinator))
                    except WriteValidationError as err:
                        errors["base"] = _refusal_error(err) or "controls_invalid"
                        invalid.append(path)
                if not errors:
                    # The heating circuit and hot water rules that compare
                    # settings, against the state after the whole draft:
                    # nothing is written when the draft as a whole breaks
                    # one (the write order keeps every step in order).
                    readings = _draft_readings(
                        coordinator, self._control_displayed, draft
                    )
                    for path, value in draft.items():
                        if rrc_contracts.contract_for(path) not in _DRAFT_CHECKED:
                            continue
                        before = self._control_displayed[path]
                        policy = assess_control(before, context_of(coordinator)).policy
                        check = policy.value_check if policy is not None else None
                        if check is not None and (
                            reason := check(before, value, readings)
                        ):
                            errors["base"] = _REJECTED_ERRORS.get(
                                reason, "controls_rejected"
                            )
                            invalid.append(path)
                if not errors:
                    # Each write is individually confirmed; the appliance API
                    # has no multi-setting commit.
                    shown_before = self._control_displayed
                    for path in sorted(
                        draft,
                        key=lambda item: _write_order(
                            item, draft[item], shown_before[item]
                        ),
                    ):
                        before = self._control_displayed[path]
                        value = draft[path]
                        if value == before.value:
                            draft.pop(path)
                            continue
                        policy = assess_control(before, context_of(coordinator)).policy
                        assert isinstance(policy, (EnumWritePolicy, NumberWritePolicy))
                        try:
                            confirmed = await coordinator.async_write_control(
                                path,
                                value,
                                policy,
                                expected=(before, *_guards(coordinator, path)),
                            )
                        except AuthenticationError:
                            errors["base"] = "controls_auth"
                        except RateLimited:
                            errors["base"] = "controls_rate_limit"
                        except WriteNotConfirmed:
                            errors["base"] = "controls_unconfirmed"
                        except WriteValidationError as err:
                            # A lock or a contract rule says why; anything
                            # else changed since it was shown.
                            errors["base"] = _refusal_error(err) or "controls_changed"
                        except WriteBusy:
                            # Nothing was sent; the change can be tried again.
                            errors["base"] = "controls_busy"
                        except PointTError:
                            errors["base"] = "controls_failed"
                        else:
                            self._control_displayed[path] = confirmed
                            draft.pop(path)
                            written = path
                        if errors:
                            stopped = path
                            break
                    if not errors:
                        self._control_displayed = controls(coordinator)
                        draft = {}
                        status = "saved"
        rows = await self._control_rows(
            self._control_displayed,
            current=controls(coordinator),
            context=context_of(coordinator),
        )
        if not rows:
            status = "empty"
        invalid.sort(key=_row_order)
        if errors:
            # The first rejected value, else where writing stopped, else the
            # first change still pending.
            preferred = [*invalid, *([stopped] if stopped else []), *draft]
        else:
            # After writing, show the last confirmed change.
            preferred = [written] if written else []
        candidates = [
            control_group(path) for path in preferred if isinstance(path, str)
        ]
        groups = {row["group"] for row in rows}
        active = next((group for group in [*candidates, shown] if group in groups), "")
        return self.async_show_form(
            step_id="operate",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "controls", default={"changes": draft}
                    ): ControlsSelector(rows, status, active=active, invalid=invalid)
                }
            ),
            errors=errors,
            last_step=False,
        )

    async def _control_rows(
        self,
        displayed: dict[str, Resource],
        *,
        current: dict[str, Resource],
        context: ControlContext | None = None,
    ) -> list[dict[str, Any]]:
        translations = {
            language: await async_get_translations(
                self.hass, language, "entity", {DOMAIN}
            )
            for language in LANGUAGES
        }
        rows = []
        for path in sorted(displayed, key=_row_order):
            resource = displayed[path]
            assessment = assess_control(resource, context)
            contract = rrc_contracts.owns(assessment.policy)
            group = control_group(path)
            key = (
                rrc_contracts.entity_key(path, "select") or ""
                if contract
                else _translation_key(path)
            )
            labels = {
                lang: control_label(
                    path, resource_name(path, language=lang), lang, contract=contract
                )
                for lang in translations
            }
            offered = _offered_options(path, assessment.options, contract=contract)
            value = resource.value
            if (
                contract
                and isinstance(assessment.policy, EnumWritePolicy)
                and value not in offered
            ):
                # A reading that confirms an option ("off" for "Off") selects it.
                value = next(
                    (raw for raw in offered if assessment.policy.confirms(value, raw)),
                    value,
                )
            options = [
                {
                    "value": raw,
                    "label": {
                        lang: translations[lang].get(
                            f"component.{DOMAIN}.entity.select.{key}.state."
                            + (
                                (contract and rrc_contracts.option_key(path, raw))
                                or enum_value_to_ha(key, raw)
                            ),
                            raw,
                        )
                        for lang in translations
                    },
                }
                for raw in offered
            ]
            rows.append(
                {
                    "key": path,
                    "group": group,
                    "label": labels,
                    "kind": "select"
                    if assessment.supports_select
                    else assessment.platform,
                    "value": value,
                    "options": options,
                    "minimum": assessment.minimum,
                    "maximum": assessment.maximum,
                    "step": assessment.ui_step,
                    "unit": {**TEMPERATURE_UNITS, **DURATION_UNITS}.get(
                        assessment.unit or "", assessment.unit or ""
                    ),
                    "icon": control_icon(
                        path,
                        numeric=assessment.platform == "number",
                        contract=contract,
                    ),
                    "advanced": control_category(
                        path,
                        numeric=assessment.platform == "number",
                        contract=contract,
                    )
                    is not None
                    and not (
                        path.startswith("/heatingCircuits/")
                        and path.endswith(("/operationMode", "/manualRoomSetpoint"))
                    ),
                    "available": current.get(path) == resource,
                }
            )
        return rows
