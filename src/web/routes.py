"""Web routes of the operator panel: pages, login and form actions (spec 8.1, 5.6).

Pages read the same services as ``/api/v1``. Every form action needs a web session
and its CSRF token; successful actions redirect (POST -> 303 -> GET), so refreshing a
page shows the state of the original request instead of repeating it (G02.2).
"""

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote
from uuid import UUID, uuid4

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from src.api import labels
from src.api.errors import ApiError, install_error_handlers
from src.api.routes import CURSOR_PATTERN, OperatorServices, current_status
from src.api.schemas import (
    AlertResolveRequest,
    ApprovalRequest,
    DecisionSummary,
    KillSwitchActivateRequest,
    KillSwitchReleaseRequest,
    ModeTransitionRequest,
    SystemStatus,
)
from src.api.security import CSRF_FIELD, SESSION_COOKIE, OperatorPrincipal
from src.api.stream import Feed, event_stream, status_feed, status_pulse
from src.domain.screening import CandidateStatus

_HERE = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
_MAX_FORM_BYTES = 16_384

DONE_MESSAGES = {
    "approved": "Команда подтверждена",
    "transition": "Режим изменён",
    "evidence": "Promotion evidence собран",
    "kill_switch_activated": "Kill switch включён",
    "kill_switch_released": "Kill switch снят",
    "alert_resolved": "Критическое событие закрыто, запись в аудите",
}

# A page holds one EventSource (``data-stream`` in base.html): the status pulse unless the
# page has a stream of its own that carries the pulse too.
STATUS_STREAM = "/api/v1/stream/status"
OVERVIEW_STREAM = "/stream/overview"

NAVIGATION = (
    ("/", "Обзор"),
    ("/pools", "Пулы"),
    ("/decisions", "Решения"),
    ("/approvals", "Подтверждения"),
    ("/positions", "Позиции"),
    ("/performance", "P&L"),
    ("/mode", "Режим"),
    ("/settings", "Настройки"),
)


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_decimal(value: Decimal | int | None, places: int = 6) -> str:
    if value is None:
        return "—"
    number = Decimal(value)
    try:
        number = number.quantize(Decimal(1).scaleb(-places))
    except InvalidOperation:
        return str(number)
    text = format(number.normalize(), "f")
    return "0" if text == "-0" else text


def _percent(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{_format_decimal(value * 100, 2)}%"


_PLAIN_NUMBER = re.compile(r"(-?)(\d+)(\.\d+)?")


def _grouped(text: str) -> str:
    """``1234567.5`` -> ``1 234 567.5`` (narrow no-break spaces); anything else as is."""

    match = _PLAIN_NUMBER.fullmatch(text)
    if match is None:
        return text
    sign, whole, fraction = match.groups()
    return f"{sign}{int(whole):,}".replace(",", " ") + (fraction or "")


def _short_address(value: str | None) -> str:
    if not value:
        return "—"
    return value if len(value) <= 12 else f"{value[:4]}…{value[-4:]}"


def _sign(value: Decimal | int | None) -> str:
    """CSS class of a money value: gains green, losses red, zero and unknown plain."""

    if value is None or value == 0:
        return ""
    return "pos" if value > 0 else "neg"


def _ago(seconds: int | None) -> str:
    """``95`` -> ``1 мин 35 с``: an age a viewer reads at a glance."""

    if seconds is None:
        return "—"
    # A clock a little ahead of the database's is "just now", not a negative age.
    seconds = max(seconds, 0)
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин {seconds % 60} с"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


# Token signals are already normalised by the domain (RiskSignal SAFE/UNSAFE, authority
# REVOKED/PRESENT), so one code has one colour wherever it appears.
_TONES = {
    "good": frozenset("OK FRESH ACCEPT OPEN READY COMPLETE CLEAR SAFE REVOKED".split()),
    "warn": frozenset(
        "DEGRADED STALE WARMING_UP UNKNOWN NOT_CONFIGURED NOT_SYNCED PARTIAL INCOMPLETE"
        " NO_DECISION APPROVAL".split()
    ),
    "bad": frozenset(
        "DOWN REJECT MISSING UNAVAILABLE EXPIRED FAILED LIVE UNSAFE PRESENT INVALID".split()
    ),
    "info": frozenset({"SHADOW"}),
}


def _tone(value: object) -> str:
    """Colour of a status pill for a code (``FRESH`` -> ``good``); unknown codes are plain."""

    code = str(getattr(value, "value", value) or "").upper()
    for tone, codes in _TONES.items():
        if code in codes:
            return tone
    return "plain"


def _static_version() -> str:
    """Digest of the panel's CSS/JS: ``?v=`` makes browsers drop a cached old version."""

    digest = hashlib.sha256()
    for path in sorted((_HERE / "static").iterdir()):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


TEMPLATES.env.filters["dt"] = _format_datetime
TEMPLATES.env.filters["dec"] = _format_decimal
TEMPLATES.env.filters["pct"] = _percent
TEMPLATES.env.filters["grp"] = _grouped
TEMPLATES.env.filters["short"] = _short_address
TEMPLATES.env.filters["sign"] = _sign
TEMPLATES.env.filters["tone"] = _tone
TEMPLATES.env.filters["ago"] = _ago
TEMPLATES.env.globals["labels"] = labels
TEMPLATES.env.globals["navigation"] = NAVIGATION
TEMPLATES.env.globals["static_version"] = _static_version()


def _services(request: Request) -> OperatorServices:
    services: OperatorServices = request.app.state.operator
    return services


async def render(
    request: Request, template: str, status_code: int = 200, **context: Any
) -> HTMLResponse:
    services = _services(request)
    session = services.auth.current_session(request)
    try:
        pulse = await status_pulse(services.context)
    except Exception:
        pulse = None
    values = {
        "request": request,
        "pulse": pulse,
        "logged_in": session is not None,
        "csrf_token": None if session is None else services.auth.csrf_token(session),
        "csrf_field": CSRF_FIELD,
        "auth_configured": services.auth.configured,
        "done": DONE_MESSAGES.get(request.query_params.get("done") or ""),
        "units": services.context.policy.quote_base_units_per_unit,
        "current_path": request.url.path,
        "stream": STATUS_STREAM,
        **context,
    }
    return TEMPLATES.TemplateResponse(request, template, values, status_code=status_code)


async def render_error(request: Request, error: ApiError) -> Response:
    try:
        return await render(
            request,
            "error.html",
            status_code=error.status_code,
            error=error,
            back=_back_link(request),
        )
    except Exception:
        return HTMLResponse(
            f"<h1>{error.status_code}</h1><p>{error.code}</p>", status_code=error.status_code
        )


async def read_form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Ожидается отправка HTML-формы")
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ApiError(413, "FORM_TOO_LARGE", "Слишком большая форма")
    try:
        parsed = parse_qs(body.decode("utf-8", errors="strict"), keep_blank_values=True)
    except UnicodeDecodeError as error:
        raise ApiError(400, "BAD_REQUEST", "Форма должна быть в UTF-8") from error
    return {key: values[-1] for key, values in parsed.items()}


def _checked(form: dict[str, str], name: str) -> bool:
    return form.get(name) in ("on", "true", "1", "yes")


def _redirect(path: str, done: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?done={quote(done)}", status_code=303)


def _cursor(request: Request) -> str | None:
    value = request.query_params.get("cursor")
    if value is None or value == "":
        return None
    if not re.fullmatch(CURSOR_PATTERN, value):
        raise ApiError(422, "INVALID_INPUT", "Некорректный курсор страницы")
    return value


def _validated[ModelT](build: Callable[[], ModelT]) -> ModelT:
    try:
        return build()
    except ValidationError as error:
        fields = [
            {"field": ".".join(str(part) for part in item["loc"]), "reason": item["msg"]}
            for item in error.errors()
        ]
        raise ApiError(
            422, "INVALID_INPUT", "Некорректные данные формы", {"fields": fields}
        ) from error


async def _authenticated(
    request: Request,
) -> tuple[OperatorServices, dict[str, str], OperatorPrincipal]:
    services = _services(request)
    form = await read_form(request)
    principal = services.auth.authenticate(request, csrf_token=form.get(CSRF_FIELD, ""))
    return services, form, principal


def install_web(app: FastAPI) -> None:
    router = APIRouter(include_in_schema=False)
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    @router.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        status, recent = await _overview(_services(request))
        return await render(
            request, "dashboard.html", status=status, recent=recent, stream=OVERVIEW_STREAM
        )

    @router.get(OVERVIEW_STREAM, response_class=StreamingResponse)
    async def overview_stream(request: Request) -> StreamingResponse:
        """The overview page's one stream: the status pulse and its live parts as HTML."""

        services = _services(request)
        ctx = services.context

        async def payload() -> str:
            status, recent = await _overview(services)
            macros = vars(TEMPLATES.env.get_template("_overview.html").module)
            parts = {
                "now": macros["now_block"](status),
                "feed": macros["decision_feed"](recent, status.generated_at),
            }
            return json.dumps({key: str(html) for key, html in parts.items()}, ensure_ascii=False)

        overview = Feed("overview", payload, every=ctx.settings.overview_every_ticks)
        return event_stream(ctx, request, [status_feed(ctx), overview])

    @router.get("/pools", response_class=HTMLResponse)
    async def pools(request: Request) -> Response:
        services = _services(request)
        query = (request.query_params.get("q") or "").strip()[:64] or None
        page = await services.reads.pools(
            offset=int(_cursor(request) or 0), limit=50, query=query
        )
        universe = await services.reads.universe_status()
        return await render(request, "pools.html", page=page, universe=universe, query=query)

    @router.get("/pools/{address}", response_class=HTMLResponse)
    async def pool(request: Request, address: str) -> Response:
        detail = await _services(request).reads.pool(address)
        return await render(request, "pool.html", detail=detail)

    @router.get("/decisions", response_class=HTMLResponse)
    async def decisions(request: Request) -> Response:
        services = _services(request)
        raw_status = request.query_params.get("status") or None
        try:
            status = None if raw_status is None else CandidateStatus(raw_status)
        except ValueError as error:
            raise ApiError(422, "INVALID_INPUT", "Неизвестный статус решения") from error
        pool_filter = request.query_params.get("pool") or None
        cursor = _cursor(request)
        page = await services.reads.decisions(
            cursor=None if cursor is None else int(cursor),
            limit=50,
            status=status,
            pool=pool_filter,
        )
        return await render(
            request,
            "decisions.html",
            page=page,
            status=raw_status,
            pool=pool_filter,
            statuses=[item.value for item in CandidateStatus],
        )

    @router.get("/decisions/{decision_id}", response_class=HTMLResponse)
    async def decision(request: Request, decision_id: UUID) -> Response:
        detail = await _services(request).reads.decision(decision_id)
        return await render(request, "decision.html", detail=detail)

    @router.get("/positions", response_class=HTMLResponse)
    async def positions(request: Request) -> Response:
        page = await _services(request).reads.positions(
            offset=int(_cursor(request) or 0), limit=50
        )
        return await render(request, "positions.html", page=page)

    @router.get("/positions/{position_id}", response_class=HTMLResponse)
    async def position(request: Request, position_id: str) -> Response:
        detail = await _services(request).reads.position(position_id)
        return await render(request, "position.html", detail=detail)

    @router.get("/performance", response_class=HTMLResponse)
    async def performance(request: Request) -> Response:
        raw = request.query_params.get("days") or "30"
        if not raw.isdigit() or not 1 <= int(raw) <= 365:
            raise ApiError(422, "INVALID_INPUT", "Окно должно быть от 1 до 365 дней")
        view = await _services(request).reads.performance(int(raw))
        return await render(request, "performance.html", view=view, days=int(raw))

    @router.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request) -> Response:
        services = _services(request)
        config = await services.reads.effective_config()
        journal = await services.reads.mode_journal()
        status = await current_status(services)
        return await render(
            request, "settings.html", config=config, journal=journal, alerts=status.alerts
        )

    @router.get("/approvals", response_class=HTMLResponse)
    async def approvals(request: Request) -> Response:
        queue = await _services(request).approvals.queue()
        return await render(request, "approvals.html", queue=queue)

    @router.get("/approvals/{command_id}", response_class=HTMLResponse)
    async def approval(request: Request, command_id: UUID) -> Response:
        preview = await _services(request).approvals.preview(command_id)
        return await render(request, "approval.html", preview=preview, approval_id=uuid4())

    @router.post("/approvals/{command_id}")
    async def approve(request: Request, command_id: UUID) -> Response:
        services, form, principal = await _authenticated(request)
        body = _validated(
            lambda: ApprovalRequest.model_validate(
                {
                    "approval_id": form.get("approval_id", ""),
                    "command_digest": form.get("command_digest", ""),
                    "simulation_digest": form.get("simulation_digest", ""),
                    "checklist": {key: _checked(form, key) for key, _ in labels.CHECKLIST},
                    "comment": form.get("comment", ""),
                    "operator_token": form.get("operator_token") or None,
                    "confirm_irreversible": _checked(form, "confirm_irreversible"),
                }
            )
        )
        await services.approvals.approve(command_id, body, principal)
        return _redirect(f"/approvals/{command_id}", "approved")

    @router.get("/mode", response_class=HTMLResponse)
    async def mode(request: Request) -> Response:
        services = _services(request)
        journal = await services.reads.mode_journal()
        promotion = await services.actions.promotion()
        evidence = None
        raw_evidence = request.query_params.get("evidence")
        if raw_evidence and _is_uuid(raw_evidence):
            evidence = await services.actions.shadow_evidence(UUID(raw_evidence))
        return await render(
            request, "mode.html", journal=journal, promotion=promotion, evidence=evidence
        )

    @router.post("/mode/transitions")
    async def transition(request: Request) -> Response:
        services, form, principal = await _authenticated(request)
        raw_evidence = form.get("evidence_id") or None
        body = _validated(
            lambda: ModeTransitionRequest.model_validate(
                {
                    "target_mode": form.get("target_mode", ""),
                    "reason": form.get("reason", ""),
                    "evidence_id": raw_evidence,
                    "confirmation_id": form.get("confirmation_id") or None,
                    "operator_token": form.get("operator_token") or None,
                    "confirm": _checked(form, "confirm"),
                }
            )
        )
        await services.actions.transition(body, principal)
        return _redirect("/mode", "transition")

    @router.post("/mode/evidence")
    async def evidence(request: Request) -> Response:
        services, _, principal = await _authenticated(request)
        view = await services.actions.build_shadow_evidence(principal)
        return RedirectResponse(f"/mode?evidence={view.evidence_id}&done=evidence", 303)

    @router.post("/kill-switch/activate")
    async def kill_switch_activate(request: Request) -> Response:
        services, form, principal = await _authenticated(request)
        body = _validated(
            lambda: KillSwitchActivateRequest.model_validate(
                {
                    "reason": form.get("reason", ""),
                    "policy": form.get("policy") or "HOLD_AND_ALERT",
                }
            )
        )
        await services.actions.activate_kill_switch(body, principal)
        return _redirect("/mode", "kill_switch_activated")

    @router.post("/kill-switch/release")
    async def kill_switch_release(request: Request) -> Response:
        services, form, principal = await _authenticated(request)
        body = _validated(
            lambda: KillSwitchReleaseRequest(
                reason=form.get("reason", ""),
                operator_token=form.get("operator_token") or None,
                confirm=_checked(form, "confirm"),
            )
        )
        await services.actions.release_kill_switch(body, principal)
        return _redirect("/mode", "kill_switch_released")

    @router.post("/alerts/{alert_id}/resolve")
    async def alert_resolve(request: Request, alert_id: UUID) -> Response:
        services, form, principal = await _authenticated(request)
        body = _validated(
            lambda: AlertResolveRequest(
                reason=form.get("reason", ""),
                operator_token=form.get("operator_token") or None,
                confirm=_checked(form, "confirm"),
            )
        )
        await services.actions.resolve_alert(alert_id, body, principal)
        return _redirect("/settings", "alert_resolved")

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        target = _safe_next(request.query_params.get("next"))
        return await render(request, "login.html", next=target)

    @router.post("/login")
    async def login(request: Request) -> Response:
        services = _services(request)
        form = await read_form(request)
        target = _safe_next(form.get("next"))
        if not services.auth.configured:
            raise ApiError(
                503,
                "OPERATOR_AUTH_NOT_CONFIGURED",
                "Вход закрыт: не задан токен оператора (APP_OPERATOR_TOKEN)",
            )
        if not services.auth.verify_token(form.get("token")):
            return await render(
                request, "login.html", status_code=401, next=target, failed=True
            )
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            services.auth.issue_session(),
            max_age=services.auth.session_max_age(),
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @router.post("/logout")
    async def logout(request: Request) -> Response:
        await _authenticated(request)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    app.include_router(router)
    install_error_handlers(app, render_error)


async def _overview(
    services: OperatorServices,
) -> tuple[SystemStatus, list[DecisionSummary]]:
    """What the overview shows live: the full status and the eight latest decisions."""

    status = await current_status(services)
    recent = await services.reads.decisions(cursor=None, limit=8, status=None, pool=None)
    return status, recent.items


def _back_link(request: Request) -> str:
    """Where an error page leads back: always a path of this panel (``//host`` is not)."""

    path = _safe_next(request.url.path)
    if request.method == "GET":
        return path
    if path.startswith(("/mode", "/kill-switch")):
        return "/mode"
    if path.startswith("/alerts/"):
        return "/settings"
    if path.startswith("/approvals/"):
        return path
    return "/"


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True



