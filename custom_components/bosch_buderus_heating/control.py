"""Shared Home Assistant control helpers."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN
from .coordinator import BoschBuderusDataUpdateCoordinator
from .pointt import (
    AuthenticationError,
    PointTError,
    RateLimited,
    WriteNotConfirmed,
    WriteValidationError,
)
from .writes import EnumWritePolicy, NumberWritePolicy


async def async_set_control(
    coordinator: BoschBuderusDataUpdateCoordinator,
    path: str,
    value: str | float,
    policy: EnumWritePolicy | NumberWritePolicy,
) -> None:
    """Write a control and expose only translated, value-free HA errors."""
    try:
        await coordinator.async_write_control(path, value, policy)
    except WriteValidationError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="write_validation_failed"
        ) from err
    except WriteNotConfirmed as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_not_confirmed"
        ) from err
    except AuthenticationError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_authentication_failed"
        ) from err
    except RateLimited as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_rate_limited"
        ) from err
    except PointTError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_failed"
        ) from err
