"""§7.1 data coverage (D09): a decision without any token-risk source is not covered.

The worker is built as in production; the daily evidence sees each candidate decision
with its ``covered`` verdict. Optional GMGN data missing is not a gap (spec 6.7).
"""

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.domain.models.trading import TradingMode
from src.domain.screening import DecisionFlag
from src.modes.models import ModeTransition
from src.worker.runtime import build_runtime
from tests.domain.guide_fixtures import NOW, POOL, guide_history
from tests.gmgn.test_token_risk_sources import CleanTokenRpc, data_api_flags


def coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, rpc: bool, mode: TradingMode
) -> tuple[list[tuple[bool, bool]], tuple[DecisionFlag, ...]]:
    overrides = tmp_path / "rules.yaml"
    overrides.write_text(
        'config_version: "d09-coverage"\n'
        "thresholds:\n"
        "  baseline_points: 6\n"
        "  minimum_baseline_points: 6\n"
        "  maximum_history_points: 20\n",
        encoding="utf-8",
    )
    environ = {
        "APP_ENV": "development",
        "LOCAL_SQLITE_PATH": str(tmp_path / "worker.sqlite3"),
        "RULESET_OVERRIDES_FILE": str(overrides),
        **({"SOLANA_RPC_URL": "https://rpc.example"} if rpc else {}),
    }
    runtime, clients = build_runtime(
        AppConfig.load(environ=environ),
        environ,
        clock=FrozenClock(NOW),
        rpc_transport=httpx.MockTransport(CleanTokenRpc()),
    )
    seen: list[tuple[bool, bool]] = []

    def record(at: object, *, candidate: bool, covered: bool) -> None:
        seen.append((candidate, covered))

    monkeypatch.setattr(runtime.evidence, "decision", record)

    async def run() -> tuple[DecisionFlag, ...]:
        try:
            universe = runtime.repositories.universe
            for item in data_api_flags(guide_history()):
                stage = await universe.begin(uuid4(), item.observed_at)
                await stage.stage((item,))
                await stage.commit()
            if mode is not TradingMode.SHADOW:
                state = runtime.repositories.mode.mode_state()
                runtime.repositories.mode.commit_transition(
                    ModeTransition(
                        transition_id=uuid4(),
                        from_mode=state.mode,
                        to_mode=mode,
                        occurred_at=NOW,
                        operator_confirmation_id="operator",
                        promotion_authorized=True,
                    ),
                    expected_version=state.version,
                    live_promotion_id=uuid4(),
                )
            runtime.keeper.ensure_open()
            outcome = await runtime.pipeline.evaluate(POOL.address.root, NOW)
            record_ = await runtime.repositories.decisions.decision(outcome.decision_id)
            assert record_ is not None
            return record_.decision.flags
        finally:
            for client in clients:
                await client.aclose()
            runtime.close()

    flags = asyncio.run(run())
    return seen, flags


@pytest.mark.parametrize("mode", [TradingMode.SHADOW, TradingMode.APPROVAL])
def test_a_decision_without_any_token_risk_source_is_not_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: TradingMode
) -> None:
    seen, flags = coverage(tmp_path, monkeypatch, rpc=False, mode=mode)

    assert DecisionFlag.TOKEN_RISK_UNAVAILABLE in flags
    assert seen == [(True, False)]


def test_a_clean_token_read_by_rpc_is_covered_without_the_optional_gmgn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen, flags = coverage(tmp_path, monkeypatch, rpc=True, mode=TradingMode.SHADOW)

    assert DecisionFlag.GMGN_UNAVAILABLE in flags
    assert seen == [(True, True)]
