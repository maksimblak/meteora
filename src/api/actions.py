"""Mode transitions, kill switch, critical alerts and promotion evidence (G01, G03).

Downgrades and kill-switch activation apply immediately for an authenticated
operator; promotions, kill-switch release and closing a critical alert also require
re-authentication and an explicit confirmation. The gates themselves stay in
``ModeService`` (T04): this layer never decides a promotion, it only refuses to ask
without the operator's consent.
"""

from datetime import datetime
from uuid import UUID, uuid4

from src.api import presenters
from src.api.context import OperatorContext
from src.api.errors import ApiError
from src.api.schemas import (
    AlertResolveRequest,
    AlertView,
    KillSwitchActivateRequest,
    KillSwitchReleaseRequest,
    KillSwitchResultView,
    LiveReadinessView,
    ModeTransitionRequest,
    ModeTransitionResult,
    ModeView,
    Notice,
    PositionActionView,
    PromotionView,
    ShadowReadinessView,
)
from src.api.security import OperatorAuth, OperatorPrincipal
from src.domain.models.events import AuditEvent, EventType
from src.domain.models.trading import TradingMode
from src.execution.contracts import LivePromotionEvidence
from src.execution.settings import ExecutorSettings
from src.modes.models import (
    MINIMUM_SHADOW_DAYS,
    RECOMMENDED_SHADOW_DAYS,
    CriticalAlert,
    ModeState,
    PromotionEvidenceSnapshot,
)
from src.modes.repository import KILL_SWITCH_ALERT
from src.modes.service import ModeService, ModeTransitionRejected, PromotionEvidenceService
from src.observability.logging import error_fields, get_logger

_RANK = {TradingMode.SHADOW: 0, TradingMode.APPROVAL: 1, TradingMode.LIVE: 2}
_LIVE_FLAGS = (
    ("receipts_reconciled", "LIVE_RECEIPTS_NOT_RECONCILED", "receipts не сверены с учётом"),
    ("risk_invariants_preserved", "LIVE_RISK_INVARIANTS_BROKEN", "нарушены risk-инварианты"),
    ("dedicated_rpc_ready", "LIVE_RPC_NOT_READY", "dedicated RPC не готов"),
    ("wallet_ready", "LIVE_WALLET_NOT_READY", "кошелёк не готов"),
    ("checklist_completed", "LIVE_CHECKLIST_INCOMPLETE", "чеклист оператора не пройден"),
)


class OperatorActions:
    def __init__(self, context: OperatorContext, auth: OperatorAuth) -> None:
        self._ctx = context
        self._auth = auth
        self._log = get_logger("api.actions")

    def _modes(self) -> ModeService:
        return ModeService(
            self._ctx.repositories.mode, self._ctx.clock, self._ctx.policy.ruleset_version
        )

    async def transition(
        self, request: ModeTransitionRequest, principal: OperatorPrincipal
    ) -> ModeTransitionResult:
        ctx = self._ctx
        state = await ctx.mode_state()
        target = request.target_mode
        if target is state.mode:
            raise ApiError(409, "MODE_ALREADY_ACTIVE", "Этот режим уже активен")
        evidence: PromotionEvidenceSnapshot | LivePromotionEvidence | None = None
        confirmation = principal.confirmation_id
        if _RANK[target] > _RANK[state.mode]:
            self._auth.require_reauthentication(request.operator_token, request.confirm)
            if target is TradingMode.APPROVAL:
                evidence = await self._shadow_evidence(request.evidence_id)
            else:
                evidence = await self._live_evidence()
                if evidence is None:
                    raise ApiError(
                        409,
                        "LIVE_EVIDENCE_MISSING",
                        "Нет действующего LIVE promotion evidence (LIVE_PROMOTION_FILE)",
                    )
                if not request.confirmation_id:
                    raise ApiError(
                        422,
                        "CONFIRMATION_ID_REQUIRED",
                        "Введите идентификатор подтверждения из LIVE promotion evidence",
                    )
                confirmation = request.confirmation_id
        try:
            transition = await ctx.sync(
                self._modes().transition, target, evidence, confirmation, request.reason
            )
        except ModeTransitionRejected as error:
            raise ApiError(
                409, "MODE_TRANSITION_REJECTED", "Переход режима отклонён", {"reason": str(error)}
            ) from error
        self._log.info(
            "mode_transition",
            from_mode=transition.from_mode.value,
            to_mode=transition.to_mode.value,
            operator=principal.confirmation_id,
        )
        return ModeTransitionResult(
            transition=presenters.transition_view(transition),
            mode=presenters.mode_view(await ctx.mode_state()),
        )

    async def activate_kill_switch(
        self, request: KillSwitchActivateRequest, principal: OperatorPrincipal
    ) -> KillSwitchResultView:
        try:
            result = await self._ctx.sync(
                self._modes().activate_kill_switch, request.reason, request.policy
            )
        except ValueError as error:
            raise ApiError(
                409, "KILL_SWITCH_REJECTED", "Kill switch не включён", {"reason": str(error)}
            ) from error
        self._log.warning(
            "kill_switch_activated", policy=request.policy.value, operator=principal.confirmation_id
        )
        return KillSwitchResultView(
            activated_at=result.activated_at,
            policy=result.policy,
            cancelled_plan_count=result.cancelled_plan_count,
            position_actions=[
                PositionActionView(position_id=item.position_id, action=item.action.value)
                for item in result.position_actions
            ],
            mode=presenters.mode_view(await self._ctx.mode_state()),
        )

    async def release_kill_switch(
        self, request: KillSwitchReleaseRequest, principal: OperatorPrincipal
    ) -> ModeView:
        self._auth.require_reauthentication(request.operator_token, request.confirm)
        try:
            state = await self._ctx.sync(
                self._modes().release_kill_switch, request.reason, principal.confirmation_id
            )
        except ValueError as error:
            raise ApiError(
                409,
                "KILL_SWITCH_RELEASE_REJECTED",
                "Kill switch не снят",
                {"reason": str(error)},
            ) from error
        self._log.warning("kill_switch_released", operator=principal.confirmation_id)
        return presenters.mode_view(state)

    async def resolve_alert(
        self, alert_id: UUID, request: AlertResolveRequest, principal: OperatorPrincipal
    ) -> AlertView:
        """Close a critical alert (G13.2): re-authentication, a reason, an audit record first.

        Any open critical alert blocks the promotion to APPROVAL (spec 7.1), so this is
        guarded like the kill-switch release. The audit record precedes the state change;
        a refusal after it is audited too, so the trail never shows a silent close.
        """

        self._auth.require_reauthentication(request.operator_token, request.confirm)
        ctx = self._ctx
        alerts = await ctx.sync(ctx.repositories.mode.alerts)
        alert = next((item for item in alerts if item.alert_id == alert_id), None)
        if alert is None:
            raise ApiError(404, "ALERT_NOT_FOUND", "Критическое событие не найдено")
        refusal = _resolve_refusal(alert, await ctx.mode_state())
        if refusal is not None:
            raise ApiError(
                409, "ALERT_RESOLVE_REJECTED", "Событие не закрыто", {"reason": refusal}
            )
        resolved_event = _alert_event(
            alert,
            ctx.clock.now(),
            {"stage": "resolved", "reason": request.reason, "operator": principal.confirmation_id},
        )
        try:
            await ctx.repositories.audit.append(resolved_event)
        except Exception as error:
            self._log.critical("alert_resolve_audit_failed", **error_fields(error))
            raise ApiError(
                503, "AUDIT_UNAVAILABLE", "Аудит недоступен: событие не закрыто"
            ) from error
        try:
            resolved = await ctx.sync(
                self._modes().resolve_alert, alert_id, request.reason, principal.confirmation_id
            )
        except ValueError as error:
            rejected = _alert_event(
                alert, ctx.clock.now(), {"stage": "resolve_rejected", "reason": str(error)}
            ).model_copy(update={"causation_id": resolved_event.event_id})
            try:
                await ctx.repositories.audit.append(rejected)
            except Exception as audit_error:
                self._log.error("alert_refusal_audit_failed", **error_fields(audit_error))
            raise ApiError(
                409, "ALERT_RESOLVE_REJECTED", "Событие не закрыто", {"reason": str(error)}
            ) from error
        self._log.warning(
            "critical_alert_resolved",
            alert_id=str(alert_id),
            code=alert.code,
            operator=principal.confirmation_id,
        )
        return presenters.alert_view(resolved)

    async def build_shadow_evidence(self, principal: OperatorPrincipal) -> ShadowReadinessView:
        ctx = self._ctx
        service = PromotionEvidenceService(ctx.repositories.mode, ctx.clock)
        try:
            evidence = await ctx.sync(service.build_shadow, ctx.policy.ruleset_version)
        except ValueError as error:
            raise ApiError(
                409, "EVIDENCE_REJECTED", "Evidence не собран", {"reason": str(error)}
            ) from error
        self._log.info("shadow_evidence_built", operator=principal.confirmation_id)
        return self._readiness(evidence)

    async def shadow_evidence(self, evidence_id: UUID) -> ShadowReadinessView:
        evidence = await self._ctx.sync(self._ctx.repositories.mode.promotion_evidence, evidence_id)
        if evidence is None:
            raise ApiError(404, "EVIDENCE_NOT_FOUND", "Promotion evidence не найден")
        return self._readiness(evidence)

    async def promotion(self) -> PromotionView:
        ctx = self._ctx
        state = await ctx.mode_state()
        observations = await ctx.sync(
            ctx.repositories.mode.observations, ctx.policy.ruleset_version, TradingMode.SHADOW
        )
        return PromotionView(
            mode=presenters.mode_view(state),
            observation_days=len(observations),
            minimum_days=MINIMUM_SHADOW_DAYS,
            recommended_days=RECOMMENDED_SHADOW_DAYS,
            live=await self._live_readiness(),
        )

    # --- helpers ----------------------------------------------------------------------

    async def _shadow_evidence(self, evidence_id: UUID | None) -> PromotionEvidenceSnapshot:
        if evidence_id is None:
            raise ApiError(
                422, "EVIDENCE_REQUIRED", "Для перехода в APPROVAL соберите promotion evidence"
            )
        evidence = await self._ctx.sync(self._ctx.repositories.mode.promotion_evidence, evidence_id)
        if evidence is None:
            raise ApiError(404, "EVIDENCE_NOT_FOUND", "Promotion evidence не найден")
        return evidence

    async def _live_evidence(self) -> LivePromotionEvidence | None:
        """The operator's LIVE record file is read off the event loop."""

        environ = self._ctx.environ

        def read() -> LivePromotionEvidence | None:
            return ExecutorSettings.from_environment(environ).live_promotion()

        return await self._ctx.sync(read)

    async def _live_readiness(self) -> LiveReadinessView:
        evidence = await self._live_evidence()
        if evidence is None:
            return LiveReadinessView(
                evidence_present=False,
                ready=False,
                reasons=[
                    Notice(
                        code="LIVE_EVIDENCE_MISSING",
                        message="нет LIVE promotion evidence (LIVE_PROMOTION_FILE)",
                    )
                ],
                approval_cycles=None,
                expires_at=None,
            )
        now = self._ctx.clock.now()
        reasons = [
            Notice(code=code, message=text)
            for field, code, text in _LIVE_FLAGS
            if not getattr(evidence, field)
        ]
        if evidence.approval_cycles < 10:
            reasons.append(
                Notice(code="LIVE_APPROVAL_CYCLES_BELOW_10", message="меньше 10 циклов APPROVAL")
            )
        if evidence.ruleset_version != self._ctx.policy.ruleset_version:
            reasons.append(
                Notice(
                    code="LIVE_EVIDENCE_RULESET_STALE",
                    message="evidence для другой версии правил",
                )
            )
        if evidence.promoted_at > now or evidence.expires_at <= now:
            reasons.append(Notice(code="LIVE_EVIDENCE_EXPIRED", message="evidence просрочен"))
        return LiveReadinessView(
            evidence_present=True,
            ready=not reasons,
            reasons=reasons,
            approval_cycles=evidence.approval_cycles,
            expires_at=evidence.expires_at,
        )

    def _readiness(self, evidence: PromotionEvidenceSnapshot) -> ShadowReadinessView:
        readiness = self._modes().shadow_readiness(evidence)
        return ShadowReadinessView(
            evidence_id=evidence.evidence_id,
            generated_at=evidence.generated_at,
            allowed=readiness.allowed,
            reasons=presenters.readiness_notices(readiness.reasons),
            full_shadow_days=readiness.full_shadow_days,
            minimum_days=readiness.minimum_days,
            recommended_days=readiness.recommended_days,
            minimum_days_remaining=readiness.minimum_days_remaining,
            recommended_days_remaining=readiness.recommended_days_remaining,
            completed_positions=readiness.completed_positions,
            decision_count=readiness.decision_count,
            insufficient_statistics=readiness.insufficient_statistics,
            net_expectancy=readiness.net_expectancy,
            maximum_drawdown=readiness.maximum_drawdown,
            data_coverage=readiness.data_coverage,
            open_critical_alerts=readiness.open_critical_alerts,
            consumed=evidence.consumed_at is not None,
        )


def _resolve_refusal(alert: CriticalAlert, state: ModeState) -> str | None:
    """Why an alert cannot be closed now (the repository applies the same rules)."""

    if not alert.open:
        return "alert already resolved"
    if alert.code == KILL_SWITCH_ALERT and state.kill_switch_active:
        return "release the kill switch to resolve its alert"
    return None


def _alert_event(
    alert: CriticalAlert, occurred_at: datetime, payload: dict[str, object]
) -> AuditEvent:
    """The alert's audit stream: ``CriticalAlertRaised`` with ``stage`` (raised -> resolved),
    as approvals use ``ApprovalRequested`` with ``stage=approved``."""

    return AuditEvent(
        event_id=uuid4(),
        occurred_at=occurred_at,
        aggregate_id=alert.alert_id,
        correlation_id=uuid4(),
        causation_id=None,
        schema_version=1,
        event_type=EventType.CRITICAL_ALERT_RAISED,
        payload={"alert_id": str(alert.alert_id), "code": alert.code, **payload},
    )
