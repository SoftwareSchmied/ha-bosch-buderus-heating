"""A simulated iCom gateway: a real HTTP server with the real encryption.

The answers follow the synthetic shapes of tests/fixtures/contracts/icom. The
server can behave like a slow gateway: it can delay, hang, answer with an error
status or with noise, and it counts requests (also overlapping ones).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from custom_components.bosch_buderus_heating.icom_crypto import (
    derive_key,
    encrypt,
    reset_key,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/contracts/icom/gateway-shapes.json"


def load_documents() -> tuple[dict[str, dict[str, Any]], set[str]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    documents = {item["id"]: item for item in data["resources"]}
    protected = {path for path in data["unavailable"].get("403", ())}
    return documents, protected


class SimulatedIcomGateway:
    def __init__(
        self,
        *,
        gateway_password: str = "abcd-efgh-ijkl-mnop",
        private_password: str = "private-secret",
        gateway_id: str = "000123",
    ) -> None:
        self.documents, self.protected = load_documents()
        self.documents["/gateway/uuid"] = {
            "id": "/gateway/uuid",
            "type": "stringValue",
            "value": gateway_id,
            "writeable": 0,
        }
        # An empty private password: a gateway whose private password was reset.
        self.key = (
            derive_key(gateway_password, private_password)
            if private_password
            else reset_key(gateway_password)
        )
        self.requests: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0
        # Behaviours; None means a normal answer.
        self.hang = False
        self.hang_next = 0
        self.delay = 0.0
        self.status: int | None = None
        self.retry_after: str | None = None
        self.noise = False
        self.noise_next = 0
        self.raw: bytes | None = None
        # A plain text that is encrypted as it is (not a document of the tree).
        self.plain: bytes | None = None
        self.stream: int = 0
        self.path_status: dict[str, int] = {}
        self.fail_after: int | None = None
        self.headers_seen: list[dict[str, str]] = []
        self._runner: web.AppRunner | None = None
        self._released = asyncio.Event()
        self.port = 0

    def value(self, path: str, value: Any) -> None:
        self.documents[path] = {**copy.deepcopy(self.documents[path]), "value": value}

    def drop(self, *paths: str) -> None:
        for path in paths:
            self.documents.pop(path, None)

    def count(self, path: str) -> int:
        return self.requests.count(path)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        self.requests.append(path)
        self.headers_seen.append(dict(request.headers))
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.hang_next > 0:
                self.hang_next -= 1
                await self._released.wait()
            if self.hang:
                await self._released.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_after is not None and len(self.requests) > self.fail_after:
                return web.Response(status=500)
            if self.status is not None:
                headers = {"Retry-After": self.retry_after} if self.retry_after else {}
                return web.Response(status=self.status, headers=headers)
            if path in self.path_status:
                return web.Response(status=self.path_status[path])
            if self.stream:
                # No Content-Length: the size is only known while reading.
                response = web.StreamResponse()
                await response.prepare(request)
                for _ in range(self.stream // 8192):
                    await response.write(b"A" * 8192)
                await response.write_eof()
                return response
            if self.raw is not None:
                return web.Response(body=self.raw)
            if self.plain is not None:
                return web.Response(body=encrypt(self.key, self.plain))
            if self.noise or self.noise_next > 0:
                self.noise_next = max(0, self.noise_next - 1)
                return web.Response(body=encrypt(os.urandom(32), b"x" * 32))
            document = self.documents.get(path)
            if document is None:
                return web.Response(status=403 if path in self.protected else 404)
            return web.Response(body=encrypt(self.key, json.dumps(document).encode()))
        finally:
            self.concurrent -= 1

    async def start(self) -> int:
        app = web.Application()
        app.router.add_get("/{tail:.*}", self._handle)
        self._released.clear()
        self._runner = web.AppRunner(app, handler_cancellation=True)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        sockets = site._server.sockets  # type: ignore[union-attr]
        self.port = sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        self._released.set()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
