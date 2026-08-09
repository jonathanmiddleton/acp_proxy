"""Authenticated FastAPI surface for Meadow's strict direct protocol."""

from __future__ import annotations

import hmac

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .direct_protocol import (
    PATH_SAFE_IDENTIFIER_PATTERN,
    CancelRequest,
    CreateSessionRequest,
    PromptRequest,
    RetireSessionRequest,
)
from .direct_service import DirectBusy, DirectGenerationMismatch, DirectService
from .direct_state import (
    DirectConflict,
    DirectLimitExceeded,
    DirectNotFound,
    DirectStateError,
)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )


def create_direct_app(service: DirectService) -> FastAPI:
    """Create an app that exposes only the direct Meadow consumer mode."""

    app = FastAPI(
        title="ACP Proxy Meadow Direct API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        RequestBodyLimitMiddleware, max_bytes=service.limits.max_request_bytes
    )

    async def authenticate(
        authorization: str | None = Header(default=None),
    ) -> None:
        prefix = "Bearer "
        supplied = authorization[len(prefix) :] if authorization and authorization.startswith(prefix) else ""
        if not supplied or not hmac.compare_digest(supplied, service.launch_secret):
            raise DirectAuthenticationError("authentication required")

    @app.exception_handler(DirectAuthenticationError)
    async def authentication_error(
        _request: Request, _exc: DirectAuthenticationError
    ) -> JSONResponse:
        return _error(401, "unauthorized", "authentication required")

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return _error(422, "invalid_direct_request", "request does not match the strict direct schema")

    @app.exception_handler(DirectStateError)
    async def state_error(_request: Request, exc: DirectStateError) -> JSONResponse:
        if isinstance(exc, DirectNotFound):
            return _error(404, "not_found", str(exc))
        if isinstance(exc, DirectLimitExceeded):
            return _error(429, "resource_limit", str(exc))
        if isinstance(exc, DirectBusy):
            return _error(409, "session_busy", str(exc))
        if isinstance(exc, DirectGenerationMismatch):
            return _error(409, "generation_or_protocol_mismatch", str(exc))
        if isinstance(exc, DirectConflict):
            return _error(409, "conflict", str(exc))
        return _error(400, "direct_protocol_error", str(exc))

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "consumer_mode": "meadow-direct",
                "protocol_major": 1,
            }
        )

    @app.get("/meadow/v1/capabilities", dependencies=[Depends(authenticate)])
    async def capabilities() -> JSONResponse:
        return JSONResponse(service.capabilities.model_dump(mode="json"))

    @app.post("/meadow/v1/sessions", dependencies=[Depends(authenticate)])
    async def create_session(request: CreateSessionRequest) -> JSONResponse:
        record, _ = await service.admit_create(request)
        view = await service.wait_for_operation(record)
        return JSONResponse(view.model_dump(mode="json"))

    @app.post(
        "/meadow/v1/sessions/{logical_session_id}/requests",
        dependencies=[Depends(authenticate)],
    )
    async def submit_prompt(
        request: PromptRequest,
        logical_session_id: str = Path(
            min_length=1,
            max_length=256,
            pattern=PATH_SAFE_IDENTIFIER_PATTERN,
        ),
    ) -> JSONResponse:
        record, _ = await service.admit_prompt(logical_session_id, request)
        view = await service.wait_for_operation(record)
        return JSONResponse(view.model_dump(mode="json"))

    @app.get(
        "/meadow/v1/operations/{operation_id}",
        dependencies=[Depends(authenticate)],
    )
    async def operation_status(
        protocol_major: int,
        operation_id: str = Path(
            min_length=1,
            max_length=256,
            pattern=PATH_SAFE_IDENTIFIER_PATTERN,
        ),
        continuity_generation_id: str = Query(
            min_length=1,
            max_length=256,
            pattern=PATH_SAFE_IDENTIFIER_PATTERN,
        ),
    ) -> JSONResponse:
        view = service.operation_view(
            service.operation(
                operation_id,
                protocol_major=protocol_major,
                generation_id=continuity_generation_id,
            )
        )
        return JSONResponse(view.model_dump(mode="json"))

    @app.post(
        "/meadow/v1/operations/{target_operation_id}/cancel",
        dependencies=[Depends(authenticate)],
    )
    async def cancel_operation(
        request: CancelRequest,
        target_operation_id: str = Path(
            min_length=1,
            max_length=256,
            pattern=PATH_SAFE_IDENTIFIER_PATTERN,
        ),
    ) -> JSONResponse:
        if request.target_operation_id != target_operation_id:
            raise DirectConflict("cancel path and target_operation_id disagree")
        record, _ = await service.admit_cancel(request)
        view = await service.wait_for_operation(record)
        return JSONResponse(view.model_dump(mode="json"))

    @app.post(
        "/meadow/v1/sessions/{logical_session_id}/retire",
        dependencies=[Depends(authenticate)],
    )
    async def retire_session(
        request: RetireSessionRequest,
        logical_session_id: str = Path(
            min_length=1,
            max_length=256,
            pattern=PATH_SAFE_IDENTIFIER_PATTERN,
        ),
    ) -> JSONResponse:
        if request.logical_session_id != logical_session_id:
            raise DirectConflict("retire path and logical_session_id disagree")
        record, _ = await service.admit_retire(request)
        view = await service.wait_for_operation(record)
        status = 200 if view.state == "completed" else 409
        return JSONResponse(view.model_dump(mode="json"), status_code=status)

    @app.api_route(
        "/v1/{legacy_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def reject_legacy(legacy_path: str) -> JSONResponse:
        return _error(
            410,
            "legacy_mode_required",
            f"/v1/{legacy_path} is available only in explicit opencode-legacy mode",
        )

    return app


class DirectAuthenticationError(RuntimeError):
    """Authentication failed without exposing credential details."""


class RequestBodyLimitMiddleware:
    """Bound actual received bytes, independent of Content-Length honesty."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        messages: list[Message] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.request":
                projected_total = total + len(message.get("body", b""))
                if projected_total > self.max_bytes:
                    response = _error(
                        413,
                        "request_too_large",
                        "request exceeds negotiated byte limit",
                    )
                    await response(scope, receive, send)
                    return
                total = projected_total
                messages.append(message)
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                messages.append(message)
                break

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.disconnect"}

        await self.app(scope, replay, send)
