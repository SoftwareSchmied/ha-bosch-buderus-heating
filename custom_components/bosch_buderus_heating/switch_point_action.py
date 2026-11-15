"""TP-06: the action ``set_switch_points`` for automations and scripts.

The action targets program sensors (``sensor`` entities of a weekly
program) and sets their switch points as the options dialog would: the same
rules (``switch_program_edit``), the same single write
(``coordinator.async_plan_switch_program``). The base of a change is always a
fresh reading of the program; the write reads it once more right before the
single PUT and refuses when it changed in between. Nothing is retried; an
automation that repeats a call does so on its own account.

Several targets are handled one after the other. A target that fails does
not undo another one that succeeded; the first failure is reported after all
targets were handled.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any, Final

from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service
from homeassistant.helpers.typing import VolDictType

from .const import DOMAIN
from .control import cloud_down_error
from .ha_schema import validation as vol
from .pointt import (
    AuthenticationError,
    PointTError,
    RateLimited,
    Resource,
    UnexpectedHttpStatus,
    WriteNotConfirmed,
    WriteValidationError,
)
from .switch_program_edit import (
    EditableProgram,
    edit_issue,
    editable_program,
    replace_day,
    same_points,
    setpoint_text,
    sorted_points,
    week_issue,
)
from .switch_program_service import SwitchProgramChanged
from .switch_programs import DAYS, SwitchPoint, assess_switch_program
from .write_policies import ContractRejected
from .writes import ControlUnused, WriteBusy

SERVICE_SET_SWITCH_POINTS: Final = "set_switch_points"
MODES: Final = ("replace_week", "replace_days")
DAY_KEYS: Final = tuple(day.lower() for day in DAYS)


def _minute_of_day(value: Any) -> int:
    """``HH:MM`` (seconds ``00`` allowed) as minutes since midnight."""
    clock: dt.time = cv.time(value)
    if clock.second or clock.microsecond:
        raise vol.Invalid("Switch points have whole minutes")
    return clock.hour * 60 + clock.minute


POINT_SCHEMA: Final = vol.Schema(
    {
        vol.Required("day"): vol.In(DAY_KEYS),
        vol.Required("time"): _minute_of_day,
        vol.Required("level"): cv.string,
    },
    extra=vol.PREVENT_EXTRA,
)
SERVICE_SCHEMA: Final[VolDictType] = {
    vol.Optional("mode", default="replace_week"): vol.In(MODES),
    vol.Required("switch_points"): vol.All(cv.ensure_list, [POINT_SCHEMA]),
    vol.Optional("expected"): vol.All(cv.ensure_list, [POINT_SCHEMA]),
    vol.Optional("validate_only", default=False): cv.boolean,
}

# The action error of each rule of ``week_issue`` and ``value_issue``.
_WEEK_ERRORS: Final = {
    "empty_week": "switch_points_empty_week",
    "invalid_time": "switch_points_invalid_time",
    "off_raster": "switch_points_invalid_time",
    "duplicate_time": "switch_points_duplicate_time",
    "too_many_per_day": "switch_points_too_many_per_day",
    "too_many_in_all": "switch_points_too_many_in_all",
    "level_not_allowed": "switch_points_level_not_allowed",
    "value_not_allowed": "switch_points_value_not_allowed",
    "value_out_of_range": "switch_points_value_not_allowed",
    "value_off_step": "switch_points_value_not_allowed",
    "off_not_offered": "switch_points_value_not_allowed",
}
# Why a program refuses any change, by ``edit_issue`` token.
_EDIT_ERRORS: Final = {
    "not_writable": "switch_points_not_writable",
    "invalid_flags": "switch_points_not_writable",
    "unavailable": "switch_points_not_writable",
    "not_used": "switch_points_unused",
    "program_mode_unknown": "switch_points_not_level_program",
    "not_level_program": "switch_points_not_level_program",
    "program_mode_not_absolute": "switch_points_not_level_program",
}


def _error(
    key: str, placeholders: Mapping[str, str] | None = None
) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=dict(placeholders or {}),
    )


def _failure(
    key: str, placeholders: Mapping[str, str] | None = None
) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=dict(placeholders or {}),
    )


@callback
def async_setup_actions(hass: HomeAssistant) -> None:
    """Register the action once, in ``async_setup`` (HA standard)."""
    service.async_register_batched_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_SWITCH_POINTS,
        entity_domain=Platform.SENSOR,
        schema=SERVICE_SCHEMA,
        func=async_set_switch_points,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _is_program_entity(entity: object) -> bool:
    description = getattr(entity, "entity_description", None)
    return getattr(description, "value_kind", None) == "switch_program" and callable(
        getattr(getattr(entity, "coordinator", None), "async_plan_switch_program", None)
    )


async def async_set_switch_points(
    entities: list[Any], call: ServiceCall
) -> dict[str, Any] | None:
    """Handle the targets one after the other; see the module notes."""
    if not entities:
        # Disabled, unavailable or foreign targets are not handed over.
        raise _error("switch_points_no_target")
    if not all(_is_program_entity(entity) for entity in entities):
        raise _error("not_a_program_entity")
    responses: dict[str, Any] = {}
    failure: HomeAssistantError | None = None
    for entity in entities:
        try:
            responses[entity.entity_id] = await _async_set_one(entity, call.data)
        except HomeAssistantError as err:
            failure = failure or err
    if failure is not None:
        raise failure
    return responses


def _requested(points: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    return [
        (DAY_KEYS.index(point["day"]), point["time"], point["level"])
        for point in points
    ]


def _points(
    requested: list[tuple[int, int, str]], program: EditableProgram
) -> tuple[SwitchPoint, ...]:
    """The requested points with setpoints in the program's form."""
    result: list[SwitchPoint] = []
    for day, minute, level in requested:
        text = setpoint_text(program, level)
        if text is None:
            raise _value_error(program)
        result.append(SwitchPoint(day, minute, text))
    return sorted_points(result)


def _value_error(program: EditableProgram) -> ServiceValidationError:
    if program.numeric:
        return _error(
            "switch_points_value_not_allowed",
            {
                "minimum": f"{program.minimum:g}",
                "maximum": f"{program.maximum:g}",
                "step": f"{program.step:g}",
            },
        )
    return _error(
        "switch_points_level_not_allowed", {"levels": ", ".join(program.levels)}
    )


def _week_error(issue: str, program: EditableProgram) -> ServiceValidationError:
    key = _WEEK_ERRORS.get(issue, "switch_points_invalid")
    if key in ("switch_points_level_not_allowed", "switch_points_value_not_allowed"):
        return _value_error(program)
    return _error(
        key,
        {
            "raster": str(program.time_raster),
            "per_day": str(program.max_switch_points_per_day),
            "total": str(program.max_switch_points),
        }
        if key
        in (
            "switch_points_invalid_time",
            "switch_points_too_many_per_day",
            "switch_points_too_many_in_all",
        )
        else None,
    )


def _week_response(
    points: tuple[SwitchPoint, ...],
) -> dict[str, list[dict[str, str]]]:
    days: dict[str, list[dict[str, str]]] = {}
    for point in sorted_points(points):
        days.setdefault(DAY_KEYS[point.day], []).append(
            {
                "time": f"{point.minute // 60:02d}:{point.minute % 60:02d}",
                "level": point.level,
            }
        )
    return days


async def _async_set_one(entity: Any, data: Mapping[str, Any]) -> dict[str, Any]:
    """Plan and write one program; the response holds no path or ID."""
    coordinator = entity.coordinator
    if (down := cloud_down_error(coordinator)) is not None:
        raise down
    path: str = entity.entity_description.resource_path
    requested = _requested(data["switch_points"])
    expected = _requested(data["expected"]) if "expected" in data else None
    replace_days = data.get("mode", "replace_week") == "replace_days"
    validate_only = bool(data.get("validate_only", False))
    outcome: dict[str, Any] = {}

    def plan(
        fresh: Resource,
        readings: Mapping[str, Resource],
        program_mode: Resource | None,
    ) -> tuple[SwitchPoint, ...] | None:
        assessment = assess_switch_program(fresh)
        outcome["previous"] = _week_response(sorted_points(assessment.points))
        issue = edit_issue(fresh, readings, program_mode=program_mode)
        if issue is not None:
            raise _error(_EDIT_ERRORS.get(issue, "switch_points_not_editable"))
        program = editable_program(fresh, readings, program_mode=program_mode)
        assert program is not None
        if not requested:
            raise _error("switch_points_empty_week")
        if expected is not None and not same_points(
            _points(expected, program), program.points
        ):
            raise _error("switch_points_program_changed")
        new = _points(requested, program)
        week = program.points
        if replace_days:
            for day in sorted({point.day for point in new}):
                week = replace_day(
                    week, day, [point for point in new if point.day == day]
                )
        else:
            week = new
        if (reason := week_issue(week, program)) is not None:
            raise _week_error(reason, program)
        outcome["switch_points"] = _week_response(week)
        if validate_only:
            outcome["result"] = "validated"
            return None
        if same_points(week, program.points):
            outcome["result"] = "unchanged"
            return None
        outcome["result"] = "sent"
        return week

    try:
        _fresh, confirmed = await coordinator.async_plan_switch_program(path, plan)
    except HomeAssistantError:
        raise
    except SwitchProgramChanged as err:
        raise _error("switch_points_program_changed") from err
    except ControlUnused as err:
        raise _error("switch_points_unused") from err
    except ContractRejected as err:
        if err.reason == "circuit_breaker":
            raise _failure("switch_points_paused") from err
        if err.reason.startswith("program_mode"):
            raise _error("switch_points_not_level_program") from err
        raise _error("switch_points_program_changed") from err
    except WriteValidationError as err:
        if (down := cloud_down_error(coordinator)) is not None:
            raise down from err
        raise _error("switch_points_program_changed") from err
    except WriteNotConfirmed as err:
        raise _failure("switch_points_not_confirmed") from err
    except WriteBusy as err:
        raise _failure("write_busy") from err
    except AuthenticationError as err:
        raise _failure("write_authentication_failed") from err
    except RateLimited as err:
        raise _failure("write_rate_limited") from err
    except UnexpectedHttpStatus as err:
        raise _failure("switch_points_rejected", {"status": str(err.status)}) from err
    except PointTError as err:
        raise _failure("write_failed") from err
    response: dict[str, Any] = {
        "result": outcome.get("result", "sent"),
        "confirmed": confirmed is not None,
        "previous": outcome.get("previous", {}),
        "switch_points": outcome.get("switch_points", {}),
    }
    if confirmed is not None:
        response["switch_points"] = _week_response(
            sorted_points(assess_switch_program(confirmed).points)
        )
    elif response["result"] != "sent":
        response["confirmed"] = response["result"] == "unchanged"
    return response
