"""The wallbox entities show a confirmed change at once (synthetic).

A wallbox whose configuration object follows its single values, as a device
does. The select, the lock switch and the display name show the
configuration object; after a confirmed write they show the confirmed value
without waiting for the next read of that object (5 to 10 minutes) and
without another request. A display name longer than 32 characters
is still shown. Nothing here confirms behaviour on a real device.
"""

from __future__ import annotations

import pytest

from custom_components.bosch_buderus_heating.pointt import (
    WriteNotConfirmed,
    WriteValidationError,
)
from custom_components.bosch_buderus_heating.wallbox_entities import (
    WallboxSelect,
    WallboxSwitch,
    WallboxText,
)
from tests.test_wallbox_controls import (
    _spec,
    async_set_setting,
    async_set_strategy,
    settle,
    sleeper,
    wallbox,
)


def device(fake) -> None:
    """A wallbox whose configuration object follows its single values."""
    settle(fake)
    inner = fake.react
    fields = {
        "conf/locked": "locked",
        "conf/chargingStrategy": "chargingStrategy",
        "conf/label": "label",
    }

    def react(method, target, body):
        inner(method, target, body)
        if method == "PUT" and target in fields:
            fake.conf(**{fields[target]: body})

    fake.react = react


async def test_lock_switch_shows_the_confirmed_value(hass):
    coordinator, fake = await wallbox(hass)
    device(fake)
    switch = WallboxSwitch(coordinator, _spec("locked"))
    assert switch.is_on is False
    await async_set_setting(coordinator, "locked", True, sleep=sleeper())
    assert fake.writes() == [("PUT", "conf/locked", "on")]
    assert switch.is_on is True
    # No extra request for the display: the object was read before the PUT.
    assert [call for call in fake.calls if call == ("GET", "conf")] == [("GET", "conf")]


async def test_strategy_select_shows_the_confirmed_value(hass):
    coordinator, fake = await wallbox(hass)
    device(fake)
    select = WallboxSelect(coordinator, _spec("charging_strategy"))
    assert select.current_option == "manual"
    await async_set_strategy(coordinator, "solar", sleep=sleeper())
    assert select.current_option == "solar"


async def test_display_name_shows_the_confirmed_value(hass):
    coordinator, fake = await wallbox(hass)
    device(fake)
    text = WallboxText(coordinator, _spec("label"))
    await async_set_setting(coordinator, "label", "new-name", sleep=sleeper())
    assert text.native_value == "new-name"


async def test_an_unconfirmed_write_keeps_the_last_reading(hass):
    """The read-back never shows the value: the object stays as read."""
    coordinator, fake = await wallbox(hass)
    fake.react = None  # the wallbox does not apply the change
    switch = WallboxSwitch(coordinator, _spec("locked"))
    with pytest.raises(WriteNotConfirmed):
        await async_set_setting(coordinator, "locked", True, sleep=sleeper())
    assert switch.is_on is False


async def test_a_display_name_longer_than_the_app_allows_is_shown(hass):
    coordinator, fake = await wallbox(hass)
    fake.conf(label="L" * 40)
    coordinator._next.clear()
    await coordinator.async_refresh()
    text = WallboxText(coordinator, _spec("label"))
    text.entity_id = "text.wallbox_label"
    assert text.native_value == "L" * 40
    assert text.state == "L" * 40
    assert text.native_max == 40
    # A write still keeps the limit of 32 characters.
    with pytest.raises(WriteValidationError):
        await async_set_setting(coordinator, "label", "L" * 33, sleep=sleeper())
    assert fake.writes() == []


async def test_a_short_display_name_keeps_the_app_limit(hass):
    coordinator, _fake = await wallbox(hass)
    text = WallboxText(coordinator, _spec("label"))
    assert text.native_max == 32
