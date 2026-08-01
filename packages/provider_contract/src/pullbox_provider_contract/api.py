"""Shared hardened HTTP boundary for direct-download provider applications."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pullbox_provider_contract.auth import bearer_token_matches
from pullbox_provider_contract.errors import ProtocolError
from pullbox_provider_contract.models import PROTOCOL_VERSION

_SECURITY = HTTPBearer(auto_error=False)
MIN_BEARER_TOKEN_LENGTH = 32


class BearerAuthenticator:
    """Constant-time bearer authentication dependency."""

    def __init__(self, expected_token: str) -> None:
        self._expected_token = expected_token

    async def __call__(
        self,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_SECURITY)],
    ) -> None:
        presented = credentials.credentials if credentials else None
        if not bearer_token_matches(presented, self._expected_token):
            raise ProtocolError(
                401,
                "provider_authentication_failed",
                "Valid provider bearer authentication is required.",
            )


def require_bearer_token(token: str) -> str:
    """Reject startup without an adequately strong provider boundary token."""
    if len(token) < MIN_BEARER_TOKEN_LENGTH:
        raise ValueError("PULLBOX_PROVIDER_TOKEN must contain at least 32 characters")
    return token


def install_protocol_handlers(app: FastAPI) -> None:
    """Install secret-safe provider protocol and validation responses."""

    @app.exception_handler(ProtocolError)
    async def protocol_error(_request: Request, exc: ProtocolError) -> JSONResponse:
        return error_response(
            exc.code,
            exc.message,
            exc.status_code,
            retry_after_seconds=exc.retry_after_seconds,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return error_response(
            "invalid_request",
            "The provider request did not match the protocol contract.",
            422,
        )


def validate_request(protocol_version: str, deadline: datetime) -> None:
    """Validate protocol compatibility and absolute request deadline."""
    if protocol_version != PROTOCOL_VERSION:
        raise ProtocolError(409, "incompatible_protocol", "Unsupported protocol version.")
    if deadline <= datetime.now(UTC):
        raise ProtocolError(408, "deadline_exceeded", "The provider request deadline has passed.")


async def within_deadline[Result](
    awaitable: Awaitable[Result],
    deadline: datetime,
) -> Result:
    """Bound source work by the caller's absolute deadline."""
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise ProtocolError(408, "deadline_exceeded", "The provider request deadline has passed.")
    try:
        async with asyncio.timeout(remaining):
            return await awaitable
    except TimeoutError as exc:
        raise ProtocolError(
            408,
            "deadline_exceeded",
            "The provider request deadline has passed.",
        ) from exc


def error_response(
    code: str,
    message: str,
    status_code: int,
    *,
    retry_after_seconds: int | None = None,
) -> JSONResponse:
    """Build the protocol's bounded error envelope."""
    error: dict[str, str | int] = {"code": code, "message": message}
    if (
        isinstance(retry_after_seconds, int)
        and not isinstance(retry_after_seconds, bool)
        and 0 <= retry_after_seconds <= 86_400
    ):
        error["retry_after_seconds"] = retry_after_seconds
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
    )
