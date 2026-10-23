"""Stable error envelope of the operator API: ``{"error": {code, message, details}}``.

``code`` is a stable English identifier for clients, ``message`` is the Russian text
an operator reads. Internal exception text never leaves the process: unexpected
failures are logged by type only and answered with ``INTERNAL_ERROR``.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.observability.logging import get_logger

_log = get_logger("api.errors")


class ErrorBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    error: ErrorBody


class ApiError(Exception):
    """A refusal the operator must understand; never carries secret values."""

    def __init__(
        self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}

    def envelope(self) -> ErrorEnvelope:
        return ErrorEnvelope(
            error=ErrorBody(code=self.code, message=self.message, details=self.details)
        )


_HTTP_CODES = {
    400: ("BAD_REQUEST", "Некорректный запрос"),
    401: ("UNAUTHORIZED", "Нужна аутентификация оператора"),
    403: ("FORBIDDEN", "Действие запрещено"),
    404: ("NOT_FOUND", "Не найдено"),
    405: ("METHOD_NOT_ALLOWED", "Метод не поддерживается"),
    409: ("CONFLICT", "Конфликт состояния"),
    415: ("UNSUPPORTED_MEDIA_TYPE", "Неподдерживаемый формат тела запроса"),
}

# Documented on every versioned route so the OpenAPI contract names the envelope.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorEnvelope, "description": text}
    for status, text in (
        (401, "Нет аутентификации оператора"),
        (403, "Нет CSRF-токена или повторного подтверждения"),
        (404, "Объект не найден"),
        (409, "Состояние не позволяет действие"),
        (422, "Некорректные входные данные"),
        (503, "Зависимость не настроена или недоступна"),
    )
}


def is_api_path(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def json_error(error: ApiError) -> JSONResponse:
    return JSONResponse(error.envelope().model_dump(mode="json"), status_code=error.status_code)


def validation_details(error: RequestValidationError) -> dict[str, Any]:
    fields = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
        fields.append({"field": location, "reason": str(item.get("msg", "invalid"))})
    return {"fields": fields}


HtmlRenderer = Callable[[Request, ApiError], Awaitable[Response]]


def install_error_handlers(app: FastAPI, render_html: HtmlRenderer) -> None:
    """``render_html`` renders the same refusal as a page outside ``/api/``."""

    async def respond(request: Request, error: ApiError) -> Response:
        if is_api_path(request):
            return json_error(error)
        return await render_html(request, error)

    async def on_api_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, ApiError)
        return await respond(request, exc)

    async def on_validation(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, RequestValidationError)
        return await respond(
            request,
            ApiError(
                422, "INVALID_INPUT", "Некорректные входные данные", validation_details(exc)
            ),
        )

    async def on_http(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, StarletteHTTPException)
        code, message = _HTTP_CODES.get(exc.status_code, ("HTTP_ERROR", "Ошибка запроса"))
        response = await respond(request, ApiError(exc.status_code, code, message))
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    async def on_unexpected(request: Request, exc: Exception) -> Response:
        _log.error("api_unhandled_error", path=request.url.path, error_type=type(exc).__name__)
        return await respond(
            request, ApiError(500, "INTERNAL_ERROR", "Внутренняя ошибка сервиса")
        )

    app.add_exception_handler(ApiError, on_api_error)
    app.add_exception_handler(RequestValidationError, on_validation)
    app.add_exception_handler(StarletteHTTPException, on_http)
    app.add_exception_handler(Exception, on_unexpected)
