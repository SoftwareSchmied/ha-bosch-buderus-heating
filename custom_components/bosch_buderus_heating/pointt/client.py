"""Typed high-level PointT client."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from urllib.parse import quote

import aiohttp

from .bulk import chunk_resource_paths, normalize_resource_path
from .const import DEFAULT_USER_AGENT, MAX_BULK_PATHS, POINTT_BASE_URL
from .exceptions import AccessTokenRejected
from .metrics import RequestMetrics
from .models import BatchItemResult, Gateway, JsonValue, Resource
from .parsers import (
    parse_batch_response,
    parse_gateway,
    parse_gateways,
    parse_part_number,
    parse_resource,
)
from .token_manager import StaticTokenProvider
from .transport import PointTTransport, RetryPolicy


class AccessTokenProvider(Protocol):
    """Access-token interface consumed by PointTClient."""

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return an access token and optionally force one refresh."""
        ...


class PointTClient:
    """Typed async client for the observed PointT API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str | AccessTokenProvider,
        *,
        base_url: str = POINTT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        concurrency: int = 3,
        retry_policy: RetryPolicy | None = None,
        metrics: RequestMetrics | None = None,
    ) -> None:
        self._token_provider: AccessTokenProvider
        if isinstance(access_token, str):
            self._token_provider = StaticTokenProvider(access_token)
        else:
            self._token_provider = access_token
        self._transport = PointTTransport(
            session,
            base_url=base_url,
            user_agent=user_agent,
            concurrency=concurrency,
            retry_policy=retry_policy,
            metrics=metrics,
        )

    @property
    def metrics(self) -> RequestMetrics:
        """Return privacy-safe aggregate request metrics."""
        return self._transport.metrics

    async def get_gateways(self) -> tuple[Gateway, ...]:
        """Return the gateways visible to the current account."""
        payload = await self._request("GET", "gateways/", retryable=True)
        return parse_gateways(payload)

    async def get_gateway(self, gateway_id: str) -> Gateway:
        """Return normalized metadata for one gateway."""
        gateway = _encode_gateway_id(gateway_id)
        payload = await self._request("GET", f"gateways/{gateway}", retryable=True)
        return parse_gateway(payload)

    async def get_part_number(self, gateway_id: str) -> str:
        """Return a gateway part number."""
        gateway = _encode_gateway_id(gateway_id)
        payload = await self._request(
            "GET", f"gateways/{gateway}/partnumber", retryable=True
        )
        return parse_part_number(payload)

    async def get_resource(
        self,
        gateway_id: str,
        path: str,
        *,
        fallback_reason: str | None = None,
    ) -> Resource:
        """Read and parse one resource path."""
        gateway = _encode_gateway_id(gateway_id)
        normalized = normalize_resource_path(path)
        encoded_path = "/".join(
            quote(segment, safe="") for segment in normalized.lstrip("/").split("/")
        )
        payload = await self._request(
            "GET",
            f"gateways/{gateway}/resource/{encoded_path}",
            retryable=True,
            resource_path=normalized,
            request_type="fallback" if fallback_reason is not None else None,
            fallback_reason=fallback_reason,
        )
        return parse_resource(payload, path=normalized)

    async def put_resource_value(
        self, gateway_id: str, path: str, value: JsonValue
    ) -> Resource | None:
        """Write one scalar value without retrying a potentially applied PUT."""
        gateway = _encode_gateway_id(gateway_id)
        normalized = normalize_resource_path(path)
        encoded_path = "/".join(
            quote(segment, safe="") for segment in normalized.lstrip("/").split("/")
        )
        payload = await self._request(
            "PUT",
            f"gateways/{gateway}/resource/{encoded_path}",
            json_body={"value": value},
            retryable=False,
            resource_path=normalized,
        )
        if payload is None:
            return None
        return parse_resource(payload, path=normalized)

    async def create_holiday_period(
        self, gateway_id: str, values: dict[str, JsonValue]
    ) -> None:
        """Create one holiday period without retrying the non-idempotent POST."""
        gateway = _encode_gateway_id(gateway_id)
        await self._request(
            "POST",
            f"gateways/{gateway}/resource/holidayMode",
            json_body=[values],
            retryable=False,
            resource_path="/holidayMode",
        )

    async def update_holiday_period(
        self, gateway_id: str, holiday_id: int, values: dict[str, JsonValue]
    ) -> None:
        """Update one numeric holiday period without an automatic write retry."""
        gateway = _encode_gateway_id(gateway_id)
        validated_id = _validate_holiday_id(holiday_id)
        await self._request(
            "PUT",
            f"gateways/{gateway}/resource/holidayMode/{validated_id}",
            json_body=[values],
            retryable=False,
            resource_path=f"/holidayMode/{validated_id}",
        )

    async def delete_holiday_period(self, gateway_id: str, holiday_id: int) -> None:
        """Delete one numeric holiday period without an automatic write retry."""
        gateway = _encode_gateway_id(gateway_id)
        validated_id = _validate_holiday_id(holiday_id)
        await self._request(
            "DELETE",
            f"gateways/{gateway}/resource/holidayMode/{validated_id}",
            retryable=False,
            resource_path=f"/holidayMode/{validated_id}",
        )

    async def get_resources_bulk(
        self,
        gateway_id: str,
        paths: Sequence[str],
        *,
        chunk_size: int = MAX_BULK_PATHS,
    ) -> tuple[BatchItemResult, ...]:
        """Read resources sequentially in safe bulk chunks."""
        _encode_gateway_id(gateway_id)
        results: list[BatchItemResult] = []
        for chunk in chunk_resource_paths(paths, size=chunk_size):
            body: JsonValue = [
                {
                    "gatewayId": gateway_id,
                    "resourcePaths": list(chunk),
                }
            ]
            payload, request_sequence = await self._request_with_sequence(
                "POST", "bulk", json_body=body, retryable=True
            )
            parsed = parse_batch_response(
                payload, gateway_id=gateway_id, requested_paths=chunk
            )
            self.metrics.record_bulk_items(
                tuple(item.status for item in parsed),
                usable=tuple(item.ok for item in parsed),
                server_statuses=tuple(item.server_status for item in parsed),
                gateway_statuses=tuple(item.gateway_status for item in parsed),
                request_sequence=request_sequence,
            )
            results.extend(parsed)
        return tuple(results)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: JsonValue = None,
        retryable: bool,
        resource_path: str | None = None,
        request_type: str | None = None,
        fallback_reason: str | None = None,
    ) -> JsonValue:
        payload, _request_sequence = await self._request_with_sequence(
            method,
            path,
            json_body=json_body,
            retryable=retryable,
            resource_path=resource_path,
            request_type=request_type,
            fallback_reason=fallback_reason,
        )
        return payload

    async def _request_with_sequence(
        self,
        method: str,
        path: str,
        *,
        json_body: JsonValue = None,
        retryable: bool,
        resource_path: str | None = None,
        request_type: str | None = None,
        fallback_reason: str | None = None,
    ) -> tuple[JsonValue, int]:
        token = await self._token_provider.get_access_token()
        try:
            return await self._transport.request_json_with_sequence(
                method,
                path,
                token,
                json_body=json_body,
                retryable=retryable,
                resource_path=resource_path,
                request_type=request_type,
                fallback_reason=fallback_reason,
            )
        except AccessTokenRejected:
            token = await self._token_provider.get_access_token(force_refresh=True)
            return await self._transport.request_json_with_sequence(
                method,
                path,
                token,
                json_body=json_body,
                retryable=retryable,
                resource_path=resource_path,
                request_type=request_type,
                fallback_reason=fallback_reason,
                attempt_offset=1,
            )


def _encode_gateway_id(gateway_id: str) -> str:
    value = gateway_id.strip()
    if not value:
        raise ValueError("Gateway ID must not be empty")
    return quote(value, safe="")


def _validate_holiday_id(holiday_id: int) -> int:
    if (
        isinstance(holiday_id, bool)
        or not isinstance(holiday_id, int)
        or not 0 <= holiday_id <= 2_147_483_647
    ):
        raise ValueError("Holiday ID must be a non-negative 32-bit integer")
    return holiday_id
