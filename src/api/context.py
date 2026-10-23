"""Dependencies shared by the operator read models and actions."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date
from typing import Any

from starlette.concurrency import run_in_threadpool

from src.api.catalog import DecisionCatalog
from src.api.probes import ServiceProbe
from src.api.settings import OperatorSettings
from src.core.clock import Clock
from src.core.config import AppConfig
from src.domain.models.trading import TradingMode
from src.modes.models import ModeState
from src.persistence.factory import Repositories
from src.worker.policy import PipelinePolicy


@dataclass(frozen=True, slots=True)
class OperatorContext:
    config: AppConfig
    environ: Mapping[str, str]
    settings: OperatorSettings
    repositories: Repositories
    catalog: DecisionCatalog
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

    def today(self) -> date:
        return self.clock.now().astimezone(UTC).date()
