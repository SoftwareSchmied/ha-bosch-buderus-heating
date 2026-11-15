"""Z2: native options dialogs for the PointT weekly switch programs.

Choose a program, then a weekday, then change, add or delete its switch
points, or copy a day onto other days. The changes collect in a draft of
the whole week; the last step shows the old and the new days and sends the
week once (``coordinator.async_write_switch_program``). Nothing is sent
before every rule of ``switch_program_edit`` holds.

Every program kind of ``switch_programs.KINDS`` is offered where its rules
allow a change; an absolute program (temperatures) gets a number field
instead of the level choice. The hot water week of an RRC room controller
(TP-19) is opened from the RRC dialog and uses the same steps with its own
write. The week step shows a read-only weekly view (TP-07) next to the text.

The programs of an air conditioner on a PointT gateway are offered with the
same steps; a switch point there has a mode, a temperature and a fan speed
(``aircon_program_rules``) and is written by the unit's own coordinator.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.translation import async_get_translations

from .aircon import AirconCoordinator
from .aircon_entity import option_key
from .aircon_program_rules import (
    OFF,
    AirconRules,
    Setting,
    canonical_setting,
    parse_setting,
)
from .const import DOMAIN
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import grouped_entity_name
from .entry_sources import pointt_coordinators
from .ha_schema import validation as vol
from .markdown_text import markdown_literal
from .name_translations import name_part
from .notifications import installation_name
from .options_navigation import (
    BACK,
    BACK_CHOICE,
    NAVIGATION,
    NavigatingOptionsFlow,
)
from .pointt import (
    AuthenticationError,
    PointTError,
    RateLimited,
    Resource,
    UnexpectedHttpStatus,
    WriteNotConfirmed,
    WriteValidationError,
)
from .resource_catalog import resource_name
from .schedule_week import ScheduleWeekSelector, week_days
from .switch_program_edit import (
    EditableProgram,
    circuit_path,
    copy_day,
    day_points,
    editable_program,
    kind_key,
    replace_day,
    same_points,
    setpoint_text,
    value_issue,
    week_issue,
)
from .switch_program_selection import program_name
from .switch_program_sensor import sensor_kind
from .switch_programs import (
    DAYS,
    SwitchPoint,
    is_switch_program_path,
)
from .write_policies import ContractRejected
from .writes import ControlUnused, WriteBusy

_TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])(?::00)?")
# The dialog error of each rule of ``week_issue``.
_ISSUE_ERRORS = {
    "empty_week": "switch_program_empty_week",
    "invalid_time": "switch_program_time",
    "off_raster": "switch_program_time",
    "duplicate_time": "switch_program_duplicate_time",
    "too_many_per_day": "switch_program_too_many_per_day",
    "too_many_in_all": "switch_program_too_many_in_all",
    "level_not_allowed": "switch_program_level",
    "value_not_allowed": "switch_program_value",
    "value_out_of_range": "switch_program_value",
    "value_off_step": "switch_program_value",
    "off_not_offered": "switch_program_value",
    "day_emptied": "switch_program_day_emptied",
}
_ACTIONS = ("edit", "copy", "review")
# The dialog error of a refusal before sending, by its token.
_REFUSAL_ERRORS = {
    "circuit_breaker": "switch_program_paused",
    "program_mode_unknown": "switch_program_mode",
    "program_mode_not_level": "switch_program_mode",
    **{f"program_{issue}": error for issue, error in _ISSUE_ERRORS.items()},
}


def _refusal_error(err: WriteValidationError) -> str:
    """The dialog error of a write refused before anything was sent."""
    if isinstance(err, ControlUnused):
        return "switch_program_unused"
    if isinstance(err, ContractRejected) and err.reason in _REFUSAL_ERRORS:
        return _REFUSAL_ERRORS[err.reason]
    # Changed meanwhile, no current reading or no longer editable.
    return "switch_program_changed"


def aircons_of(entry: ConfigEntry[Any]) -> tuple[AirconCoordinator, ...]:
    """The air conditioners the entry currently runs."""
    runtime = getattr(entry, "runtime_data", None)
    legacy = getattr(runtime, "aircons", None)
    if isinstance(legacy, tuple):
        return legacy
    connections = getattr(runtime, "connections", None)
    snapshot = getattr(connections, "snapshot", None)
    if connections is None or snapshot is None:
        return ()
    return tuple(
        value
        for key in tuple(snapshot.bindings)
        if isinstance(value := connections.display_data(key), AirconCoordinator)
    )


@dataclass(frozen=True, slots=True)
class ProgramChoice:
    coordinator: BoschBuderusDataUpdateCoordinator | AirconCoordinator
    resource: Resource
    program: EditableProgram
    label: str
    # "dhw", "inactive" (another program of the circuit is active) or "active".
    hint: str
    # The translations of the level names: ``<category>.<key>`` whose
    # ``state``/``options`` hold one name per level.
    labels_key: str = "entity.sensor.heating_switch_program.state"
    # The write of a program that is not a PointT switch program (RRC hot
    # water); ``None`` writes through ``async_write_switch_program``.
    write: Callable[[tuple[SwitchPoint, ...]], Awaitable[object]] | None = None
    # A day without points has no level (RRC).
    mark_empty_days: bool = False


def _number_label(level: str, decimal: str) -> str:
    """A setpoint number with the language's decimal sign, e.g. ``21,5 °C``."""
    try:
        number = float(level)
    except ValueError:
        return level
    return f"{number:g}".replace(".", decimal or ".") + " °C"


def _program_letter(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _day_key(day: int) -> str:
    return DAYS[day].lower()


class SwitchProgramOptionsFlow(NavigatingOptionsFlow):
    """Edit an EMS/iCom weekly program as a draft and send it once."""

    _entry: ConfigEntry
    _sp_choice: ProgramChoice | None = None
    _sp_offered: dict[str, ProgramChoice] | None = None
    _sp_draft: tuple[SwitchPoint, ...] = ()
    _sp_day: int | None = None
    _sp_point: int | None = None

    def _switch_program_choices(self) -> dict[str, ProgramChoice]:
        coordinators = pointt_coordinators(self._entry) or ()
        result: dict[str, ProgramChoice] = {}
        for number, coordinator in enumerate(coordinators, 1):
            data = coordinator.data or {}
            if not any(is_switch_program_path(path) for path in data):
                continue
            readings = coordinator.switch_program_readings()
            language = coordinator.hass.config.language
            for path in sorted(data):
                snapshot = data[path]
                if (
                    not is_switch_program_path(path)
                    or not snapshot.available
                    or snapshot.freshness is not Freshness.FRESH
                ):
                    continue
                program = editable_program(
                    snapshot.resource,
                    readings,
                    program_mode=coordinator.switch_program_mode(path),
                )
                if program is None:
                    continue
                letter = _program_letter(path)
                circuit = circuit_path(path)
                kind = kind_key(path)
                label = grouped_entity_name(
                    coordinator, path, resource_name(path, language=language)
                )
                name = (
                    program_name(readings.get(f"{circuit}/switchPrograms/name{letter}"))
                    if kind == "heating"
                    else None
                )
                if name is not None:
                    label = f"{label} ({name})"
                if len(coordinators) > 1:
                    # The system by the name the user knows.
                    system = installation_name(
                        self.hass,
                        self._entry.entry_id,
                        coordinator.gateway.gateway_id,
                        number,
                    )
                    label = f"{system} / {label}"
                if kind == "dhw":
                    hint = "dhw"
                elif kind == "heating":
                    active = readings.get(f"{circuit}/activeSwitchProgram")
                    hint = (
                        "inactive"
                        if active is not None
                        and isinstance(active.value, str)
                        and active.value != letter
                        else "active"
                    )
                else:
                    hint = "active"
                result[f"{number}:{path}"] = ProgramChoice(
                    coordinator,
                    snapshot.resource,
                    program,
                    label,
                    hint,
                    labels_key=(
                        f"entity.sensor.{sensor_kind(snapshot.resource).translation_key}"
                        ".state"
                    ),
                )
        result.update(self._aircon_program_choices())
        return result

    def _aircon_program_choices(self) -> dict[str, ProgramChoice]:
        """The programs of the entry's air conditioners that may be changed."""
        result: dict[str, ProgramChoice] = {}
        language = self.hass.config.language
        for number, coordinator in enumerate(aircons_of(self._entry), 1):
            readings = coordinator.program_readings()
            for path in coordinator.program_paths():
                resource = coordinator.resource(path)
                program = (
                    editable_program(resource, readings)
                    if resource is not None
                    else None
                )
                if resource is None or program is None:
                    continue
                device = installation_name(
                    self.hass,
                    self._entry.entry_id,
                    coordinator.gateway.gateway_id,
                    number,
                )
                label = name_part(language, "name_part_program_aircon", number=path[-1])
                active = coordinator.program_active(path)
                result[f"aircon:{number}:{path}"] = ProgramChoice(
                    coordinator,
                    resource,
                    program,
                    f"{device} / {label}",
                    "aircon_inactive" if active is False else "aircon",
                    labels_key="entity.sensor.aircon_switch_program.state",
                    write=partial(coordinator.async_write_switch_program, resource),
                )
        return result

    def _sp_extra_choices(self) -> dict[str, ProgramChoice]:
        """Programs edited with these steps but chosen elsewhere (RRC hot
        water, TP-19); the RRC dialog provides them."""
        return {}

    def _sp_current(self) -> ProgramChoice | None:
        """The chosen program while it is still shown unchanged."""
        choice = self._sp_choice
        if choice is None or not any(
            current.coordinator is choice.coordinator
            and current.resource == choice.resource
            for current in (
                *self._switch_program_choices().values(),
                *self._sp_extra_choices().values(),
            )
        ):
            return None
        return choice

    async def _sp_labels(self) -> tuple[dict[str, str], dict[str, str]]:
        """The translated weekday and level names of this language."""
        language = self.hass.config.language
        prefix = f"component.{DOMAIN}."
        selectors = await async_get_translations(
            self.hass, language, "selector", {DOMAIN}
        )
        entities = await async_get_translations(self.hass, language, "entity", {DOMAIN})
        translations = {**selectors, **entities}
        days = {
            key: selectors.get(f"{prefix}selector.rrc_weekday.options.{key}", key)
            for key in (_day_key(day) for day in range(len(DAYS)))
        }
        choice = self._sp_choice
        if choice is None:
            return days, {}
        program = choice.program
        shown = {
            *program.levels,
            *(point.level for point in program.points),
            *(point.level for point in self._sp_draft),
        }
        if program.aircon is not None:
            decimal = selectors.get(
                f"{prefix}selector.switch_program_value.options.decimal_sign", "."
            )
            levels = {
                level: self._aircon_label(level, translations, decimal)
                for level in shown
            }
        elif program.numeric:
            off = selectors.get(
                f"{prefix}selector.switch_program_value.options.off", "off"
            )
            # The decimal sign of the language, from its translation file.
            decimal = selectors.get(
                f"{prefix}selector.switch_program_value.options.decimal_sign", "."
            )
            levels = {
                level: off if level in ("0.0", "off") else _number_label(level, decimal)
                for level in shown
            }
        else:
            levels = {
                level: translations.get(f"{prefix}{choice.labels_key}.{level}", level)
                for level in shown
            }
        return days, levels

    @staticmethod
    def _aircon_label(level: str, translations: dict[str, str], decimal: str) -> str:
        """A setting as text: the translated mode, temperature and fan speed."""
        setting = parse_setting(level)
        if setting is None:
            return level
        prefix = f"component.{DOMAIN}.entity."
        parts = [
            translations.get(
                f"{prefix}sensor.aircon_switch_program.state.{option_key(setting.mode)}",
                setting.mode,
            )
        ]
        if setting.temperature is not None:
            parts.append(_number_label(repr(setting.temperature), decimal))
        if setting.fan is not None:
            parts.append(
                translations.get(
                    f"{prefix}climate.pointt_air_conditioner.state_attributes"
                    f".fan_mode.state.{option_key(setting.fan)}",
                    setting.fan,
                )
            )
        return " · ".join(parts)

    @staticmethod
    def _points_text(
        points: tuple[SwitchPoint, ...],
        levels: dict[str, str],
        *,
        markdown: bool = True,
    ) -> str:
        """The points of a day as text; for a dialog description (Markdown)
        the level names are shown literally (D43: a level may be a raw value
        from the cloud), for a selection label as they are."""
        return (
            ", ".join(
                f"{point.minute // 60:02d}:{point.minute % 60:02d} "
                + (markdown_literal(label) if markdown else label)
                for point in points
                for label in (levels.get(point.level, point.level),)
            )
            or "∅"
        )

    def _sp_placeholders(self, choice: ProgramChoice) -> dict[str, str]:
        program = choice.program
        return {
            # D43: the label holds the program name from the cloud.
            "program": markdown_literal(choice.label),
            "per_day": str(program.max_switch_points_per_day),
            "total": str(program.max_switch_points),
            "raster": str(program.time_raster),
        }

    async def async_step_switch_programs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        choices = self._switch_program_choices()
        if not choices:
            return self.async_abort(reason="no_switch_programs")
        if user_input is not None:
            if user_input.get("program") == BACK:
                return await self._show_menu("init")
            offered = self._sp_offered or choices
            choice = offered.get(str(user_input.get("program", "")))
            if choice is None or not any(
                current.coordinator is choice.coordinator
                and current.resource == choice.resource
                for current in choices.values()
            ):
                return self.async_abort(reason="no_switch_programs")
            self._sp_choice = choice
            self._sp_draft = choice.program.points
            return await self.async_step_switch_program_week()
        self._sp_offered = choices
        return self.async_show_form(
            step_id="switch_programs",
            data_schema=vol.Schema(
                {
                    vol.Required("program"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                BACK_CHOICE,
                                *(
                                    SelectOptionDict(value=key, label=choice.label)
                                    for key, choice in choices.items()
                                ),
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key=NAVIGATION,
                        )
                    ),
                }
            ),
        )

    async def async_step_switch_program_week(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        choice = self._sp_current()
        if choice is None:
            return self.async_abort(reason="switch_program_changed")
        errors: dict[str, str] = {}
        if user_input is not None:
            action = user_input.get("action")
            day = next(
                (
                    index
                    for index in range(len(DAYS))
                    if _day_key(index) == user_input.get("day")
                ),
                None,
            )
            if action == "review":
                if same_points(self._sp_draft, choice.program.points):
                    errors["base"] = "switch_program_no_changes"
                else:
                    return await self.async_step_switch_program_confirm()
            elif action in ("edit", "copy"):
                if day is None:
                    errors["day"] = "switch_program_day_required"
                else:
                    self._sp_day = day
                    if action == "edit":
                        return await self.async_step_switch_program_day()
                    return await self.async_step_switch_program_copy()
            else:
                errors["action"] = "switch_program_day_required"
        days, levels = await self._sp_labels()
        changed = {
            day
            for day in range(len(DAYS))
            if not same_points(day_points(self._sp_draft, day), choice.program.day(day))
        }
        week = "\n\n".join(
            f"**{days[_day_key(day)]}{' *' if day in changed else ''}:** "
            + self._points_text(day_points(self._sp_draft, day), levels)
            for day in range(len(DAYS))
        )
        step_id = {
            "dhw": "switch_program_week_dhw",
            "inactive": "switch_program_week_inactive",
            "rrc_dhw": "switch_program_week_rrc_dhw",
            "aircon": "switch_program_week_aircon",
            "aircon_inactive": "switch_program_week_aircon_inactive",
        }.get(choice.hint, "switch_program_week")
        # TP-07: the same week as a read-only view; its value is not used.
        view = ScheduleWeekSelector(
            week_days(
                self._sp_draft,
                labels=levels,
                day_names=days,
                carry_over=choice.program.carry_over,
                mark_empty_days=choice.mark_empty_days,
                changed=frozenset(changed),
            )
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Optional("week_view"): view,
                    vol.Required("action"): SelectSelector(
                        SelectSelectorConfig(
                            options=list(_ACTIONS),
                            translation_key="switch_program_action",
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional("day"): SelectSelector(
                        SelectSelectorConfig(
                            options=[_day_key(day) for day in range(len(DAYS))],
                            translation_key="rrc_weekday",
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={**self._sp_placeholders(choice), "week": week},
        )

    async def async_step_switch_program_week_inactive(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_switch_program_week(user_input)

    async def async_step_switch_program_week_dhw(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_switch_program_week(user_input)

    async def async_step_switch_program_week_rrc_dhw(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_switch_program_week(user_input)

    async def async_step_switch_program_week_aircon(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_switch_program_week(user_input)

    async def async_step_switch_program_week_aircon_inactive(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_switch_program_week(user_input)

    async def async_step_switch_program_day(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        choice, day = self._sp_current(), self._sp_day
        if choice is None or day is None:
            return self.async_abort(reason="switch_program_changed")
        points = day_points(self._sp_draft, day)
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input.get("point")
            if selected == "done":
                return await self.async_step_switch_program_week()
            if selected == "add":
                if len(points) >= choice.program.max_switch_points_per_day:
                    errors["base"] = "switch_program_too_many_per_day"
                elif len(self._sp_draft) >= choice.program.max_switch_points:
                    errors["base"] = "switch_program_too_many_in_all"
                else:
                    self._sp_point = None
                    return await self.async_step_switch_program_point()
            elif isinstance(selected, str) and selected.isdecimal():
                index = int(selected)
                if index < len(points):
                    self._sp_point = index
                    return await self.async_step_switch_program_point()
                errors["base"] = "switch_program_changed"
            else:
                errors["base"] = "switch_program_changed"
        days, levels = await self._sp_labels()
        options = [
            SelectOptionDict(
                value=str(index),
                label=self._points_text((point,), levels, markdown=False),
            )
            for index, point in enumerate(points)
        ]
        options += [
            SelectOptionDict(value="add", label="+"),
            SelectOptionDict(value="done", label="←"),
        ]
        return self.async_show_form(
            step_id="switch_program_day",
            data_schema=vol.Schema(
                {
                    vol.Required("point"): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            translation_key="switch_program_point",
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                **self._sp_placeholders(choice),
                "day": days[_day_key(day)],
                "points": self._points_text(points, levels),
            },
        )

    async def async_step_switch_program_point(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        choice, day = self._sp_current(), self._sp_day
        if choice is None or day is None:
            return self.async_abort(reason="switch_program_changed")
        points = day_points(self._sp_draft, day)
        index = self._sp_point
        if index is not None and index >= len(points):
            return self.async_abort(reason="switch_program_changed")
        original = points[index] if index is not None else None
        program = choice.program
        errors: dict[str, str] = {}
        if user_input is not None:
            others = [
                point for position, point in enumerate(points) if position != index
            ]
            if index is not None and user_input.get("delete") is True:
                candidate = replace_day(self._sp_draft, day, others)
                issue = week_issue(candidate, program)
                if issue is None:
                    self._sp_draft = candidate
                    return await self.async_step_switch_program_day()
                errors["base"] = _ISSUE_ERRORS.get(issue, "switch_program_invalid")
            else:
                time = user_input.get("time")
                match = _TIME.fullmatch(time) if isinstance(time, str) else None
                level = self._sp_entered_level(program, user_input)
                if match is None:
                    errors["time"] = "switch_program_time"
                elif level is None or value_issue(level, program) is not None:
                    if program.aircon is not None:
                        errors["base"] = "switch_program_aircon_setting"
                    elif program.numeric:
                        errors["value"] = "switch_program_value"
                    else:
                        errors["level"] = "switch_program_level"
                else:
                    minute = int(match[1]) * 60 + int(match[2])
                    candidate = replace_day(
                        self._sp_draft,
                        day,
                        [*others, SwitchPoint(day, minute, str(level))],
                    )
                    issue = week_issue(candidate, program)
                    if issue is None:
                        self._sp_draft = candidate
                        return await self.async_step_switch_program_day()
                    error = _ISSUE_ERRORS.get(issue, "switch_program_invalid")
                    errors["time" if error == "switch_program_time" else "base"] = error
        days, levels = await self._sp_labels()
        time_selector = TextSelector(TextSelectorConfig(type=TextSelectorType.TIME))
        fields: dict[Any, Any] = {}
        if original is None:
            fields[vol.Required("time")] = time_selector
        else:
            fields[
                vol.Required(
                    "time",
                    default=f"{original.minute // 60:02d}:{original.minute % 60:02d}",
                )
            ] = time_selector
        if program.aircon is not None:
            fields.update(
                self._sp_aircon_fields(
                    program.aircon, original, await self._sp_aircon_labels()
                )
            )
        elif program.numeric:
            fields.update(self._sp_number_fields(program, original))
        else:
            level_selector = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=level, label=levels.get(level, level))
                        for level in program.levels
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
            if original is None:
                fields[vol.Required("level")] = level_selector
            else:
                fields[vol.Required("level", default=original.level)] = level_selector
        if original is not None:
            fields[vol.Optional("delete", default=False)] = BooleanSelector()
        schema = vol.Schema(fields)
        return self.async_show_form(
            step_id=(
                "switch_program_point_aircon"
                if program.aircon is not None
                else "switch_program_point_value"
                if program.numeric
                else "switch_program_point"
            ),
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders={
                **self._sp_placeholders(choice),
                **self._sp_range_placeholders(program),
                **(
                    {
                        "settings": self._sp_aircon_settings(
                            program.aircon, await self._sp_aircon_labels()
                        )
                    }
                    if program.aircon is not None
                    else {}
                ),
                "day": days[_day_key(day)],
            },
        )

    async def async_step_switch_program_point_value(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_switch_program_point(user_input)

    async def async_step_switch_program_point_aircon(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_switch_program_point(user_input)

    async def _sp_aircon_labels(self) -> dict[str, str]:
        """The translated mode and fan speed names (``mode.<token>``,
        ``fan.<token>``) and the decimal sign (``decimal``)."""
        language = self.hass.config.language
        prefix = f"component.{DOMAIN}."
        entities = await async_get_translations(self.hass, language, "entity", {DOMAIN})
        selectors = await async_get_translations(
            self.hass, language, "selector", {DOMAIN}
        )
        labels = {
            "decimal": selectors.get(
                f"{prefix}selector.switch_program_value.options.decimal_sign", "."
            )
        }
        for token in (OFF, "auto", "cool", "heat", "dry", "fanOnly"):
            labels[f"mode.{token}"] = entities.get(
                f"{prefix}entity.sensor.aircon_switch_program.state.{option_key(token)}",
                token,
            )
        for token in ("auto", "quiet", "low", "mid", "high", "turbo"):
            labels[f"fan.{token}"] = entities.get(
                f"{prefix}entity.climate.pointt_air_conditioner.state_attributes"
                f".fan_mode.state.{option_key(token)}",
                token,
            )
        return labels

    @staticmethod
    def _sp_aircon_settings(rules: AirconRules, labels: dict[str, str]) -> str:
        """The modes with their temperature ranges and fan speeds, as text."""
        decimal = labels.get("decimal", ".")
        lines: list[str] = []
        for mode in rules.offered:
            rule = rules.modes.get(mode)
            text = labels.get(f"mode.{mode}", mode)
            details: list[str] = []
            if rule is not None and rule.minimum is not None:
                assert rule.maximum is not None and rule.step is not None
                low = f"{rule.minimum:g}".replace(".", decimal)
                high = f"{rule.maximum:g}".replace(".", decimal)
                step = f"{rule.step:g}".replace(".", decimal)
                details.append(f"{low}-{high} °C ({step})")
            if rule is not None and rule.fans:
                details.append(
                    ", ".join(labels.get(f"fan.{fan}", fan) for fan in rule.fans)
                )
            lines.append(f"- {text}" + (f": {'; '.join(details)}" if details else ""))
        return "\n".join(lines)

    @staticmethod
    def _sp_aircon_fields(
        rules: AirconRules, original: SwitchPoint | None, labels: dict[str, str]
    ) -> dict[Any, Any]:
        """The mode, a temperature (where a mode has a range) and a fan speed
        (where a mode lists them)."""
        setting: Setting | None = (
            parse_setting(original.level) if original is not None else None
        )
        fields: dict[Any, Any] = {}
        mode_selector = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=mode, label=labels.get(f"mode.{mode}", mode))
                    for mode in rules.offered
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        if setting is None:
            fields[vol.Required("mode")] = mode_selector
        else:
            fields[vol.Required("mode", default=setting.mode)] = mode_selector
        ranged = [rule for rule in rules.modes.values() if rule.minimum is not None]
        if ranged:
            minimum = min(rule.minimum for rule in ranged if rule.minimum is not None)
            maximum = max(rule.maximum for rule in ranged if rule.maximum is not None)
            step = min(rule.step for rule in ranged if rule.step is not None)
            number = NumberSelector(
                NumberSelectorConfig(
                    min=minimum,
                    max=maximum,
                    step=step,
                    unit_of_measurement="°C",
                    mode=NumberSelectorMode.BOX,
                )
            )
            if setting is not None and setting.temperature is not None:
                fields[vol.Optional("value", default=setting.temperature)] = number
            else:
                fields[vol.Optional("value")] = number
        fans = tuple(
            dict.fromkeys(fan for rule in rules.modes.values() for fan in rule.fans)
        )
        if fans:
            fan_selector = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=fan, label=labels.get(f"fan.{fan}", fan))
                        for fan in fans
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
            if setting is not None and setting.fan is not None:
                fields[vol.Optional("fan", default=setting.fan)] = fan_selector
            else:
                fields[vol.Optional("fan")] = fan_selector
        return fields

    @staticmethod
    def _sp_range_placeholders(program: EditableProgram) -> dict[str, str]:
        if not program.numeric:
            return {}
        return {
            "minimum": f"{program.minimum:g}",
            "maximum": f"{program.maximum:g}",
            "step": f"{program.step:g}",
        }

    @staticmethod
    def _sp_number_fields(
        program: EditableProgram, original: SwitchPoint | None
    ) -> dict[Any, Any]:
        """A number field for a temperature, and "off" where it is offered."""
        assert program.minimum is not None and program.maximum is not None
        assert program.step is not None
        number = NumberSelector(
            NumberSelectorConfig(
                min=program.minimum,
                max=program.maximum,
                step=program.step,
                unit_of_measurement="°C",
                mode=NumberSelectorMode.BOX,
            )
        )
        fields: dict[Any, Any] = {}
        value: float | None = None
        if original is not None and original.level != program.off_value:
            try:
                value = float(original.level)
            except ValueError:
                value = None
        if value is None:
            fields[vol.Optional("value")] = number
        else:
            fields[vol.Optional("value", default=value)] = number
        if program.off_value is not None:
            switched_off = original is not None and original.level == program.off_value
            fields[vol.Optional("off", default=switched_off)] = BooleanSelector()
        return fields

    @staticmethod
    def _sp_entered_level(
        program: EditableProgram, user_input: dict[str, Any]
    ) -> str | None:
        """The setpoint text of the entered level or number, or ``None``.

        For an air conditioner the temperature and the fan speed count only
        where the chosen mode has them (a field left from another mode is
        ignored); ``off`` carries neither.
        """
        if program.aircon is not None:
            mode = user_input.get("mode")
            rule = program.aircon.modes.get(mode) if isinstance(mode, str) else None
            if mode == OFF:
                return OFF
            if rule is None:
                return None
            value = user_input.get("value") if rule.minimum is not None else None
            fan = user_input.get("fan") if rule.fans else None
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int | float)
            ):
                return None
            # Not rounded: an entry off the step is refused, never corrected.
            parts = [rule.mode]
            if value is not None:
                parts.append(repr(float(value)))
            if isinstance(fan, str):
                parts.append(fan)
            return canonical_setting(" ".join(parts))
        if not program.numeric:
            level = user_input.get("level")
            return level if isinstance(level, str) else None
        if program.off_value is not None and user_input.get("off") is True:
            return program.off_value
        return setpoint_text(program, user_input.get("value"))

    async def async_step_switch_program_copy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        choice, day = self._sp_current(), self._sp_day
        if choice is None or day is None:
            return self.async_abort(reason="switch_program_changed")
        errors: dict[str, str] = {}
        targets = [index for index in range(len(DAYS)) if index != day]
        if user_input is not None:
            selected = user_input.get("days")
            chosen = [
                index
                for index in targets
                if isinstance(selected, list) and _day_key(index) in selected
            ]
            if not chosen:
                errors["days"] = "switch_program_day_required"
            else:
                candidate = copy_day(self._sp_draft, day, chosen)
                issue = week_issue(candidate, choice.program)
                if issue is None:
                    self._sp_draft = candidate
                    return await self.async_step_switch_program_week()
                errors["base"] = _ISSUE_ERRORS.get(issue, "switch_program_invalid")
        days, levels = await self._sp_labels()
        return self.async_show_form(
            step_id="switch_program_copy",
            data_schema=vol.Schema(
                {
                    vol.Required("days"): SelectSelector(
                        SelectSelectorConfig(
                            options=[_day_key(index) for index in targets],
                            translation_key="rrc_weekday",
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                **self._sp_placeholders(choice),
                "day": days[_day_key(day)],
                "points": self._points_text(day_points(self._sp_draft, day), levels),
            },
        )

    async def _sp_changes(self, choice: ProgramChoice) -> tuple[str, str]:
        """The changed days before and after, as text for the dialog."""
        days, levels = await self._sp_labels()
        before: list[str] = []
        after: list[str] = []
        for day in range(len(DAYS)):
            old = choice.program.day(day)
            new = day_points(self._sp_draft, day)
            if same_points(old, new):
                continue
            label = days[_day_key(day)]
            before.append(f"**{label}:** {self._points_text(old, levels)}")
            after.append(f"**{label}:** {self._points_text(new, levels)}")
        return "\n\n".join(before), "\n\n".join(after)

    async def async_step_switch_program_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        choice = self._sp_current()
        if choice is None:
            return self.async_abort(reason="switch_program_changed")
        before, after = await self._sp_changes(choice)
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                if choice.write is not None:
                    await choice.write(self._sp_draft)
                else:
                    await choice.coordinator.async_write_switch_program(
                        choice.resource, self._sp_draft
                    )
            except RateLimited:
                errors["base"] = "write_rate_limited"
            except AuthenticationError:
                errors["base"] = "write_authentication_failed"
            except WriteValidationError as err:
                errors["base"] = _refusal_error(err)
            except WriteNotConfirmed:
                errors["base"] = "switch_program_not_confirmed"
            except WriteBusy:
                errors["base"] = "write_busy"
            except UnexpectedHttpStatus:
                errors["base"] = "switch_program_rejected"
            except PointTError:
                errors["base"] = "switch_program_failed"
            else:
                self._sp_choice = None
                # Back to the main menu, with the saved program and the
                # previous switch points as its note.
                return await self.async_return_to_menu(
                    "init",
                    "switch_program_saved",
                    {
                        # D43: the label holds the program name from the cloud.
                        "program": markdown_literal(choice.label),
                        "before": before,
                    },
                )
        return self.async_show_form(
            step_id="switch_program_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                **self._sp_placeholders(choice),
                "before": before,
                "after": after,
            },
            last_step=True,
        )
