"""Guard and stop coordinator updates independently of their device data model."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from .pointt.exceptions import SourceStopped


class ManagedSource(Protocol):
    """The lifecycle operations needed by a managed coordinator."""

    def current(self) -> bool: ...

    def authentication_failed(self) -> None: ...


class SourceUpdates:
    """Track active polls and reject results from a stopped source attempt."""

    def __init__(self, source: ManagedSource) -> None:
        self.source = source
        self._active: set[asyncio.Task[Any]] = set()

    async def run[T](self, update: Callable[[], Awaitable[T]]) -> T:
        if not self.source.current():
            raise UpdateFailed("Source is unavailable")
        task = asyncio.current_task()
        assert task is not None
        self._active.add(task)
        try:
            try:
                result = await update()
            except ConfigEntryAuthFailed as error:
                self.source.authentication_failed()
                raise UpdateFailed("Connection requires sign-in") from error
            except SourceStopped as error:
                raise UpdateFailed("Source is unavailable") from error
            if not self.source.current():
                raise UpdateFailed("Source is unavailable")
            return result
        finally:
            self._active.discard(task)

    async def stop(self, extra: Iterable[asyncio.Task[Any]] = ()) -> None:
        tasks = (self._active | set(extra)) - {asyncio.current_task()}
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
