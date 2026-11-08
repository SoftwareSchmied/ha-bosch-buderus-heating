"""Complete versioned read catalog; address domains are not device discovery."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote, urlencode

from ._data import OPERATIONS, SCHEMAS, SOURCE_COMMIT, SOURCE_SHA256
from .errors import LocalResourceError, Problem
from .schema import matches

__all__ = [
    "CATALOG",
    "SOURCE_COMMIT",
    "SOURCE_SHA256",
    "Parameter",
    "ReadRequest",
    "ResourceDefinition",
    "make_request",
    "request_for_path",
    "validate_path",
]


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    location: str
    required: bool
    kind: str
    options: tuple[str | bool, ...] = ()
    format: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceDefinition:
    template: str
    group: str
    parameters: tuple[Parameter, ...]
    response_schemas: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReadRequest:
    """A canonical, explicitly selected resource. No endpoint or credentials."""

    definition: ResourceDefinition
    path: str = field(repr=False)
    query: tuple[tuple[str, str | bool], ...] = field(repr=False)

    @property
    def method(self) -> Literal["GET"]:
        return "GET"

    @property
    def target(self) -> str:
        query = urlencode(
            [
                (key, str(value).lower() if type(value) is bool else value)
                for key, value in self.query
            ]
        )
        return quote(self.path, safe="/") + ("?" + query if query else "")


def _parameter(data: dict[str, Any]) -> Parameter:
    schema = data["schema"]
    if "$ref" in schema:
        schema = SCHEMAS[schema["$ref"].rsplit("/", 1)[-1]]
    return Parameter(
        data["name"],
        data["in"],
        data.get("required", False),
        schema["type"],
        tuple(schema.get("enum", ())),
        schema.get("format"),
    )


def _refs(schema: dict[str, Any]) -> set[str]:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return (
            set()
            if name in {"ReadableResource", "WriteableResource", "BaseResource"}
            else {name}
        )
    return {
        name
        for key in ("oneOf", "anyOf", "allOf")
        for child in schema.get(key, ())
        for name in _refs(child)
    }


CATALOG = tuple(
    ResourceDefinition(
        path,
        item["group"],
        tuple(_parameter(p) for p in item["parameters"]),
        tuple(sorted(_refs(item["response"]))),
    )
    for path, item in OPERATIONS.items()
)
_BY_TEMPLATE = {definition.template: definition for definition in CATALOG}
_PATTERNS = tuple(
    (
        re.compile(re.sub(r"\\\{[^}]+\\\}", "([^/]+)", re.escape(definition.template))),
        definition,
    )
    for definition in CATALOG
)


def validate_path(path: str) -> None:
    """Accept a canonical resource ID, never a URI or query-bearing input."""
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 1024:
        raise LocalResourceError(Problem.INVALID_PATH)
    if (
        any(part in {"", ".", ".."} for part in path[1:].split("/"))
        or any(c in path for c in "\\%?#")
        or any(ord(c) < 33 or ord(c) == 127 for c in path)
    ):
        raise LocalResourceError(Problem.INVALID_PATH)
    try:
        path.encode("utf-8")
    except UnicodeError:
        raise LocalResourceError(Problem.INVALID_PATH) from None


def make_request(
    template: str,
    *,
    parameters: Mapping[str, str] | None = None,
    query: Mapping[str, str | bool] | None = None,
    signal_ids: Collection[str] = (),
) -> ReadRequest:
    """Build one request. Dynamic signals require caller-supplied read evidence."""
    definition = _BY_TEMPLATE.get(template)
    if definition is None:
        raise LocalResourceError(Problem.INVALID_PATH)
    path_values, query_values = dict(parameters or {}), dict(query or {})
    path_names = {p.name for p in definition.parameters if p.location == "path"}
    query_names = {p.name for p in definition.parameters if p.location == "query"}
    if set(path_values) != path_names or not set(query_values) <= query_names:
        raise LocalResourceError(Problem.INVALID_PARAMETER)
    for parameter in definition.parameters:
        values = path_values if parameter.location == "path" else query_values
        if parameter.name not in values:
            if parameter.required:
                raise LocalResourceError(Problem.INVALID_PARAMETER)
            continue
        value = values[parameter.name]
        schema: dict[str, Any] = {"type": parameter.kind}
        if parameter.options:
            schema["enum"] = parameter.options
        if parameter.format:
            schema["format"] = parameter.format
        if not matches(value, schema):
            raise LocalResourceError(Problem.INVALID_PARAMETER)
        if parameter.location == "path":
            if "/" in str(value):
                raise LocalResourceError(Problem.INVALID_PATH)
            validate_path("/" + str(value))
    if "signalId" in path_values and path_values["signalId"] not in signal_ids:
        raise LocalResourceError(Problem.UNCONFIRMED_SIGNAL)
    if query_values.get("refresh") is True:
        raise LocalResourceError(Problem.UNSAFE_QUERY)
    start, end = query_values.get("startDate"), query_values.get("endDate")
    if isinstance(start, str) and isinstance(end, str) and end < start:
        raise LocalResourceError(Problem.INVALID_PARAMETER)
    path = template.format_map(path_values)
    validate_path(path)
    return ReadRequest(definition, path, tuple(sorted(query_values.items())))


def request_for_path(
    path: str,
    *,
    query: Mapping[str, str | bool] | None = None,
    signal_ids: Collection[str] = (),
) -> ReadRequest:
    """Match the exact canonical path without expanding any address domain."""
    validate_path(path)
    for pattern, definition in _PATTERNS:
        if matched := pattern.fullmatch(path):
            names = (p.name for p in definition.parameters if p.location == "path")
            return make_request(
                definition.template,
                parameters=dict(zip(names, matched.groups(), strict=True)),
                query=query,
                signal_ids=signal_ids,
            )
    raise LocalResourceError(Problem.INVALID_PATH)


def response_schema(request: ReadRequest) -> dict[str, Any]:
    """Revalidate constructed requests before selecting their response contract."""
    signal = (
        request.path.removeprefix("/signals/")
        if request.path.startswith("/signals/")
        else ""
    )
    checked = request_for_path(
        request.path, query=dict(request.query), signal_ids=(signal,)
    )
    if checked != request:
        raise LocalResourceError(Problem.INVALID_PARAMETER)
    return OPERATIONS[checked.definition.template]["response"]  # type: ignore[no-any-return]
