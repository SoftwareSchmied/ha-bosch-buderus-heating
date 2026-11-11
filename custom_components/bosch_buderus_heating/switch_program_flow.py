"""Z2: native options dialogs for the EMS/iCom weekly switch programs.

Choose a program, then a weekday, then change, add or delete its switch
points, or copy a day onto other days. The changes collect in a draft of
the whole week; the last step shows the old and the new days and sends the
week once (``coordinator.async_write_switch_program``). Nothing is sent
before every rule of ``switch_program_edit`` holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .device import grouped_entity_name
from .entry_sources import pointt_coordinators
from .ha_schema import validation as vol
from .markdown_text import markdown_literal
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
from .switch_program_edit import (
    EditableProgram,
    circuit_path,
    copy_day,
    day_points,
    editable_program,
    replace_day,
    same_points,
    week_issue,
)
from .switch_program_selection import program_name
from .switch_programs import (
    DAYS,
    SwitchPoint,
    is_dhw_switch_program_path,
    is_switch_program_path,
)
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
}
_ACTIONS = ("edit", "copy", "review")


@dataclass(frozen=True, slots=True)
class ProgramChoice:
    coordinator: BoschBuderusDataUpdateCoordinator
    resource: Resource
    program: EditableProgram
    label: str
    # "dhw", "inactive" (another program of the circuit is active) or "active".
    hint: str


def _program_letter(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _day_key(day: int) -> str:
    return DAYS[day].lower()


class SwitchProgramOptionsFlow(OptionsFlow):
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
                label = grouped_entity_name(
                    coordinator, path, resource_name(path, language=language)
                )
                name = program_name(
                    readings.get(f"{circuit}/switchPrograms/name{letter}")
                )
                if name is not None:
                    label = f"{label} ({name})"
                if len(coordinators) > 1:
                    label = (
                        f"{coordinator.gateway.model or self._entry.title} "
                        f"[{number}] / {label}"
                    )
                if is_dhw_switch_program_path(path):
                    hint = "dhw"
                else:
                    active = readings.get(f"{circuit}/activeSwitchProgram")
                    hint = (
                        "inactive"
                        if active is not None
                        and isinstance(active.value, str)
                        and active.value != letter
                        else "active"
                    )
                result[f"{number}:{path}"] = ProgramChoice(
                    coordinator, snapshot.resource, program, label, hint
                )
        return result

    def _sp_current(self) -> ProgramChoice | None:
        """The chosen program while it is still shown unchanged."""
        choice = self._sp_choice
        if choice is None or not any(
            current.coordinator is choice.coordinator
            and current.resource == choice.resource
            for current in self._switch_program_choices().values()
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
        days = {
            key: selectors.get(f"{prefix}selector.rrc_weekday.options.{key}", key)
            for key in (_day_key(day) for day in range(len(DAYS)))
        }
        choice = self._sp_choice
        family = (
            "dhw_switch_program"
            if choice is not None and is_dhw_switch_program_path(choice.resource.path)
            else "heating_switch_program"
        )
        levels = {
            level: entities.get(f"{prefix}entity.sensor.{family}.state.{level}", level)
            for level in (choice.program.levels if choice is not None else ())
        }
        return days, levels

    @staticmethod
    def _points_text(points: tuple[SwitchPoint, ...], levels: dict[str, str]) -> str:
        return (
            ", ".join(
                f"{point.minute // 60:02d}:{point.minute % 60:02d} "
                f"{levels.get(point.level, point.level)}"
                for point in points
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
                                SelectOptionDict(value=key, label=choice.label)
                                for key, choice in choices.items()
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
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
        }.get(choice.hint, "switch_program_week")
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
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
                value=str(index), label=self._points_text((point,), levels)
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
                level = user_input.get("level")
                if match is None:
                    errors["time"] = "switch_program_time"
                elif level not in program.levels:
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
        level_selector = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=level, label=levels.get(level, level))
                    for level in program.levels
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        fields: dict[Any, Any]
        if original is None:
            fields = {
                vol.Required("time"): time_selector,
                vol.Required("level"): level_selector,
            }
        else:
            fields = {
                vol.Required(
                    "time",
                    default=f"{original.minute // 60:02d}:{original.minute % 60:02d}",
                ): time_selector,
                vol.Required("level", default=original.level): level_selector,
                vol.Optional("delete", default=False): BooleanSelector(),
            }
        schema = vol.Schema(fields)
        return self.async_show_form(
            step_id="switch_program_point",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders={
                **self._sp_placeholders(choice),
                "day": days[_day_key(day)],
            },
        )

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
                await choice.coordinator.async_write_switch_program(
                    choice.resource, self._sp_draft
                )
            except RateLimited:
                errors["base"] = "write_rate_limited"
            except AuthenticationError:
                errors["base"] = "write_authentication_failed"
            except ControlUnused:
                errors["base"] = "switch_program_unused"
            except WriteValidationError:
                errors["base"] = "switch_program_changed"
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
                return self.async_abort(
                    reason="switch_program_saved",
                    description_placeholders={
                        "program": choice.label,
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
