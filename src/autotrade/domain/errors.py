"""Structured provider errors with explicit retry classification."""

from __future__ import annotations

from enum import StrEnum


class ErrorKind(StrEnum):
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    RATE_LIMITED = "rate_limited"
    HTTP_STATUS = "http_status"
    RPC_ERROR = "rpc_error"
    MALFORMED = "malformed_response"
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"


class ProviderError(Exception):
    kind: ErrorKind
    retryable: bool = False

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class ProviderTimeout(ProviderError):
    kind = ErrorKind.TIMEOUT
    retryable = True


class TransportError(ProviderError):
    kind = ErrorKind.TRANSPORT
    retryable = True


class RateLimited(ProviderError):
    kind = ErrorKind.RATE_LIMITED
    retryable = True


class HttpStatusError(ProviderError):
    kind = ErrorKind.HTTP_STATUS

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        # 5xx and 408 are transient; other 4xx (auth, bad request) are not.
        self.retryable = status >= 500 or status == 408


# JSON-RPC codes Solana nodes use for transient conditions
# (-32005 node unhealthy/behind, -32004 block not yet available).
RETRYABLE_RPC_CODES = frozenset({-32005, -32004})


class RpcError(ProviderError):
    kind = ErrorKind.RPC_ERROR

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"rpc error {code}: {message}")
        self.code = code
        self.retryable = code in RETRYABLE_RPC_CODES


class MalformedResponse(ProviderError):
    kind = ErrorKind.MALFORMED


class InvalidRequest(ProviderError):
    kind = ErrorKind.INVALID_REQUEST


class ProviderUnavailable(ProviderError):
    """All endpoints / attempts exhausted."""

    kind = ErrorKind.UNAVAILABLE
