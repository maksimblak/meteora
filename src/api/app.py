"""Operator application: T01 process shell + ``/api/v1`` + the Russian web panel (G13).

``create_operator_app`` builds on ``src.core.app.create_app`` (health, readiness,
metrics), opens repositories through the single factory (D01) unless given, and wires
approvals to ``ExecutionService`` over the HTTP gateway (T09) when the deployment is
configured for it. ``src.api.main:app`` is the ASGI entry point.
"""

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from src.api.actions import OperatorActions
from src.api.approvals import ApprovalDesk, ApprovalWiring
from src.api.catalog import DecisionCatalog, decision_catalog
from src.api.context import OperatorContext
from src.api.metrics import RequestMetrics
from src.api.probes import GatewayProbe, ServiceProbe, WorkerHealthProbe
from src.api.reads import OperatorReads
from src.api.routes import OperatorServices, router
from src.api.security import OperatorAuth
from src.api.settings import OperatorSettings
from src.core.app import create_app
from src.core.clock import Clock, SystemClock
from src.core.config import AppConfig
from src.domain.models.trading import TradingMode
from src.execution.service import ExecutionService
from src.execution.settings import ExecutorSettings
from src.persistence.factory import PersistenceBackend, Repositories, open_repositories
from src.web.routes import install_web
from src.worker.execution import CommandResolver, ModeKillSwitch
from src.worker.policy import PipelinePolicy, load_pipeline_policy
from src.worker.runtime import build_gateway


@dataclass(frozen=True, slots=True)
class ApprovalSetup:
    wiring: ApprovalWiring | None
    unavailable: tuple[str, ...]
    client: httpx.AsyncClient | None = None


def build_approval_setup(
    config: AppConfig,
    environ: Mapping[str, str],
    repositories: Repositories,
    clock: Clock,
    policy: PipelinePolicy,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ApprovalSetup:
    """The same gateway the worker uses (``TRADING_MODE``), plus the approval secret."""

    reasons: list[str] = []
    if config.requested_mode is TradingMode.SHADOW:
        reasons.append("TRADING_MODE_SHADOW")
    secret = ExecutorSettings.from_environment(environ).approval_secret
    if secret is None:
        reasons.append("APPROVAL_SECRET_NOT_CONFIGURED")
    gateway, client = build_gateway(config, environ, repositories, transport=transport)
    if gateway is None and config.requested_mode is not TradingMode.SHADOW:
        reasons.append(
            "DURABLE_EXECUTION_STORE_REQUIRED"
            if repositories.backend is not PersistenceBackend.POSTGRESQL
            else "EXECUTION_GATEWAY_NOT_CONFIGURED"
        )
    if gateway is None or secret is None:
        return ApprovalSetup(None, tuple(reasons), client)
    service = ExecutionService(
        gateway=gateway.gateway,
        store=repositories.execution,
        plans=CommandResolver(repositories.execution),
        clock=clock,
        allowed_mode=gateway.allowed_mode,
        simulation_ttl=gateway.simulation_ttl,
        approval_verifier=gateway.approval_verifier,
        live_promotion=gateway.live_promotion,
        receipts=gateway.receipts,
        kill_switch=ModeKillSwitch(repositories.mode),
        kill_switch_policy=policy.kill_switch_policy,
    )
    wiring = ApprovalWiring(
        service=service,
        approval_secret=secret,
        simulation_ttl=gateway.simulation_ttl,
        readiness=gateway.readiness,
    )
    return ApprovalSetup(wiring, (), client)


def create_operator_app(
    *,
    config: AppConfig | None = None,
    environ: Mapping[str, str] | None = None,
    repositories: Repositories | None = None,
    clock: Clock | None = None,
    policy: PipelinePolicy | None = None,
    settings: OperatorSettings | None = None,
    approvals: ApprovalSetup | None = None,
    catalog: DecisionCatalog | None = None,
    probes: tuple[ServiceProbe, ...] | None = None,
) -> FastAPI:
    environment = os.environ if environ is None else environ
    app_config = config or AppConfig.load(environ=environment)
    clock = clock or SystemClock()
    policy = policy or load_pipeline_policy(environment)
    operator_settings = settings or OperatorSettings.from_environment(app_config, environment)
    owns_repositories = repositories is None
    stores = repositories or open_repositories(app_config, environ=environment, clock=clock)
    try:
        setup = approvals or build_approval_setup(
            app_config, environment, stores, clock, policy
        )
        wiring = setup.wiring

        async def current_mode() -> TradingMode:
            return (await run_in_threadpool(stores.mode.mode_state)).mode

        context = OperatorContext(
            config=app_config,
            environ=environment,
            settings=operator_settings,
            repositories=stores,
            catalog=catalog or decision_catalog(stores),
            policy=policy,
            clock=clock,
            probes=probes
            if probes is not None
            else (
                WorkerHealthProbe(
                    operator_settings.worker_health_url,
                    timeout_seconds=operator_settings.probe_timeout.total_seconds(),
                ),
                GatewayProbe(None if wiring is None else wiring.readiness, current_mode),
            ),
        )
    except BaseException:
        if owns_repositories:
            stores.close()
        raise

    application = create_app(config=app_config, readiness_probe=stores.ping)
    application.title = "Meteora Volume Bot — операторская панель"
    application.description = (
        "Versioned operator API (/api/v1) and Russian web panel. Reads are open to the "
        "local operator; every write needs the operator token (Bearer) or a web session "
        "with CSRF; approvals, promotions and kill-switch release need re-authentication."
    )
    auth = OperatorAuth(operator_settings, clock)
    application.state.operator = OperatorServices(
        context=context,
        auth=auth,
        reads=OperatorReads(context),
        actions=OperatorActions(context, auth),
        approvals=ApprovalDesk(context, auth, wiring, setup.unavailable),
    )
    application.include_router(router)
    install_web(application)
    application.add_middleware(RequestMetrics)

    shell_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            async with shell_lifespan(app):
                yield
        finally:
            if setup.client is not None:
                await setup.client.aclose()
            if owns_repositories:
                stores.close()

    application.router.lifespan_context = lifespan
    return application

