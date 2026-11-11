"""D43: outside text in dialog descriptions renders as text, not Markdown."""

from __future__ import annotations

import re

import pytest

from custom_components.bosch_buderus_heating.markdown_text import (
    MAX_LENGTH,
    markdown_literal,
)


def _rendered(escaped: str) -> str:
    """What a CommonMark renderer shows for backslash escapes."""
    return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", escaped)


@pytest.mark.parametrize(
    "name",
    [
        "![x](https://tracker.invalid/pixel.png)",
        "[Click](https://example.invalid)",
        "<img src=x onerror=alert(1)>",
        "<https://example.invalid>",
        "https://example.invalid/path",
        "www.example.invalid",
        "someone@example.invalid",
        "**bold** _it_ `code` ~~gone~~ # head | cell",
        "&lt;b&gt;",
    ],
)
def test_links_images_html_and_markup_are_escaped(name: str) -> None:
    escaped = markdown_literal(name)
    assert _rendered(escaped) == name
    active = _strip(escaped)
    for unsafe in ("![", "](", "<", "://", "www.", "@", "**", "&", "`", "#"):
        assert unsafe not in active


def _strip(escaped: str) -> str:
    """The text without the escaped characters, i.e. what Markdown can act on."""
    return re.sub(r"\\.", "", escaped)


@pytest.mark.parametrize(
    "plain",
    [
        "Urlaub 3 · 2030-08-01 08:00 / 2030-08-08 18:00",
        "Ferien am Meer",
        "192.168.1.20",
        "101012902",
    ],
)
def test_ordinary_names_dates_and_addresses_stay_unchanged(plain: str) -> None:
    assert markdown_literal(plain) == plain


def test_text_is_one_bounded_line() -> None:
    assert markdown_literal("a\n\n# b\tc") == "a \\# b c"
    assert len(markdown_literal("x" * 1000)) == MAX_LENGTH


@pytest.mark.parametrize("action", ["holiday", "delete_holiday"])
async def test_a_holiday_name_cannot_add_an_image_or_link_to_the_dialog(
    hass, enable_custom_integrations, action
) -> None:
    import base64

    from custom_components.bosch_buderus_heating.holidays import HOLIDAY_LIST_PATH
    from tests.test_config_flow import (
        _entry_with_writable_holiday,
        _open_existing_holiday,
    )

    name = "![](https://t.invalid/p)[a](b)"  # within the 32-byte name limit
    entry, coordinator = _entry_with_writable_holiday(hass)
    row = coordinator.data[HOLIDAY_LIST_PATH].resource.value[0]
    row["name"] = base64.b64encode(name.encode()).decode()
    result = await _open_existing_holiday(hass, entry, action=action)
    shown = result["description_placeholders"]["holiday"]
    assert _rendered(shown).startswith(name)
    active = _strip(shown)
    assert "![" not in active and "](" not in active and "://" not in active
