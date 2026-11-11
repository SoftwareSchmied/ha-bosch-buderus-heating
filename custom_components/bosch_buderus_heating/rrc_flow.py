"""Native options dialogs for viewing and editing existing RRC switch points."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.helpers.selector import (
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

from . import rrc
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
from .entry_sources import pointt_coordinators
from .ha_schema import validation as vol
from .markdown_text import markdown_literal
from .options_navigation import NavigatingOptionsFlow
from .pointt import (
    AuthenticationError,
    PointTError,
    RateLimited,
    Resource,
    WriteNotConfirmed,
    WriteValidationError,
)
from .rrc_programs import (
    DAYS,
    WEEK,
    Schedule,
    SwitchPoint,
    edit_point,
    parse_schedule,
    program_names,
)
from .writes import WriteBusy


@dataclass(frozen=True, slots=True)
class ScheduleChoice:
    coordinator: BoschBuderusDataUpdateCoordinator
    resource: Resource
    schedule: Schedule
    label: str


def _point_label(point: SwitchPoint) -> str:
    return (
        f"{point.minute // 60:02d}:{point.minute % 60:02d} ({point.temperature:g} °C)"
    )


class RrcScheduleOptionsFlow(NavigatingOptionsFlow):
    """Keep weekly schedules separate from holiday dates and time zones."""

    _entry: ConfigEntry
    _rrc_choice: ScheduleChoice | None = None
    _rrc_point: int | None = None
    _rrc_day: str | None = None
    _rrc_offered: dict[str, ScheduleChoice] | None = None

    def _rrc_choices(self) -> dict[str, ScheduleChoice]:
        coordinators = pointt_coordinators(self._entry)
        if coordinators is None:
            return {}
        result: dict[str, ScheduleChoice] = {}
        for number, coordinator in enumerate(coordinators, 1):
            names = _program_names(coordinator)
            for path, snapshot in (coordinator.data or {}).items():
                match = WEEK.fullmatch(path)
                if (
                    match is None
                    or not snapshot.available
                    or snapshot.freshness is not Freshness.FRESH
                ):
                    continue
                schedule = parse_schedule(snapshot.resource)
                if schedule is None:
                    continue
                # The controller's program name, as in the zone's program
                # select; the number without one. Shown as plain text in
                # the dialog descriptions (markdown_literal there).
                name = names.get(int(match[1]))
                label = f"{name} (#{match[1]})" if name else f"#{match[1]}"
                if len(coordinators) > 1:
                    label = (
                        f"{coordinator.gateway.model or self._entry.title} "
                        f"[{number}] / {label}"
                    )
                result[f"{number}:{path}"] = ScheduleChoice(
                    coordinator, snapshot.resource, schedule, label
                )
        return result

    async def async_step_rrc_programs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        choices = self._rrc_choices()
        if not choices:
            return self.async_abort(reason="no_rrc_schedules")
        if user_input is not None:
            offered = self._rrc_offered or choices
            choice = offered.get(str(user_input.get("program", "")))
            if choice is None or not any(
                current.coordinator is choice.coordinator
                and current.resource == choice.resource
                for current in choices.values()
            ):
                return self.async_abort(reason="no_rrc_schedules")
            self._rrc_choice = choice
            return await self.async_step_rrc_schedule()
        self._rrc_offered = choices
        return self.async_show_form(
            step_id="rrc_programs",
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

    async def async_step_rrc_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        choice = self._rrc_choice
        if choice is None:
            return await self.async_step_rrc_programs()
        if user_input is not None:
            if not choice.schedule.editable or not choice.schedule.points:
                # Only shown: back to the main menu.
                self._rrc_choice = None
                return await self.async_return_to_menu("init")
            day = next(
                (day for day in DAYS if day.lower() == user_input.get("day")), None
            )
            if day not in {point.day for point in choice.schedule.points}:
                return self.async_abort(reason="no_rrc_schedules")
            self._rrc_day = day
            return await self.async_step_rrc_day()
        can_edit = choice.schedule.editable and bool(choice.schedule.points)
        schema = (
            vol.Schema(
                {
                    vol.Required("day"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                day.lower()
                                for day in DAYS
                                if any(
                                    point.day == day for point in choice.schedule.points
                                )
                            ],
                            translation_key="rrc_weekday",
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            )
            if can_edit
            else vol.Schema({})
        )
        return self.async_show_form(
            step_id="rrc_schedule" if can_edit else "rrc_readonly",
            data_schema=schema,
            description_placeholders={
                "program": markdown_literal(choice.label),
                **{
                    day.lower(): ", ".join(
                        _point_label(point)
                        for point in choice.schedule.points
                        if point.day == day
                    )
                    or "∅"
                    for day in DAYS
                },
            },
        )

    async def async_step_rrc_day(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        choice = self._rrc_choice
        if choice is None or self._rrc_day is None:
            return await self.async_step_rrc_programs()
        points = {
            str(index): point
            for index, point in enumerate(choice.schedule.points)
            if point.day == self._rrc_day
        }
        if user_input is not None:
            index = user_input.get("point")
            if index not in points:
                return self.async_abort(reason="no_rrc_schedules")
            self._rrc_point = int(index)
            return await self.async_step_rrc_point()
        return self.async_show_form(
            step_id="rrc_day",
            data_schema=vol.Schema(
                {
                    vol.Required("point"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=index,
                                    label=_point_label(point),
                                )
                                for index, point in points.items()
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            description_placeholders={"program": markdown_literal(choice.label)},
        )

    async def async_step_rrc_readonly(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_rrc_schedule(user_input)

    async def async_step_rrc_point(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        choice, index = self._rrc_choice, self._rrc_point
        if choice is None or index is None:
            return await self.async_step_rrc_programs()
        if choice.coordinator not in (pointt_coordinators(self._entry) or ()):
            return self.async_abort(reason="no_rrc_schedules")
        original = choice.schedule.points[index]
        errors: dict[str, str] = {}
        if user_input is not None:
            time = user_input.get("time")
            temperature = user_input.get("temperature")
            match = (
                re.fullmatch(r"([01][0-9]|2[0-3]):([0-5][0-9])(?::00)?", time)
                if isinstance(time, str)
                else None
            )
            minute = int(match[1]) * 60 + int(match[2]) if match else -1
            if minute < 0 or minute % 5:
                errors["time"] = "rrc_time"
            elif isinstance(temperature, bool) or not isinstance(
                temperature, int | float
            ):
                errors["temperature"] = "rrc_temperature"
            else:
                try:
                    edit_point(choice.schedule, index, minute, temperature)
                except WriteValidationError:
                    errors["base"] = "rrc_point_invalid"
                if not errors:
                    try:
                        await choice.coordinator.async_edit_rrc_program(
                            choice.resource, index, minute, temperature
                        )
                    except RateLimited:
                        errors["base"] = "write_rate_limited"
                    except AuthenticationError:
                        errors["base"] = "write_authentication_failed"
                    except WriteValidationError:
                        errors["base"] = "rrc_changed"
                    except WriteNotConfirmed:
                        errors["base"] = "rrc_not_confirmed"
                    except WriteBusy:
                        errors["base"] = "write_busy"
                    except PointTError:
                        errors["base"] = "rrc_failed"
                    else:
                        self._rrc_choice = None
                        return await self.async_return_to_menu(
                            "init", "rrc_program_saved"
                        )
        schema = vol.Schema(
            {
                vol.Required(
                    "time",
                    default=f"{original.minute // 60:02d}:{original.minute % 60:02d}",
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TIME)),
                vol.Required(
                    "temperature", default=original.temperature
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=5,
                        max=30,
                        step=0.5,
                        unit_of_measurement="°C",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="rrc_point",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders={"program": markdown_literal(choice.label)},
        )


_PROGRAM_NAME = re.compile(rf"^/programs/pg({rrc.RRC_ID})/name$")


def _program_names(coordinator: BoschBuderusDataUpdateCoordinator) -> dict[int, str]:
    """The program names the zone program select shows (same source)."""
    snapshots = coordinator.data or {}
    programs = snapshots.get(rrc.PROGRAM_LIST)
    names: dict[int, Resource | None] = {}
    for path, snapshot in snapshots.items():
        match = _PROGRAM_NAME.fullmatch(path)
        if (
            match is not None
            and snapshot.available
            and snapshot.freshness is Freshness.FRESH
        ):
            names[int(match[1])] = snapshot.resource
    return program_names(programs.resource if programs is not None else None, names)
