"""Operator authentication, web-session CSRF and re-authentication for money actions.

* API clients send ``Authorization: Bearer <APP_OPERATOR_TOKEN>``; no ambient
  credential is involved, so CSRF does not apply to them.
* The web panel exchanges the token once for an HMAC-signed, HttpOnly, SameSite=Strict
  session cookie; every state-changing web request must then carry the session's CSRF
  token (form field or ``X-CSRF-Token``).
* Money actions (approval, promotion, kill-switch release) additionally re-check the
  operator token typed again and an explicit confirmation flag.

Without ``APP_OPERATOR_TOKEN`` every state-changing endpoint is closed (fail-closed).
Token values are compared in constant time and never logged or echoed.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import Request

from src.api.errors import ApiError
from src.api.settings import OperatorSettings
from src.core.clock import Clock

SESSION_COOKIE = "mvb_session"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    method: Literal["bearer", "session"]
    session_id: str | None = None

    @property
    def confirmation_id(self) -> str:
        """Audit label of who confirmed an action; never contains the token."""

        suffix = self.session_id[:8] if self.session_id else "api"
        return f"operator:{self.method}:{suffix}"


class OperatorAuth:
    def __init__(self, settings: OperatorSettings, clock: Clock) -> None:
        self._settings = settings
        self._clock = clock

    @property
    def configured(self) -> bool:
        return self._settings.auth_configured

    def verify_token(self, candidate: str | None) -> bool:
        token = self._settings.operator_token
        if token is None or not candidate:
            return False
        return hmac.compare_digest(
            candidate.encode("utf-8"), token.get_secret_value().encode("utf-8")
        )

    # --- web session ------------------------------------------------------------------

    def issue_session(self) -> str:
        session_id = secrets.token_urlsafe(18)
        issued = str(int(self._clock.now().timestamp()))
        return f"{session_id}.{issued}.{self._sign('session', session_id, issued)}"

    def session_id(self, cookie: str | None) -> str | None:
        if not cookie or cookie.count(".") != 2:
            return None
        session_id, issued, signature = cookie.split(".")
        if not hmac.compare_digest(signature, self._sign("session", session_id, issued)):
            return None
        try:
            issued_at = int(issued)
        except ValueError:
            return None
        age = self._clock.now().timestamp() - issued_at
        if age < 0 or age > self._settings.session_ttl.total_seconds():
            return None
        return session_id

    def csrf_token(self, session_id: str) -> str:
        return self._sign("csrf", session_id)

    def session_max_age(self) -> int:
        return int(self._settings.session_ttl.total_seconds())

    # --- request checks ---------------------------------------------------------------

    def current_session(self, request: Request) -> str | None:
        return self.session_id(request.cookies.get(SESSION_COOKIE))

    def authenticate(self, request: Request, *, csrf_token: str | None = None) -> OperatorPrincipal:
        """Principal of a state-changing request, or a refusal (401/403/503)."""

        if not self.configured:
            raise ApiError(
                503,
                "OPERATOR_AUTH_NOT_CONFIGURED",
                "Действия закрыты: не задан токен оператора (APP_OPERATOR_TOKEN)",
            )
        header = request.headers.get("authorization", "")
        if header:
            scheme, _, value = header.partition(" ")
            if scheme.lower() != "bearer" or not self.verify_token(value.strip()):
                raise ApiError(401, "UNAUTHORIZED", "Неверный токен оператора")
            return OperatorPrincipal("bearer")
        session_id = self.current_session(request)
        if session_id is None:
            raise ApiError(401, "UNAUTHORIZED", "Нужен вход оператора")
        supplied = csrf_token if csrf_token is not None else request.headers.get(CSRF_HEADER)
        if not supplied or not hmac.compare_digest(supplied, self.csrf_token(session_id)):
            raise ApiError(403, "CSRF_FAILED", "CSRF-токен отсутствует или неверен")
        return OperatorPrincipal("session", session_id)

    def require_reauthentication(self, token: str | None, confirmed: bool) -> None:
        """Second, explicit confirmation of an irreversible or risk-raising action."""

        if not confirmed:
            raise ApiError(
                403, "CONFIRMATION_REQUIRED", "Нужно явное подтверждение действия оператором"
            )
        if not self.verify_token(token):
            raise ApiError(
                403, "REAUTHENTICATION_FAILED", "Повторный ввод токена оператора не совпал"
            )

    def _sign(self, purpose: str, *parts: str) -> str:
        message = ":".join((purpose, *parts)).encode("utf-8")
        return hmac.new(self._settings.session_key, message, hashlib.sha256).hexdigest()
