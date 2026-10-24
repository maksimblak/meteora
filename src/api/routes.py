"""``/api/v1``: read endpoints are open to the local operator, writes need auth (5.6)."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse

from src.api.actions import OperatorActions
from src.api.approvals import ApprovalDesk
from src.api.context import OperatorContext
from src.api.errors import ERROR_RESPONSES
from src.api.reads import OperatorReads
from src.api.schemas import (
    AlertResolveRequest,
    AlertView,
    ApprovalPreview,
    ApprovalQueue,
    ApprovalRequest,
    ApprovalResult,
    DecisionDetail,
    DecisionSummary,
    EffectiveConfig,
    KillSwitchActivateRequest,
    KillSwitchReleaseRequest,
    KillSwitchResultView,
    ModeJournal,
    ModeTransitionRequest,
    ModeTransitionResult,
    ModeView,
    Page,
    PerformanceView,
    PoolDetail,
    PoolSummary,
    PositionDetail,
    PositionSummary,
    PromotionView,
    ShadowReadinessView,
    SystemStatus,
)
from src.api.security import OperatorAuth, OperatorPrincipal
from src.api.stream import status_stream
from src.domain.screening import CandidateStatus

CURSOR_PATTERN = r"^[0-9]{1,18}$"
ADDRESS_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"
POSITION_PATTERN = r"^[A-Za-z0-9:_\-]{1,128}$"


@dataclass(frozen=True, slots=True)
class OperatorServices:
    context: OperatorContext
    auth: OperatorAuth
    reads: OperatorReads
    actions: OperatorActions
    approvals: ApprovalDesk


def operator_services(request: Request) -> OperatorServices:
    services: OperatorServices = request.app.state.operator
    return services


def operator_principal(
    request: Request, services: Annotated[OperatorServices, Depends(operator_services)]
) -> OperatorPrincipal:
    return services.auth.authenticate(request)


Services = Annotated[OperatorServices, Depends(operator_services)]
Principal = Annotated[OperatorPrincipal, Depends(operator_principal)]
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(pattern=CURSOR_PATTERN)]

router = APIRouter(prefix="/api/v1", responses=ERROR_RESPONSES)


@router.get("/system/status", response_model=SystemStatus, tags=["status"])
async def system_status(services: Services) -> SystemStatus:
    return await current_status(services)


async def current_status(services: OperatorServices) -> SystemStatus:
    """The dashboard status with the universe summary read once for the whole request."""

    universe = await services.context.universe()
    return await services.reads.status(
        approval_blockers=await services.approvals.global_blockers(universe=universe),
        universe=universe,
    )


@router.get("/stream/status", response_class=StreamingResponse, tags=["status"])
async def stream_status(request: Request, services: Services) -> StreamingResponse:
    return status_stream(services.context, request)


@router.get("/pools", response_model=Page[PoolSummary], tags=["pools"])
async def list_pools(
    services: Services,
    cursor: Cursor = None,
    limit: Limit = 50,
    q: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[PoolSummary]:
    return await services.reads.pools(offset=int(cursor or 0), limit=limit, query=q)


@router.get("/pools/{address}", response_model=PoolDetail, tags=["pools"])
async def get_pool(
    services: Services, address: Annotated[str, Path(pattern=ADDRESS_PATTERN)]
) -> PoolDetail:
    return await services.reads.pool(address)


@router.get("/decisions", response_model=Page[DecisionSummary], tags=["decisions"])
async def list_decisions(
    services: Services,
    cursor: Cursor = None,
    limit: Limit = 50,
    status: CandidateStatus | None = None,
    pool: Annotated[str | None, Query(pattern=ADDRESS_PATTERN)] = None,
) -> Page[DecisionSummary]:
    return await services.reads.decisions(
        cursor=None if cursor is None else int(cursor), limit=limit, status=status, pool=pool
    )


@router.get("/decisions/{decision_id}", response_model=DecisionDetail, tags=["decisions"])
async def get_decision(services: Services, decision_id: UUID) -> DecisionDetail:
    return await services.reads.decision(decision_id)


@router.get("/positions", response_model=Page[PositionSummary], tags=["positions"])
async def list_positions(
    services: Services, cursor: Cursor = None, limit: Limit = 50
) -> Page[PositionSummary]:
    return await services.reads.positions(offset=int(cursor or 0), limit=limit)


@router.get("/positions/{position_id}", response_model=PositionDetail, tags=["positions"])
async def get_position(
    services: Services, position_id: Annotated[str, Path(pattern=POSITION_PATTERN)]
) -> PositionDetail:
    return await services.reads.position(position_id)


@router.get("/performance", response_model=PerformanceView, tags=["performance"])
async def performance(
    services: Services, days: Annotated[int, Query(ge=1, le=365)] = 30
) -> PerformanceView:
    return await services.reads.performance(days)


@router.get("/config/effective", response_model=EffectiveConfig, tags=["config"])
async def effective_config(services: Services) -> EffectiveConfig:
    return await services.reads.effective_config()


@router.get("/approvals", response_model=ApprovalQueue, tags=["approvals"])
async def approval_queue(services: Services) -> ApprovalQueue:
    return await services.approvals.queue()


@router.get("/approvals/{command_id}", response_model=ApprovalPreview, tags=["approvals"])
async def approval_preview(services: Services, command_id: UUID) -> ApprovalPreview:
    return await services.approvals.preview(command_id)


@router.post("/approvals/{command_id}", response_model=ApprovalResult, tags=["approvals"])
async def approve(
    services: Services, principal: Principal, command_id: UUID, body: ApprovalRequest
) -> ApprovalResult:
    return await services.approvals.approve(command_id, body, principal)


@router.get("/mode", response_model=ModeJournal, tags=["mode"])
async def mode_journal(services: Services) -> ModeJournal:
    return await services.reads.mode_journal()


@router.post("/mode/transitions", response_model=ModeTransitionResult, tags=["mode"])
async def transition_mode(
    services: Services, principal: Principal, body: ModeTransitionRequest
) -> ModeTransitionResult:
    return await services.actions.transition(body, principal)


@router.post("/kill-switch/activate", response_model=KillSwitchResultView, tags=["mode"])
async def activate_kill_switch(
    services: Services, principal: Principal, body: KillSwitchActivateRequest
) -> KillSwitchResultView:
    return await services.actions.activate_kill_switch(body, principal)


@router.post("/kill-switch/release", response_model=ModeView, tags=["mode"])
async def release_kill_switch(
    services: Services, principal: Principal, body: KillSwitchReleaseRequest
) -> ModeView:
    return await services.actions.release_kill_switch(body, principal)


@router.post("/alerts/{alert_id}/resolve", response_model=AlertView, tags=["mode"])
async def resolve_alert(
    services: Services, principal: Principal, alert_id: UUID, body: AlertResolveRequest
) -> AlertView:
    return await services.actions.resolve_alert(alert_id, body, principal)


@router.get("/promotion", response_model=PromotionView, tags=["promotion"])
async def promotion(services: Services) -> PromotionView:
    return await services.actions.promotion()


@router.post("/promotion/evidence", response_model=ShadowReadinessView, tags=["promotion"])
async def build_evidence(services: Services, principal: Principal) -> ShadowReadinessView:
    return await services.actions.build_shadow_evidence(principal)


@router.get(
    "/promotion/evidence/{evidence_id}", response_model=ShadowReadinessView, tags=["promotion"]
)
async def get_evidence(services: Services, evidence_id: UUID) -> ShadowReadinessView:
    return await services.actions.shadow_evidence(evidence_id)
