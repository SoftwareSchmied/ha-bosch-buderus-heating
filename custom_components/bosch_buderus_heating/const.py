"""Constants for Bosch/Buderus Heating."""

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

from homeassistant.const import Platform

DOMAIN: Final = "bosch_buderus_heating"
NAME: Final = "Bosch/Buderus Heating"

CONF_BRAND: Final = "brand"
CONF_GATEWAY_IDS: Final = "gateway_ids"
CONF_POLLING_PROFILE: Final = "polling_profile"
CONF_REDIRECT_URL: Final = "redirect_url"

CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_EXPIRES_AT: Final = "expires_at"
CONF_TOKEN_TYPE: Final = "token_type"
CONF_SCOPE: Final = "scope"


class PollingProfile(StrEnum):
    """Supported cloud-friendly polling profiles."""

    STANDARD = "standard"
    CLOUD_FRIENDLY = "cloud_friendly"


DEFAULT_POLLING_PROFILE: Final = PollingProfile.STANDARD
RATE_LIMIT_ISSUE_PREFIX: Final = "repeated_rate_limit_"
FIRMWARE_ISSUE_PREFIX: Final = "incompatible_firmware_"


def polling_profile_from_options(options: Mapping[str, Any]) -> PollingProfile:
    """Read a persisted profile while tolerating older or invalid entries."""
    value = options.get(CONF_POLLING_PROFILE)
    if not isinstance(value, str):
        return DEFAULT_POLLING_PROFILE
    try:
        return PollingProfile(value)
    except ValueError:
        return DEFAULT_POLLING_PROFILE


PLATFORMS: Final = (
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
)
