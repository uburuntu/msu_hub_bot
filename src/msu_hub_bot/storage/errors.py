"""Sanitized failures shared by persistence services and transports."""

from enum import StrEnum


class RepositoryFailure(StrEnum):
    AUTH = "authentication"
    DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    REJECTED = "request_rejected"
    CLOSED = "closed"


class RepositoryError(Exception):
    """Only local classifications and HTTP status may escape the transport."""

    def __init__(self, code: RepositoryFailure, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(f"Database operation failed: {code.value}" + (f" (HTTP {status})" if status is not None else ""))


class RepositoryUnavailable(RepositoryError):
    pass


class RepositoryAuthError(RepositoryError):
    pass


class RepositoryProtocolError(RepositoryError):
    def __init__(self) -> None:
        super().__init__(RepositoryFailure.INVALID_RESPONSE)
