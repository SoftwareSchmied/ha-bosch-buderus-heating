"""Typed high-level PointT client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Literal, Protocol
from urllib.parse import quote, urlencode

import aiohttp

from .bulk import chunk_resource_paths, normalize_query, normalize_resource_path
from .const import DEFAULT_USER_AGENT, MAX_BULK_PATHS, POINTT_BASE_URL
from .exceptions import AccessTokenRejected
from .metrics import RequestMetrics
from .models import BatchItemResult, Gateway, JsonValue, Resource
from .parsers import (
    parse_batch_response,
    parse_gateway,
    parse_gateway_list,
    parse_part_number,
    parse_resource,
)
from .token_manager import StaticTokenProvider
from .transport import PointTTransport, RateLimitBackoff, RetryPolicy

_LOGGER = logging.getLogger(__name__)


class AccessTokenProvider(Protocol):
    """Access-token interface consumed by PointTClient."""

    async def get_access_token(
        self, *, force_refresh: bool = False, rejected_token: str | None = None
    ) -> str:
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
        backoff: RateLimitBackoff | None = None,
        request_guard: Callable[[], None] | None = None,
        request_semaphore: asyncio.Semaphore | None = None,
        gateway_id: str | None = None,
    ) -> None:
        self._token_provider: AccessTokenProvider
        self._request_guard = request_guard or (lambda: None)
        self._gateway_id = gateway_id
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
            backoff=backoff,
            request_guard=self._request_guard,
            request_semaphore=request_semaphore,
        )

    @property
    def backoff(self) -> RateLimitBackoff:
        """Return the account brake so setup retries can retain its deadline."""
        return self._transport.backoff

    @property
    def metrics(self) -> RequestMetrics:
        """Return privacy-safe aggregate request metrics."""
        return self._transport.metrics

    async def get_gateways(self) -> tuple[Gateway, ...]:
        """Return the gateways visible to the current account."""
        if self._gateway_id is not None:
            raise ValueError("A gateway source cannot enumerate an account")
        payload = await self._request("GET", "gateways/", retryable=True)
        gateways, skipped = parse_gateway_list(payload)
        if skipped:
            self.metrics.record_skipped_gateway_entries(skipped)
            _LOGGER.debug("Ignored %d unreadable gateway list entries", skipped)
        return gateways

    async def get_gateway(self, gateway_id: str) -> Gateway:
        """Return normalized metadata for one gateway."""
        gateway = self._encode_gateway(gateway_id)
        payload = await self._request("GET", f"gateways/{gateway}", retryable=True)
        return parse_gateway(payload)

    async def get_part_number(self, gateway_id: str) -> str:
        """Return a gateway part number."""
        gateway = self._encode_gateway(gateway_id)
        payload = await self._request(
            "GET", f"gateways/{gateway}/partnumber", retryable=True
        )
        return parse_part_number(payload)

    async def get_local_identity_evidence(
        self, gateway_id: str
    ) -> tuple[JsonValue, JsonValue]:
        """Read raw identity fields before tolerant resource parsing discards them.

        Explicit local setup only; both GETs retain normal account pacing,
        authentication, request guards and rate-limit accounting.
        """
        gateway = self._encode_gateway(gateway_id)
        metadata = await self._request("GET", f"gateways/{gateway}", retryable=True)
        system = await self._request(
            "GET",
            f"gateways/{gateway}/resource/system/info",
            retryable=True,
            resource_path="/system/info",
        )
        return metadata, system

    async def get_resource(
        self,
        gateway_id: str,
        path: str,
        *,
        fallback_reason: str | None = None,
    ) -> Resource:
        """Read and parse one resource path."""
        gateway = self._encode_gateway(gateway_id)
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

    async def get_wallbox_section(
        self, gateway_id: str, section: Literal["telemetry", "conf", "info"]
    ) -> JsonValue:
        """Read one cp0 object through the shared account transport."""
        if section not in ("telemetry", "conf", "info"):
            raise ValueError("Invalid wallbox section")
        gateway = self._encode_gateway(gateway_id)
        path = f"/rest/v1/cp0/{section}"
        return await self._request(
            "GET",
            f"gateways/{gateway}/resource{path}",
            retryable=True,
            resource_path=path,
        )

    async def put_resource_value(
        self, gateway_id: str, path: str, value: JsonValue
    ) -> Resource | None:
        """Write one scalar value without retrying a potentially applied PUT."""
        gateway = self._encode_gateway(gateway_id)
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

    async def put_resource_json(
        self, gateway_id: str, path: str, value: JsonValue
    ) -> Resource | None:
        """Write one structured value (a list or an object) exactly once.

        The body is ``{"value": value}`` as for scalars; never retried.
        """
        if not isinstance(value, list | dict):
            raise ValueError("A structured PointT value is a list or an object")
        return await self.put_resource_value(gateway_id, path, value)

    async def post_resource(
        self, gateway_id: str, path: str, value: JsonValue
    ) -> JsonValue:
        """Create one entry below a resource (``{"value": value}``) without
        retrying the non-idempotent POST; returns the raw answer, if any."""
        gateway = self._encode_gateway(gateway_id)
        normalized = normalize_resource_path(path)
        return await self._request(
            "POST",
            f"gateways/{gateway}/resource/{_encode_path(normalized)}",
            json_body={"value": value},
            retryable=False,
            resource_path=normalized,
        )

    async def delete_resource(self, gateway_id: str, path: str) -> None:
        """Delete one resource without an automatic retry."""
        gateway = self._encode_gateway(gateway_id)
        normalized = normalize_resource_path(path)
        await self._request(
            "DELETE",
            f"gateways/{gateway}/resource/{_encode_path(normalized)}",
            retryable=False,
            resource_path=normalized,
        )

    async def get_resource_query(
        self, gateway_id: str, path: str, query: Mapping[str, str | int]
    ) -> Resource:
        """Read one resource with a validated query (W5: ``?entry=``,
        ``?date=``, ``?next=``).

        Errors, metrics and logs name the path without its query.
        """
        gateway = self._encode_gateway(gateway_id)
        normalized = normalize_resource_path(path)
        parameters = urlencode(normalize_query(query))
        payload = await self._request(
            "GET",
            f"gateways/{gateway}/resource/{_encode_path(normalized)}?{parameters}",
            retryable=True,
            resource_path=normalized,
        )
        return parse_resource(payload, path=normalized)

    async def put_icom_holiday_assignments(
        self, gateway_id: str, slot: int, circuits: tuple[str, ...]
    ) -> None:
        """Write the legacy array envelope once, separately from scalar PUT."""
        if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 999:
            raise ValueError("Invalid ICOM holiday slot")
        if (
            not circuits
            or len(circuits) != len(set(circuits))
            or any(
                not isinstance(circuit, str)
                or not (
                    (circuit.startswith("hc") and circuit[2:].isdecimal())
                    or (circuit.startswith("dhw") and circuit[3:].isdecimal())
                )
                for circuit in circuits
            )
        ):
            raise ValueError("Invalid ICOM holiday assignments")
        gateway = self._encode_gateway(gateway_id)
        path = f"/system/holidayModes/hm{slot}/assignedTo"
        await self._request(
            "PUT",
            f"gateways/{gateway}/resource{path}",
            json_body={"values": list(circuits)},
            retryable=False,
            resource_path=path,
        )

    async def create_holiday_period(
        self, gateway_id: str, values: dict[str, JsonValue]
    ) -> None:
        """Create one holiday period without retrying the non-idempotent POST."""
        gateway = self._encode_gateway(gateway_id)
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
        gateway = self._encode_gateway(gateway_id)
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
        gateway = self._encode_gateway(gateway_id)
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
        retryable: bool = True,
        request_type: str | None = None,
    ) -> tuple[BatchItemResult, ...]:
        """Read resources sequentially in safe bulk chunks."""
        self._encode_gateway(gateway_id)
        results: list[BatchItemResult] = []
        for chunk in chunk_resource_paths(paths, size=chunk_size):
            body: JsonValue = [
                {
                    "gatewayId": gateway_id,
                    "resourcePaths": list(chunk),
                }
            ]
            payload, request_sequence = await self._request_with_sequence(
                "POST",
                "bulk",
                json_body=body,
                retryable=retryable,
                request_type=request_type,
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
            if any(item.status == 429 for item in parsed):
                self._transport.backoff.activate(None)
                break
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
        self._request_guard()
        self._transport.backoff.raise_if_active()
        token = await self._token_provider.get_access_token()
        self._request_guard()
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
            self._request_guard()
            self._transport.backoff.raise_if_active()
            token = await self._token_provider.get_access_token(
                force_refresh=True, rejected_token=token
            )
            self._request_guard()
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

    def _encode_gateway(self, gateway_id: str) -> str:
        self._request_guard()
        if self._gateway_id is not None and gateway_id != self._gateway_id:
            raise ValueError("Gateway does not match the source binding")
        return _encode_gateway_id(gateway_id)


def _encode_path(normalized: str) -> str:
    return "/".join(
        quote(segment, safe="") for segment in normalized.lstrip("/").split("/")
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
