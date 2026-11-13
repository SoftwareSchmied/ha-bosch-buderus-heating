"""Explicit runtime and identity capabilities required by the local setup UI."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant

from .configuration import ConfigurationSnapshot
from .const import DOMAIN
from .local_completion import ScopedIdentityContract
from .local_setup import VerifiedLocalSetup

SUPPORT_KEY = f"{DOMAIN}.local_setup_support"


@dataclass(frozen=True, slots=True)
class LocalSetupSupport:
    """Installed by a tested local runtime, never by form data or discovery TXT.

    The managed local runtime installs its source policy during integration
    setup. That policy acquires scoped evidence and controls offline reuse.
    """

    runtime_ready: Callable[[], bool] = field(repr=False)
    identity_contract: Callable[
        [ConfigurationSnapshot, str, str], ScopedIdentityContract | None
    ] = field(repr=False)
    verify_identity: (
        Callable[
            [ConfigurationSnapshot, str, VerifiedLocalSetup],
            Awaitable[ScopedIdentityContract | None],
        ]
        | None
    ) = field(default=None, repr=False)


def setup_support(hass: HomeAssistant) -> LocalSetupSupport | None:
    from .config_flow import BoschBuderusConfigFlow

    support = hass.data.get(SUPPORT_KEY)
    if (
        not isinstance(support, LocalSetupSupport)
        or not support.runtime_ready()
        or BoschBuderusConfigFlow.VERSION != 4
        or BoschBuderusConfigFlow.MINOR_VERSION < 2
    ):
        return None
    return support
