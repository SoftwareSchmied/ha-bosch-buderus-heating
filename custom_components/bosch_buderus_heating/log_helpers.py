"""Shared logging of unexpected failures."""

from __future__ import annotations

import logging


def log_failure(
    logger: logging.Logger,
    level: int,
    message: str,
    *args: object,
    error: BaseException,
) -> None:
    """Log an unexpected failure with its error class, the details at debug level.

    ERR-9: the message names only the class, never the error text, which may
    carry account or device details. The traceback follows at debug level, so
    the cause can be found with debug logging enabled.
    """
    logger.log(level, f"{message} (%s)", *args, type(error).__name__)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("Details of the failure above", exc_info=error)
