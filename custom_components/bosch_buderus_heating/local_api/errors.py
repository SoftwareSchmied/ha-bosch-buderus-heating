"""Static failure codes without response values or endpoint details."""

from enum import StrEnum


class Problem(StrEnum):
    INVALID_PATH = "invalid_path"
    INVALID_PARAMETER = "invalid_parameter"
    UNCONFIRMED_SIGNAL = "unconfirmed_signal"
    UNSAFE_QUERY = "unsafe_query"
    INVALID_JSON = "invalid_json"
    BODY_TOO_LARGE = "body_too_large"
    INVALID_SCHEMA = "invalid_schema"
    WRONG_RESOURCE = "wrong_resource"
    INVALID_METADATA = "invalid_metadata"
    INVALID_CONTEXT = "invalid_context"


class LocalResourceError(ValueError):
    def __init__(self, problem: Problem) -> None:
        self.problem = problem
        super().__init__(problem.value)
