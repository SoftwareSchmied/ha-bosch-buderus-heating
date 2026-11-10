"""ERR-9: unexpected failures name their class, never their text."""

from __future__ import annotations

import logging

import pytest

from custom_components.bosch_buderus_heating import notifications
from custom_components.bosch_buderus_heating.log_helpers import log_failure

_LOGGER = logging.getLogger("tests.log_helpers")
SECRET = "account secret@example.invalid"


def _failure() -> RuntimeError:
    try:
        raise RuntimeError(SECRET)
    except RuntimeError as err:
        return err


def test_failure_names_the_class_and_keeps_the_text_out(caplog):
    caplog.set_level(logging.WARNING, logger=_LOGGER.name)
    log_failure(_LOGGER, logging.WARNING, "Step %s failed", "save", error=_failure())
    assert [record.getMessage() for record in caplog.records] == [
        "Step save failed (RuntimeError)"
    ]
    assert caplog.records[0].levelno == logging.WARNING
    assert caplog.records[0].exc_info is None
    assert SECRET not in caplog.text


def test_details_follow_only_at_debug_level(caplog):
    caplog.set_level(logging.DEBUG, logger=_LOGGER.name)
    log_failure(_LOGGER, logging.ERROR, "Update failed", error=_failure())
    first, details = caplog.records
    assert first.getMessage() == "Update failed (RuntimeError)"
    assert first.levelno == logging.ERROR and first.exc_info is None
    assert details.levelno == logging.DEBUG
    assert details.exc_info is not None and details.exc_info[0] is RuntimeError


class _BrokenNotifications(notifications.FaultNotifications):
    def __init__(self) -> None:
        self._update_error_logged = False
        self._closed = False

    def _source_current(self) -> bool:
        return False

    def _update(self) -> None:
        raise KeyError(SECRET)


@pytest.mark.parametrize("repeat", [1, 3])
def test_fault_notification_failure_is_logged_once_with_its_class(caplog, repeat):
    caplog.set_level(logging.WARNING, logger=notifications.__name__)
    broken = _BrokenNotifications()
    for _ in range(repeat):
        broken.async_update()
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "The heating fault notification could not be updated (KeyError)"
    ]
    assert SECRET not in caplog.text
