"""Wallbox commands and settings: checked, sent once, read back.

Every action follows the same order: nothing while the cloud is paused or
unreachable; the wallbox lock (no write while the wallbox is being read);
fresh reads of every value the rule depends on; the rule; exactly one
request (start after an allowed authentication is the only two-request
action); then reading again every two seconds for up to twenty seconds.
A request is never repeated, also not after a timeout: the reading decides.

The rules follow the vendor app where it is stricter than nothing, and are
stricter than the app where the app relies on its own screens: a setting is
written only when the wallbox reports it writeable, a command is refused in
a state the app would not offer it, and the app's own corrections (a hidden
``limit 6`` after a start, raising a power limit below 5 kW while reading)
are not reproduced.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from time import monotonic
from typing import TYPE_CHECKING, Final, Literal

from .pointt.exceptions import (
    PointTError,
    ProtocolError,
    RateLimited,
    RequestTimeout,
    ResourceError,
    ServiceUnavailable,
    TransportError,
    UnexpectedHttpStatus,
    WriteNotConfirmed,
    WriteRejected,
    WriteValidationError,
)
from .pointt.models import JsonValue
from .wallbox import Reading, Section, parse_section
from .wallbox_leaves import (
    FW_AUTHENTICATE,
    FW_BOOST,
    FW_ECO,
    FW_FEED_IN,
    FW_HOME_BATTERY,
    FW_OVERLOAD,
    FW_SOLAR,
    LEAVES,
    Firmware,
    Leaf,
    firmware_reaches,
    parse_leaf,
)
from .write_window import ensure_send_window, send_window
from .writes import WriteBusy, WriteDeadlineExceeded

if TYPE_CHECKING:
    from .wallbox import WallboxCoordinator

# Two-second read-back steps for up to twenty seconds.
READ_BACK_DELAYS: Final = (2.0,) * 10
# The vendor app waits five seconds after every command.
COMMAND_SPACING: Final = 5.0
LOCK_TIMEOUT: Final = 30.0
TRANSACTION_DEADLINE: Final = 150.0
OPTION_AUTHENTICATE: Final = "wallbox_authentication"
OPTION_AUTHENTICATION_NAME: Final = "wallbox_authentication_name"
MINIMUM_CURRENT: Final = 6
MAXIMUM_HARDWARE_CURRENT: Final = 63

START_STATES: Final = frozenset({"Preparing", "SuspendedEV", "SuspendedEVSE"})
# A station pause may be ended by a start only for these limiters; any
# other reported limiter keeps the pause (the app offers "pause" there).
START_LIMITERS: Final = frozenset(
    {"none", "nocar", "app", "web", "watchdog", "paragraph14a", "pv", "dynLmm"}
)
KNOWN_LIMITERS: Final = frozenset(
    {
        *START_LIMITERS,
        "derating",
        "key",
        "emma",
        "modbustcp",
        "ocpp",
        "loadbalancer",
        "staticLmm",
        "powermeterFailSafe",
        "extlimit",
    }
)
PAUSE_STATES: Final = frozenset({"Charging", "Limited"})
STARTED_STATES: Final = frozenset({"Charging", "Limited", "Authenticated"})
PAUSED_STATES: Final = frozenset({"SuspendedEVSE"})
LIMIT_STATES: Final = frozenset({"Preparing", "Charging", "Limited", "SuspendedEVSE"})
GROUP_COMMANDS: Final = frozenset({"start", "pause", "limit"})

type Action = str
type MeterRule = Literal["none", "present", "connected"]


class WallboxRejected(WriteValidationError):
    """Refused before anything was sent; ``translation_key`` says why."""

    def __init__(self, translation_key: str) -> None:
        super().__init__(translation_key)
        self.translation_key = translation_key


class WallboxCloudDown(WriteValidationError):
    """The cloud cannot take a command now; nothing was sent."""


@dataclass(frozen=True, slots=True)
class WallboxView:
    """What the rules look at, from fresh reads or from the last poll."""

    telemetry: Mapping[str, Reading]
    conf: Mapping[str, Reading]
    info: Mapping[str, Reading]
    leaves: Mapping[str, Leaf]
    firmware: Firmware | None

    def leaf_value(self, path: str) -> object:
        leaf = self.leaves.get(path)
        return leaf.value if leaf is not None else None


@dataclass(frozen=True, slots=True)
class SettingRule:
    """One writable cp0 setting and the conditions of the vendor app."""

    path: str
    kind: Literal["on_off", "number", "text", "offset"]
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    integer: bool = False
    firmware: Firmware | None = None
    meter: MeterRule = "none"
    energy_manager_blocks: bool = True
    solar_only: bool = False
    brand_check: bool = False
    max_length: int = 0


SETTINGS: Final[dict[str, SettingRule]] = {
    "solar_supplement": SettingRule(
        "conf/solarSurplus/supplementEnable",
        "on_off",
        firmware=FW_ECO,
        meter="present",
        solar_only=True,
    ),
    "supplement_limit": SettingRule(
        "conf/solarSurplus/supplementLimit",
        "number",
        0,
        11000,
        100,
        firmware=FW_ECO,
        meter="present",
        solar_only=True,
    ),
    "supplement_threshold": SettingRule(
        "conf/solarSurplus/supplementThreshold",
        "number",
        0,
        5000,
        100,
        firmware=FW_ECO,
        meter="present",
        solar_only=True,
    ),
    "home_battery_priority": SettingRule(
        "conf/strategy/powerOffset",
        "offset",
        firmware=FW_HOME_BATTERY,
        meter="present",
    ),
    "filter_time": SettingRule(
        "conf/strategy/filterTime",
        "number",
        5,
        600,
        1,
        integer=True,
        firmware=FW_HOME_BATTERY,
        meter="present",
    ),
    "current_limiter": SettingRule(
        "conf/overloadProtection/currentLimiter",
        "on_off",
        firmware=FW_OVERLOAD,
        meter="connected",
        brand_check=True,
    ),
    "power_limiter": SettingRule(
        "conf/overloadProtection/powerLimiter",
        "on_off",
        firmware=FW_OVERLOAD,
        meter="connected",
        brand_check=True,
    ),
    "power_limit": SettingRule(
        "conf/overloadProtection/powerLimit",
        "number",
        5000,
        None,
        100,
        firmware=FW_OVERLOAD,
        meter="present",
        brand_check=True,
    ),
    "locked": SettingRule("conf/locked", "on_off", energy_manager_blocks=False),
    "label": SettingRule(
        "conf/label", "text", energy_manager_blocks=False, max_length=32
    ),
    "electricity_price": SettingRule("conf/price", "number", 0, 100, 0.01),
    "feed_in_price": SettingRule(
        "conf/feedinPrice",
        "number",
        0,
        100,
        0.01,
        firmware=FW_FEED_IN,
        meter="connected",
    ),
}
PHASES_PATH: Final = "conf/overloadProtection/currentLimit"
PHASE_RULE: Final = SettingRule(
    PHASES_PATH,
    "number",
    16,
    63,
    1,
    firmware=FW_OVERLOAD,
    meter="present",
    brand_check=True,
)
STRATEGY_PATH: Final = "conf/chargingStrategy"
FORCE_PATH: Final = "telemetry/forceActive"
METER_PATH: Final = "telemetry/extmeterState"
BRAND_PATH: Final = "info/brand"
METER_MAXIMUM_PATH: Final = "info/extmeterCurrentMaximum"
# Facts of the installation that do not change while it runs.
STATIC_LEAVES: Final = frozenset({BRAND_PATH, METER_MAXIMUM_PATH})
DEFAULT_POWER_LIMIT_MAXIMUM: Final = 43000.0
STRATEGY_WIRE: Final = {"manual": "default", "solar": "solar-eco"}


# Rules -----------------------------------------------------------------------


def hardware_limit(view: WallboxView) -> int | None:
    """The hardware current limit when it is a whole number of 6 to 63 A."""
    value = view.info.get("hardware_current_limit")
    if (
        isinstance(value, float | int)
        and float(value).is_integer()
        and MINIMUM_CURRENT <= value <= MAXIMUM_HARDWARE_CURRENT
    ):
        return int(value)
    return None


def _load_management(view: WallboxView) -> bool:
    """The app's "load management" limiter: a group member limited by the
    group's balancer."""
    return view.conf.get("role") in ("leader", "follower") and view.telemetry.get(
        "limiter_token"
    ) in ("loadbalancer", "dynLmm")


def group_issue(view: WallboxView, action: Action) -> str | None:
    """A follower takes nothing; a leader only start, pause and the current."""
    role = view.conf.get("role")
    if role == "follower" or (role == "leader" and action not in GROUP_COMMANDS):
        return "wallbox_group_controlled"
    return None


def energy_manager_active(view: WallboxView) -> bool:
    return view.telemetry.get("energy_manager_token") == "emma"


def meter_issue(view: WallboxView, rule: MeterRule) -> str | None:
    state = view.leaf_value(METER_PATH)
    if rule == "connected" and state != "connected":
        return "wallbox_meter_missing"
    if rule == "present" and state not in ("connected", "error"):
        return "wallbox_meter_missing"
    return None


def brand_excluded(view: WallboxView) -> bool | None:
    """The app offers no overload protection for one brand; unknown: None."""
    brand = view.leaf_value(BRAND_PATH)
    if not isinstance(brand, str):
        return None
    return "heidelberg" in brand.casefold()


def start_issue(view: WallboxView) -> str | None:
    if (issue := group_issue(view, "start")) is not None:
        return issue
    state = view.telemetry.get("wb_state")
    limiter = view.telemetry.get("limiter_token")
    if state not in START_STATES:
        return "wallbox_state_not_allowed"
    if state == "SuspendedEVSE":
        allowed = (
            limiter in START_LIMITERS
            or (isinstance(limiter, str) and limiter not in KNOWN_LIMITERS)
        ) and not _load_management(view)
        if not allowed:
            return "wallbox_state_not_allowed"
        current = view.telemetry.get("current_limit")
        if (
            limiter == "app"
            and isinstance(current, int | float)
            and current < MINIMUM_CURRENT
            and view.conf.get("charging_strategy") in ("default", None)
        ):
            return "wallbox_current_below_minimum"
    return None


def pause_issue(view: WallboxView) -> str | None:
    if (issue := group_issue(view, "pause")) is not None:
        return issue
    if view.telemetry.get("wb_state") not in PAUSE_STATES:
        return "wallbox_state_not_allowed"
    return None


def limit_issue(view: WallboxView) -> str | None:
    if (issue := group_issue(view, "limit")) is not None:
        return issue
    if energy_manager_active(view):
        return "wallbox_energy_manager"
    if hardware_limit(view) is None:
        return "wallbox_value_unavailable"
    if view.telemetry.get("wb_state") not in LIMIT_STATES:
        return "wallbox_state_not_allowed"
    if view.conf.get("charging_strategy") not in ("default", None):
        return "wallbox_not_manual"
    if view.leaf_value(FORCE_PATH) == "on":
        return "wallbox_boost_active"
    if _load_management(view):
        return "wallbox_group_controlled"
    return None


def boost_issue(view: WallboxView) -> str | None:
    if (issue := group_issue(view, "boost")) is not None:
        return issue
    if not firmware_reaches(view.firmware, FW_BOOST):
        return "wallbox_firmware"
    if energy_manager_active(view):
        return "wallbox_energy_manager"
    if hardware_limit(view) is None or view.leaf_value(FORCE_PATH) is None:
        return "wallbox_value_unavailable"
    return None


def leaf_issue(leaf: Leaf | None) -> str | None:
    """Written only when reported writeable, available and used."""
    if leaf is None or leaf.available is False:
        return "wallbox_value_unavailable"
    if not leaf.writeable:
        return "wallbox_not_writeable"
    if leaf.used is False:
        return "control_unused"
    return None


def strategy_issue(view: WallboxView, option: str | None = None) -> str | None:
    if (issue := group_issue(view, "strategy")) is not None:
        return issue
    if not firmware_reaches(view.firmware, FW_SOLAR):
        return "wallbox_firmware"
    if energy_manager_active(view):
        return "wallbox_energy_manager"
    if (issue := meter_issue(view, "present")) is not None:
        return issue
    if (issue := leaf_issue(view.leaves.get(STRATEGY_PATH))) is not None:
        return issue
    if option == "solar" and (issue := meter_issue(view, "connected")) is not None:
        return issue
    if option == "manual" and view.leaf_value(FORCE_PATH) == "on":
        return "wallbox_boost_active"
    return None


def setting_issue(view: WallboxView, rule: SettingRule) -> str | None:
    """Whether the setting can be changed now (the value aside)."""
    if (issue := group_issue(view, "setting")) is not None:
        return issue
    if not firmware_reaches(view.firmware, rule.firmware):
        return "wallbox_firmware"
    if rule.energy_manager_blocks and energy_manager_active(view):
        return "wallbox_energy_manager"
    if (issue := meter_issue(view, rule.meter)) is not None:
        return issue
    if rule.brand_check and brand_excluded(view) is not False:
        return "wallbox_not_offered"
    if rule.solar_only and view.leaf_value(STRATEGY_PATH) != "solar-eco":
        return "wallbox_not_solar"
    return leaf_issue(view.leaves.get(rule.path))


def number_bounds(
    view: WallboxView, rule: SettingRule, leaf: Leaf | None
) -> tuple[float, float, float] | None:
    """Minimum, maximum and step: the narrower of the app's range and the
    range the wallbox reports; the reported step wins (none: the app's)."""
    if rule.minimum is None or rule.step is None:
        return None
    maximum = rule.maximum
    if rule.path == "conf/overloadProtection/powerLimit":
        meter = view.leaf_value(METER_MAXIMUM_PATH)
        maximum = (
            3 * 230 * float(meter)
            if isinstance(meter, float) and meter > 0
            else DEFAULT_POWER_LIMIT_MAXIMUM
        )
    if maximum is None:
        return None
    minimum = rule.minimum
    step = rule.step
    if leaf is not None:
        if leaf.minimum is not None:
            minimum = max(minimum, leaf.minimum)
        if leaf.maximum is not None:
            maximum = min(maximum, leaf.maximum)
        if leaf.step is not None:
            step = leaf.step
    if minimum > maximum:
        return None
    return minimum, maximum, step


def on_grid(value: float, minimum: float, maximum: float, step: float) -> bool:
    if not math.isfinite(value) or not minimum <= value <= maximum:
        return False
    steps = (value - minimum) / step
    return abs(steps - round(steps)) < 1e-6


def label_valid(value: object, max_length: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= max_length
        and value == value.strip()
        and all(character.isprintable() for character in value)
    )


# Transaction -----------------------------------------------------------------


@dataclass(slots=True)
class _Session:
    coordinator: WallboxCoordinator
    sleep: Callable[[float], Awaitable[None]]

    @property
    def gateway_id(self) -> str:
        return self.coordinator.gateway.gateway_id

    async def section(self, section: Section) -> Mapping[str, Reading]:
        try:
            payload = await self.coordinator.client.get_wallbox_section(
                self.gateway_id, section
            )
            snapshot = parse_section(section, payload)
        except (ResourceError, ProtocolError) as err:
            raise WallboxRejected("wallbox_value_unavailable") from err
        self.coordinator.store_section(section, snapshot)
        return snapshot.values

    async def leaf(self, path: str) -> Leaf:
        try:
            payload = await self.coordinator.client.get_wallbox_leaf(
                self.gateway_id, path
            )
            leaf = parse_leaf(LEAVES[path], payload, monotonic())
        except (ResourceError, ProtocolError) as err:
            raise WallboxRejected("wallbox_value_unavailable") from err
        self.coordinator.store_leaf(path, leaf)
        return leaf

    async def view(
        self, sections: tuple[Section, ...], leaves: tuple[str, ...] = ()
    ) -> WallboxView:
        """Fresh reads of the objects and of single values known to exist."""
        values: dict[Section, Mapping[str, Reading]] = {}
        for section in sections:
            values[section] = await self.section(section)
        read: dict[str, Leaf] = {}
        known = self.coordinator.leaves
        for path in leaves:
            if path in known:
                read[path] = await self.leaf(path)
        for path, leaf in known.items():
            # Unchanging facts (brand, meter maximum) come from the last read.
            read.setdefault(path, leaf)
        return WallboxView(
            values.get("telemetry", {}),
            values.get("conf", {}),
            values.get("info", {}),
            read,
            self.coordinator.firmware(),
        )

    async def send(self, request: Callable[[], Awaitable[None]]) -> bool:
        """Send once; True when the outcome must be learned by reading."""
        ensure_send_window(sum(READ_BACK_DELAYS))
        try:
            await request()
        except RateLimited:
            raise
        except UnexpectedHttpStatus as err:
            if 400 <= err.status < 500:
                raise WriteRejected("The wallbox refused the request") from err
            return True
        except TransportError:
            # Lost or late: it may have been applied. Never sent again.
            return True
        except ResourceError as err:
            raise WriteRejected("The wallbox refused the request") from err
        except ProtocolError:
            return True
        return False

    async def confirm(
        self, read: Callable[[], Awaitable[object]], check: Callable[[object], bool]
    ) -> None:
        last_temporary: PointTError | None = None
        for delay in READ_BACK_DELAYS:
            await self.sleep(delay)
            try:
                value = await read()
            except (RequestTimeout, ServiceUnavailable) as err:
                last_temporary = err
                continue
            except WallboxRejected as err:
                raise WriteNotConfirmed("Wallbox read-back failed") from err
            except PointTError as err:
                raise WriteNotConfirmed("Wallbox read-back failed") from err
            if check(value):
                return
        if last_temporary is not None:
            raise WriteNotConfirmed("Wallbox read-back incomplete") from last_temporary
        raise WriteNotConfirmed("Wallbox read-back did not confirm the change")


async def _transaction(
    coordinator: WallboxCoordinator,
    action: Action,
    run: Callable[[_Session], Awaitable[bool]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    command: bool = False,
) -> None:
    """Run one action under the wallbox lock and record its outcome.

    ``run`` returns False when nothing had to be sent (already set).
    """
    counts = coordinator.write_counts
    if coordinator.control_status()[0] is not None:
        counts[action + "_refused"] = counts.get(action + "_refused", 0) + 1
        raise WallboxCloudDown("The cloud cannot take a command now")
    lock = coordinator.poll_lock
    try:
        async with asyncio.timeout(LOCK_TIMEOUT):
            await lock.acquire()
    except TimeoutError as err:
        raise WriteBusy("The wallbox was busy; nothing was sent") from err
    outcome = "failed"
    try:
        if (
            command
            and coordinator.last_command is not None
            and monotonic() - coordinator.last_command < COMMAND_SPACING
        ):
            outcome = "busy"
            raise WriteBusy("A command was sent moments ago; nothing was sent")
        with send_window(TRANSACTION_DEADLINE) as window:
            try:
                async with asyncio.timeout(TRANSACTION_DEADLINE) as scope:
                    sent = await run(_Session(coordinator, sleep))
            except TimeoutError as err:
                if not scope.expired():
                    raise
                if not window.sent:
                    raise WriteBusy("The change could not start in time") from err
                raise WriteDeadlineExceeded("The change ran out of time") from err
            finally:
                if command and window.sent:
                    coordinator.last_command = monotonic()
        outcome = "confirmed" if sent else "unchanged"
    except WallboxRejected:
        outcome = "refused"
        raise
    except WriteBusy:
        outcome = "busy"
        raise
    except WriteNotConfirmed:
        outcome = "not_confirmed"
        raise
    except WriteRejected:
        outcome = "rejected"
        raise
    finally:
        lock.release()
        counts[action + "_" + outcome] = counts.get(action + "_" + outcome, 0) + 1
        coordinator.async_update_listeners()


def _option(coordinator: WallboxCoordinator, key: str) -> object:
    entry = coordinator.config_entry
    return entry.options.get(key) if entry is not None else None


def authentication_name(coordinator: WallboxCoordinator) -> str:
    name = _option(coordinator, OPTION_AUTHENTICATION_NAME)
    return name if isinstance(name, str) and label_valid(name, 64) else ""


# Actions ---------------------------------------------------------------------


async def async_start(
    coordinator: WallboxCoordinator,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Start charging; with an allowed authentication first."""

    async def run(session: _Session) -> bool:
        view = await session.view(("telemetry", "conf", "info"))
        if (issue := start_issue(view)) is not None:
            raise WallboxRejected(issue)
        name = authentication_name(coordinator)
        client = coordinator.client
        if view.conf.get("authentication") == "on":
            if not (
                _option(coordinator, OPTION_AUTHENTICATE) is True
                and firmware_reaches(view.firmware, FW_AUTHENTICATE)
            ):
                raise WallboxRejected("wallbox_authentication_required")
            if await session.send(
                lambda: client.post_wallbox_command(
                    session.gateway_id, "authenticate", {"name": name}
                )
            ):
                # Its outcome is unknown: no start without it.
                raise WriteNotConfirmed("Authentication was not confirmed")
        await session.send(
            lambda: client.post_wallbox_command(
                session.gateway_id, "start", {"name": name}
            )
        )
        await session.confirm(
            lambda: session.section("telemetry"),
            lambda values: _get(values, "wb_state") in STARTED_STATES,
        )
        return True

    await _transaction(coordinator, "start", run, sleep=sleep, command=True)


async def async_pause(
    coordinator: WallboxCoordinator,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Pause a running charge."""

    async def run(session: _Session) -> bool:
        view = await session.view(("telemetry", "conf"))
        if (issue := pause_issue(view)) is not None:
            raise WallboxRejected(issue)
        name = authentication_name(coordinator)
        await session.send(
            lambda: coordinator.client.post_wallbox_command(
                session.gateway_id, "pause", {"name": name}
            )
        )
        await session.confirm(
            lambda: session.section("telemetry"),
            lambda values: _get(values, "wb_state") in PAUSED_STATES,
        )
        return True

    await _transaction(coordinator, "pause", run, sleep=sleep, command=True)


async def async_set_current_limit(
    coordinator: WallboxCoordinator,
    value: float,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Set the charging current limit in whole amperes."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not float(value).is_integer()
    ):
        raise WriteValidationError("The current must be a whole number")
    target = int(value)

    async def run(session: _Session) -> bool:
        view = await session.view(("telemetry", "conf", "info"), (FORCE_PATH,))
        if (issue := limit_issue(view)) is not None:
            raise WallboxRejected(issue)
        maximum = hardware_limit(view)
        if maximum is None or not MINIMUM_CURRENT <= target <= maximum:
            raise WriteValidationError("The current is outside the allowed range")
        if view.telemetry.get("current_limit") == target:
            return False
        await session.send(
            lambda: coordinator.client.post_wallbox_command(
                session.gateway_id, "limit", {"limit": target}
            )
        )
        await session.confirm(
            lambda: session.section("telemetry"),
            lambda values: _get(values, "current_limit") == target,
        )
        return True

    await _transaction(coordinator, "limit", run, sleep=sleep, command=True)


async def async_set_boost(
    coordinator: WallboxCoordinator,
    on: bool,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Charge with the hardware maximum (on) or end it (off)."""

    async def run(session: _Session) -> bool:
        view = await session.view(("telemetry", "conf", "info"), (FORCE_PATH,))
        if (issue := boost_issue(view)) is not None:
            raise WallboxRejected(issue)
        target = "on" if on else "off"
        if view.leaf_value(FORCE_PATH) == target:
            return False
        maximum = hardware_limit(view)
        client = coordinator.client
        if on:
            if maximum is None:
                raise WallboxRejected("wallbox_value_unavailable")
            await session.send(
                lambda: client.post_wallbox_command(
                    session.gateway_id, "force/limit", {"limit": maximum}
                )
            )
        else:
            await session.send(
                lambda: client.post_wallbox_command(
                    session.gateway_id, "force/limitReset", {}
                )
            )
        await session.confirm(
            lambda: session.leaf(FORCE_PATH),
            lambda leaf: isinstance(leaf, Leaf) and leaf.value == target,
        )
        return True

    await _transaction(coordinator, "boost", run, sleep=sleep, command=True)


async def async_set_strategy(
    coordinator: WallboxCoordinator,
    option: str,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Switch between normal (``manual``) and solar charging (``solar``)."""
    wire = STRATEGY_WIRE.get(option)
    if wire is None:
        raise WriteValidationError("Unknown charging mode")

    async def run(session: _Session) -> bool:
        view = await session.view(
            ("telemetry", "conf", "info"), (STRATEGY_PATH, METER_PATH, FORCE_PATH)
        )
        if (issue := strategy_issue(view, option)) is not None:
            raise WallboxRejected(issue)
        if view.leaf_value(STRATEGY_PATH) == wire:
            return False
        await _put(session, STRATEGY_PATH, wire)
        return True

    await _transaction(coordinator, "strategy", run, sleep=sleep)


async def async_set_setting(
    coordinator: WallboxCoordinator,
    key: str,
    value: str | float | bool,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Change one setting of ``SETTINGS``: a switch (bool), a number or text."""
    rule = SETTINGS[key]

    async def run(session: _Session) -> bool:
        view = await session.view(
            ("telemetry", "conf", "info"), (rule.path, METER_PATH, STRATEGY_PATH)
        )
        if (issue := setting_issue(view, rule)) is not None:
            raise WallboxRejected(issue)
        leaf = view.leaves.get(rule.path)
        wire = _wire_value(view, rule, leaf, value)
        if leaf is not None and _same(leaf.value, wire):
            return False
        await _put(session, rule.path, wire)
        return True

    await _transaction(coordinator, key, run, sleep=sleep)


async def async_set_phase_limit(
    coordinator: WallboxCoordinator,
    phase: int,
    value: float,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Change one phase of the three-phase current limit list.

    The list is read fresh, one item replaced, and the list of exactly
    three values sent in one request.
    """
    if phase not in (0, 1, 2):
        raise WriteValidationError("Unknown phase")

    async def run(session: _Session) -> bool:
        view = await session.view(
            ("telemetry", "conf", "info"), (PHASES_PATH, METER_PATH)
        )
        if (issue := setting_issue(view, PHASE_RULE)) is not None:
            raise WallboxRejected(issue)
        leaf = view.leaves.get(PHASES_PATH)
        current = leaf.value if leaf is not None else None
        if not isinstance(current, tuple) or len(current) != 3:
            raise WallboxRejected("wallbox_value_unavailable")
        bounds = number_bounds(view, PHASE_RULE, leaf)
        if (
            bounds is None
            or isinstance(value, bool)
            or not isinstance(value, int | float)
            or not on_grid(float(value), *bounds)
        ):
            raise WriteValidationError("The current is outside the allowed range")
        items = [float(item) for item in current]
        items[phase] = float(value)
        if _same(current, tuple(items)):
            return False
        wire: list[JsonValue] = list(items)
        await _put(session, PHASES_PATH, wire)
        return True

    await _transaction(coordinator, "current_limit", run, sleep=sleep)


def _wire_value(
    view: WallboxView, rule: SettingRule, leaf: Leaf | None, value: object
) -> JsonValue:
    if rule.kind == "on_off":
        if not isinstance(value, bool):
            raise WriteValidationError("A switch takes on or off")
        return "on" if value else "off"
    if rule.kind == "offset":
        if not isinstance(value, bool):
            raise WriteValidationError("A switch takes on or off")
        return -100.0 if value else 0.0
    if rule.kind == "text":
        if not label_valid(value, rule.max_length):
            raise WriteValidationError("The text is empty, too long or not printable")
        assert isinstance(value, str)
        return value
    bounds = number_bounds(view, rule, leaf)
    if (
        bounds is None
        or isinstance(value, bool)
        or not isinstance(value, int | float)
        or not on_grid(float(value), *bounds)
    ):
        raise WriteValidationError("The value is outside the allowed range")
    if rule.integer:
        if not float(value).is_integer():
            raise WriteValidationError("The value must be a whole number")
        return int(value)
    return float(value)


# The single values that the configuration object shows as well (its field).
_CONF_FIELDS: Final = {
    "conf/locked": "locked",
    STRATEGY_PATH: "charging_strategy",
    "conf/label": "label",
}


async def _put(session: _Session, path: str, wire: JsonValue) -> None:
    await session.send(
        lambda: session.coordinator.client.put_wallbox_setting(
            session.gateway_id, path, wire
        )
    )
    expected = tuple(wire) if isinstance(wire, list) else wire
    await session.confirm(
        lambda: session.leaf(path),
        lambda leaf: isinstance(leaf, Leaf) and _same(leaf.value, expected),
    )
    _note_confirmed(session.coordinator, path, wire)


def _note_confirmed(
    coordinator: WallboxCoordinator, path: str, wire: JsonValue
) -> None:
    """The configuration object read before the write still holds the old
    value: replace that field by the confirmed one, so the entities show it
    at once, without another request. Its read time stays as it was."""
    name = _CONF_FIELDS.get(path)
    snapshot = (coordinator.data or {}).get("conf") if name is not None else None
    if name is None or snapshot is None or not isinstance(wire, str):
        return
    coordinator.store_section(
        "conf", replace(snapshot, values={**snapshot.values, name: wire})
    )


def _same(left: object, right: object) -> bool:
    if isinstance(left, tuple) and isinstance(right, tuple):
        return len(left) == len(right) and all(
            _same(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return math.isclose(float(left), float(right), rel_tol=0, abs_tol=1e-6)
    return left == right


def _get(values: object, key: str) -> object:
    return values.get(key) if isinstance(values, Mapping) else None
