"""Dependencies shared by the operator read models and actions."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date
from typing import Any

from starlette.concurrency import run_in_threadpool

from src.api.probes import ServiceProbe
from src.api.settings import OperatorSettings
from src.core.clock import Clock
from src.core.config import AppConfig
from src.domain.models.trading import TradingMode
from src.execution.contracts import ExecutionPhase
from src.execution.service import ExecutionRecord
from src.modes.models import ModeState
from src.persistence.contracts import DecisionJournal, UniverseSummary, universe_summary
from src.persistence.factory import Repositories
from src.worker.policy import PipelinePolicy


@dataclass(frozen=True, slots=True)
class OperatorContext:
    config: AppConfig
    environ: Mapping[str, str]
    settings: OperatorSettings
    repositories: Repositories
    catalog: DecisionJournal
    policy: PipelinePolicy
    clock: Clock
    probes: tuple[ServiceProbe, ...] = ()

    async def sync[T](self, function: Callable[..., T], *args: Any) -> T:
        """T04/T07 repositories are synchronous over a bridge thread: keep the loop free."""

        return await run_in_threadpool(function, *args)

    async def mode_state(self) -> ModeState:
        return await self.sync(self.repositories.mode.mode_state)

    async def trading_mode(self) -> TradingMode:
        return (await self.mode_state()).mode

    async def universe(self) -> UniverseSummary:
        """Addresses, sync time and the D06 anchor flag of the newest universe in one light
        read; a page reads it once and hands it to every part that needs it."""

        return await universe_summary(self.repositories.history)

    async def pending_approvals(self) -> tuple[ExecutionRecord, ...]:
        """Requests the operator can still approve: an expired one waits for nobody (the
        worker renews an exit's preview), so it is neither listed nor counted."""

        now = self.clock.now()
        waiting = await self.repositories.execution.in_phases((ExecutionPhase.AWAITING_APPROVAL,))
        return tuple(item for item in waiting if item.command.expires_at > now)

    def today(self) -> date:
        return self.clock.now().astimezone(UTC).date()
