"""Narrow response variants observed beside the pinned manufacturer schema."""

from typing import Any

from .errors import LocalResourceError, Problem


def response_for_validation(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a schema-shaped copy without changing the received document.

    Numeric state maps occur both directly and inside the documented list.
    Update reports may spell moduleId as module_id. Neither adaptation relaxes
    the remaining schema, invents an identifier or authorizes a write.
    """
    if payload.get("type") in ("floatValue", "integerValue") and isinstance(
        payload.get("state"), dict
    ):
        return {**payload, "state": [payload["state"]]}
    if payload.get("type") == "updateReport" and isinstance(
        payload.get("values"), list
    ):
        rows = []
        for row in payload["values"]:
            if (
                isinstance(row, dict)
                and row.get("moduleId", row.get("module_id")) == ""
            ):
                raise LocalResourceError(Problem.INVALID_SCHEMA)
            if isinstance(row, dict) and "module_id" in row:
                alias = row["module_id"]
                if "moduleId" in row and (
                    type(alias) is not type(row["moduleId"]) or alias != row["moduleId"]
                ):
                    raise LocalResourceError(Problem.INVALID_SCHEMA)
                row = {**row, "moduleId": alias}
            rows.append(row)
        return {**payload, "values": rows}
    return payload
