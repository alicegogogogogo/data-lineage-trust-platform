"""Application errors rendered as stable JSON error responses."""

from __future__ import annotations


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


class NotFoundError(APIError):
    """A referenced dataset, schema version or field does not exist (404)."""

    def __init__(self, message: str) -> None:
        super().__init__(404, "not_found", message)


class ConflictError(APIError):
    """The request conflicts with already stored state (409)."""

    def __init__(self, message: str) -> None:
        super().__init__(409, "conflict", message)


class RequestInvalidError(APIError):
    """The request is structurally or semantically invalid (422)."""

    def __init__(self, message: str) -> None:
        super().__init__(422, "validation_error", message)
