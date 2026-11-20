"""Every platform declares how many entity updates may run in parallel.

The limit is 0 (none) everywhere. A write already runs under the gateway's
write lock, and an overlapping change is refused as busy rather than queued
(see ``test_a_second_target_while_one_is_sent_is_busy``); a limit of 1 would
queue it behind the first change and send it afterwards.
"""

from __future__ import annotations

import importlib

import pytest

from custom_components.bosch_buderus_heating.const import PLATFORMS


def test_every_platform_has_a_limit() -> None:
    for platform in PLATFORMS:
        module = importlib.import_module(
            f"custom_components.bosch_buderus_heating.{platform.value}"
        )
        assert hasattr(module, "PARALLEL_UPDATES"), platform.value


@pytest.mark.parametrize("platform", sorted(p.value for p in PLATFORMS))
def test_no_platform_queues_writes(platform: str) -> None:
    module = importlib.import_module(
        f"custom_components.bosch_buderus_heating.{platform}"
    )
    assert module.PARALLEL_UPDATES == 0
