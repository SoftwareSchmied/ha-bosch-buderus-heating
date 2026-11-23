"""Native options dialogs for viewing and editing RRC weekly programs.

A heating program: change, add (TP-17) or delete a switch point of one day;
each change is shown before and after and sent once for that day. A day
holds at most six points (a fixed limit, not reported by the controller);
its last point is not deleted. The hot water week of the controller (TP-19)
is opened here and edited with the steps of the weekly programs
(``switch_program_flow``), with its own write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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

from . import rrc
from .const import DOMAIN
from .coordinator import BoschBuderusDataUpdateCoordinator, Freshness
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
    WriteNotConfirmed,
    WriteValidationError,
)
from .rrc_dhw import PROGRAM_WEEK as DHW_PROGRAM_WEEK
from .rrc_dhw_program import editable as editable_dhw_program
from .rrc_programs import (
    APP_MAX_POINTS_PER_DAY,
    DAYS,
    WEEK,
    Schedule,
    SwitchPoint,
    day_body,
    day_change,
    edit_point,
    parse_schedule,
    program_names,
)
from .schedule_week import ScheduleWeekSelector, week_days
from .switch_program_flow import ProgramChoice
from .switch_programs import SwitchPoint as SwitchPoint_
from .switch_programs import SwitchPoint as WeekPoint
from .write_policies import ContractRejected
from .writes import WriteBusy

# The dialog error of a day change the rules refuse, by its token.
_DAY_ERRORS = {
    "rrc_point_limit": "rrc_point_limit",
    "rrc_point_last": "rrc_point_last",
    "rrc_point_duplicate": "rrc_point_duplicate",
}
# The default of a new point: 12:00, 15 °C.
_NEW_MINUTE = 12 * 60
_NEW_TEMPERATURE = 15.0


def _refused_error(coordinator: BoschBuderusDataUpdateCoordinator) -> str:
    """The dialog error of a change refused before anything was sent.

    During the pause after repeated failed readings nothing may be sent;
    saying so is more useful than asking to reopen a program that did not
    change.
    """
    reason, _until = coordinator.control_status()
    return "switch_program_paused" if reason == "protection_pause" else "rrc_changed"


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
    # The checked change (minute, temperature) shown before it is sent.
    _rrc_pending: tuple[int, float] | None = None
    # TP-17: "edit" a point, "add" one (``_rrc_pending``) or "delete" it.
    _rrc_action: str = "edit"
    # TP-19: set here, kept by the weekly program steps (annotations only).
    _sp_choice: ProgramChoice | None
    _sp_draft: tuple[SwitchPoint_, ...]

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
                # The name alone; its number (as "Schedule 3") only without
                # a name or when another program has the same one.
                name = names.get(int(match[1]))
                numbered = name_part(
                    self.hass.config.language,
                    "name_part_program_aircon",
                    number=match[1],
                )
                if not name:
                    label = numbered
                elif list(names.values()).count(name) > 1:
                    label = f"{name} ({numbered})"
                else:
                    label = name
                if len(coordinators) > 1:
                    # The system by the name the user knows.
                    system = installation_name(
                        self.hass,
                        self._entry.entry_id,
                        coordinator.gateway.gateway_id,
                        number,
                    )
                    label = f"{system} / {label}"
                result[f"{number}:{path}"] = ScheduleChoice(
                    coordinator, snapshot.resource, schedule, label
                )
        return result

    def _sp_extra_choices(self) -> dict[str, ProgramChoice]:
        """TP-19: the hot water weeks of the RRC controllers that can be
        changed, for the steps of the weekly programs."""
        coordinators = pointt_coordinators(self._entry)
        if coordinators is None:
            return {}
        result: dict[str, ProgramChoice] = {}
        language = self.hass.config.language
        for number, coordinator in enumerate(coordinators, 1):
            for path, snapshot in sorted((coordinator.data or {}).items()):
                if (
                    DHW_PROGRAM_WEEK.fullmatch(path) is None
                    or not snapshot.available
                    or snapshot.freshness is not Freshness.FRESH
                ):
                    continue
                program = editable_dhw_program(snapshot.resource)
                if program is None:
                    continue
                label = name_part(language, "name_part_program_hot_water")
                if len(coordinators) > 1:
                    system = installation_name(
                        self.hass,
                        self._entry.entry_id,
                        coordinator.gateway.gateway_id,
                        number,
                    )
                    label = f"{system} / {label}"
                resource = snapshot.resource

                async def write(
                    points: tuple[Any, ...],
                    coordinator: BoschBuderusDataUpdateCoordinator = coordinator,
                    resource: Resource = resource,
                ) -> None:
                    await coordinator.async_write_rrc_dhw_program(resource, points)

                result[f"dhw:{number}:{path}"] = ProgramChoice(
                    coordinator,
                    resource,
                    program,
                    label,
                    "rrc_dhw",
                    labels_key="selector.rrc_dhw_level.options",
                    write=write,
                )
        return result

    async def async_step_rrc_programs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        choices = self._rrc_choices()
        dhw = self._sp_extra_choices()
        if not choices and not dhw:
            return self.async_abort(reason="no_rrc_schedules")
        if user_input is not None:
            if user_input.get("program") == BACK:
                return await self._show_menu("init")
            selected = str(user_input.get("program", ""))
            if selected in dhw:
                # TP-19: the steps of the weekly programs take over.
                program = dhw[selected]
                self._sp_choice = program
                self._sp_draft = program.program.points
                # Provided by the weekly program steps of the same options flow.
                step: Any = getattr(self, "async_step_switch_program_week")  # noqa: B009
                result: ConfigFlowResult = await step()
                return result
            offered = self._rrc_offered or choices
            choice = offered.get(selected)
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
                                BACK_CHOICE,
                                *(
                                    SelectOptionDict(value=key, label=choice.label)
                                    for key, choice in choices.items()
                                ),
                                *(
                                    SelectOptionDict(value=key, label=choice.label)
                                    for key, choice in dhw.items()
                                ),
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key=NAVIGATION,
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
            if day is None:
                return self.async_abort(reason="no_rrc_schedules")
            self._rrc_day = day
            return await self.async_step_rrc_day()
        can_edit = choice.schedule.editable and bool(choice.schedule.points)
        view = {vol.Optional("week_view"): await self._rrc_week_view(choice)}
        schema = (
            vol.Schema(
                {
                    **view,
                    vol.Required("day"): SelectSelector(
                        SelectSelectorConfig(
                            # TP-17: every day, also one to add a first point.
                            options=[day.lower() for day in DAYS],
                            translation_key="rrc_weekday",
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            )
            if can_edit
            else vol.Schema(view)
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

    async def _rrc_week_view(self, choice: ScheduleChoice) -> ScheduleWeekSelector:
        """TP-07: the program as a read-only weekly view; a day without
        points is marked as such, never as off."""
        prefix = f"component.{DOMAIN}.selector."
        selectors = await async_get_translations(
            self.hass, self.hass.config.language, "selector", {DOMAIN}
        )
        decimal = selectors.get(
            f"{prefix}switch_program_value.options.decimal_sign", "."
        )
        days = {
            day.lower(): selectors.get(
                f"{prefix}rrc_weekday.options.{day.lower()}", day
            )
            for day in DAYS
        }
        points = tuple(
            WeekPoint(DAYS.index(point.day), point.minute, f"{point.temperature:g}")
            for point in choice.schedule.points
        )
        labels = {
            point.level: point.level.replace(".", decimal) + " °C" for point in points
        }
        return ScheduleWeekSelector(
            week_days(points, labels=labels, day_names=days, mark_empty_days=True)
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
        errors: dict[str, str] = {}
        if user_input is not None:
            index = user_input.get("point")
            if index == "add":
                if len(points) >= APP_MAX_POINTS_PER_DAY:
                    errors["base"] = "rrc_point_limit"
                else:
                    self._rrc_point = None
                    return await self.async_step_rrc_add()
            elif index not in points:
                return self.async_abort(reason="no_rrc_schedules")
            else:
                self._rrc_point = int(index)
                return await self.async_step_rrc_point()
        options = [
            SelectOptionDict(value=index, label=_point_label(point))
            for index, point in points.items()
        ]
        if len(points) < APP_MAX_POINTS_PER_DAY:
            options.append(SelectOptionDict(value="add", label="+"))
        return self.async_show_form(
            step_id="rrc_day",
            data_schema=vol.Schema(
                {
                    vol.Required("point"): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            translation_key="rrc_point_choice",
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders=self._rrc_day_placeholders(choice),
        )

    def _rrc_day_placeholders(self, choice: ScheduleChoice) -> dict[str, str]:
        day = self._rrc_day or ""
        return {
            "program": markdown_literal(choice.label),
            "points": ", ".join(
                _point_label(point) for point in choice.schedule.day_points(day)
            )
            or "∅",
            "per_day": str(APP_MAX_POINTS_PER_DAY),
        }

    async def async_step_rrc_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """TP-17: a new point for the chosen day; shown before it is sent."""
        choice, day = self._rrc_choice, self._rrc_day
        if choice is None or day is None:
            return await self.async_step_rrc_programs()
        errors: dict[str, str] = {}
        if user_input is not None:
            minute, temperature = _entered(user_input, errors)
            if not errors:
                try:
                    day_body(
                        choice.schedule,
                        day,
                        day_change(choice.schedule, day, add=(minute, temperature)),
                    )
                except ContractRejected as err:
                    errors["base"] = _DAY_ERRORS.get(err.reason, "rrc_point_invalid")
                except WriteValidationError:
                    errors["base"] = "rrc_point_invalid"
                if not errors:
                    self._rrc_action = "add"
                    self._rrc_pending = (minute, temperature)
                    return self._rrc_day_confirm_form(choice)
        taken = {point.minute for point in choice.schedule.day_points(day)}
        minute = _NEW_MINUTE
        while minute in taken and minute < 1435:
            # Five minutes later until the time is free.
            minute += 5
        schema = vol.Schema(
            {
                vol.Required(
                    "time", default=f"{minute // 60:02d}:{minute % 60:02d}"
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TIME)),
                vol.Required("temperature", default=_NEW_TEMPERATURE): NumberSelector(
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
            step_id="rrc_add",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders=self._rrc_day_placeholders(choice),
            last_step=False,
        )

    def _rrc_day_confirm_form(
        self, choice: ScheduleChoice, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Before and after of a day with a point added or deleted."""
        day = self._rrc_day or ""
        before = choice.schedule.day_points(day)
        if self._rrc_action == "delete":
            new = day_change(choice.schedule, day, delete=self._rrc_point)
        else:
            assert self._rrc_pending is not None
            new = day_change(choice.schedule, day, add=self._rrc_pending)
        after = sorted(new)
        return self.async_show_form(
            step_id="rrc_confirm",
            data_schema=vol.Schema({}),
            errors=errors or {},
            description_placeholders={
                "program": markdown_literal(choice.label),
                "before": ", ".join(_point_label(point) for point in before) or "∅",
                "after": ", ".join(
                    _point_label(SwitchPoint(day, minute, temperature))
                    for minute, temperature in after
                )
                or "∅",
            },
            last_step=True,
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
        if user_input is not None and user_input.get("delete") is True:
            # TP-17: the last point of a day stays (meaning of an empty day
            # on the controller not known).
            try:
                day_body(
                    choice.schedule,
                    original.day,
                    day_change(choice.schedule, original.day, delete=index),
                )
            except ContractRejected as err:
                errors["base"] = _DAY_ERRORS.get(err.reason, "rrc_point_invalid")
            except WriteValidationError:
                errors["base"] = "rrc_point_invalid"
            if not errors:
                self._rrc_action = "delete"
                self._rrc_day = original.day
                return self._rrc_day_confirm_form(choice)
        elif user_input is not None:
            minute, temperature = _entered(user_input, errors)
            if not errors:
                try:
                    edit_point(choice.schedule, index, minute, temperature)
                except WriteValidationError:
                    errors["base"] = "rrc_point_invalid"
            if not errors:
                # Before and after first, as for the heating schedules;
                # nothing is sent before it is confirmed.
                self._rrc_action = "edit"
                self._rrc_pending = (minute, temperature)
                return self._rrc_confirm_form(choice, original)
        return self._rrc_point_form(choice, original, errors, user_input)

    def _rrc_point_form(
        self,
        choice: ScheduleChoice,
        original: SwitchPoint,
        errors: dict[str, str],
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        deletable = len(choice.schedule.day_points(original.day)) > 1
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
                **(
                    {vol.Optional("delete", default=False): BooleanSelector()}
                    if deletable
                    else {}
                ),
            }
        )
        return self.async_show_form(
            step_id="rrc_point",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders={"program": markdown_literal(choice.label)},
            last_step=False,
        )

    def _rrc_confirm_form(
        self, choice: ScheduleChoice, original: SwitchPoint
    ) -> ConfigFlowResult:
        assert self._rrc_pending is not None
        minute, temperature = self._rrc_pending
        after = SwitchPoint(original.day, minute, temperature)
        return self.async_show_form(
            step_id="rrc_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "program": markdown_literal(choice.label),
                "before": _point_label(original),
                "after": _point_label(after),
            },
            last_step=True,
        )

    async def async_step_rrc_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send the confirmed change once; a refusal goes back to the point."""
        choice, index, pending = self._rrc_choice, self._rrc_point, self._rrc_pending
        if choice is None:
            return await self.async_step_rrc_programs()
        if choice.coordinator not in (pointt_coordinators(self._entry) or ()):
            return self.async_abort(reason="no_rrc_schedules")
        if self._rrc_action in ("add", "delete"):
            return await self._rrc_write_day(choice, user_input)
        if index is None or pending is None:
            return await self.async_step_rrc_programs()
        original = choice.schedule.points[index]
        if user_input is None:
            return self._rrc_confirm_form(choice, original)
        minute, temperature = pending
        self._rrc_pending = None
        errors: dict[str, str] = {}
        try:
            await choice.coordinator.async_edit_rrc_program(
                choice.resource, index, minute, temperature
            )
        except RateLimited:
            errors["base"] = "write_rate_limited"
        except AuthenticationError:
            errors["base"] = "write_authentication_failed"
        except WriteValidationError:
            errors["base"] = _refused_error(choice.coordinator)
        except WriteNotConfirmed:
            errors["base"] = "rrc_not_confirmed"
        except WriteBusy:
            errors["base"] = "write_busy"
        except PointTError:
            errors["base"] = "rrc_failed"
        else:
            self._rrc_choice = None
            return await self.async_return_to_menu("init", "rrc_program_saved")
        entered = {
            "time": f"{minute // 60:02d}:{minute % 60:02d}",
            "temperature": temperature,
        }
        return self._rrc_point_form(choice, original, errors, entered)

    async def _rrc_write_day(
        self, choice: ScheduleChoice, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """TP-17: send the day with the point added or deleted, once."""
        day = self._rrc_day
        if day is None or (self._rrc_action == "add" and self._rrc_pending is None):
            return await self.async_step_rrc_programs()
        if user_input is None:
            return self._rrc_day_confirm_form(choice)
        if self._rrc_action == "delete":
            points = day_change(choice.schedule, day, delete=self._rrc_point)
        else:
            assert self._rrc_pending is not None
            points = day_change(choice.schedule, day, add=self._rrc_pending)
        errors: dict[str, str] = {}
        try:
            await choice.coordinator.async_write_rrc_day(choice.resource, day, points)
        except RateLimited:
            errors["base"] = "write_rate_limited"
        except AuthenticationError:
            errors["base"] = "write_authentication_failed"
        except ContractRejected as err:
            errors["base"] = _DAY_ERRORS.get(
                err.reason, _refused_error(choice.coordinator)
            )
        except WriteValidationError:
            errors["base"] = _refused_error(choice.coordinator)
        except WriteNotConfirmed:
            errors["base"] = "rrc_not_confirmed"
        except WriteBusy:
            errors["base"] = "write_busy"
        except PointTError:
            errors["base"] = "rrc_failed"
        else:
            self._rrc_choice = None
            self._rrc_pending = None
            return await self.async_return_to_menu("init", "rrc_program_saved")
        return self._rrc_day_confirm_form(choice, errors)


def _entered(user_input: dict[str, Any], errors: dict[str, str]) -> tuple[int, float]:
    """The entered time (minutes) and temperature; errors where invalid."""
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
        return minute, 0.0
    if isinstance(temperature, bool) or not isinstance(temperature, int | float):
        errors["temperature"] = "rrc_temperature"
        return minute, 0.0
    return minute, float(temperature)


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
