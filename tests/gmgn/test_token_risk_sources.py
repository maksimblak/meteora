"""D09: the base on-chain risk ages on its own; no RPC is a flag in SHADOW, closed beyond.

The worker is built as in production (``build_runtime``): the real ``SolanaRpcProvider``
behind a stubbed RPC transport, or the not-configured provider when ``SOLANA_RPC_URL`` is
unset; no enhanced provider, no GMGN service. Pools carry the Data API's own flags.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.domain.models.trading import TradingMode
from src.domain.screening import (
    CandidateDecision,
    CandidateStatus,
    DecisionContext,
    DecisionFlag,
    PoolScreener,
    RejectionCode,
)
from src.market.models import Freshness, PoolSnapshot, TokenRiskSnapshot
from src.modes.models import ModeTransition
from src.worker.policy import GuidePolicySettings, guide_policy
from src.worker.runtime import build_runtime
from tests.domain.guide_fixtures import (
    CONFIG,
    NOW,
    POOL,
    RULESET,
    guide_features,
    guide_history,
    guide_token,
)

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SYSTEM_PROGRAM = "11111111111111111111111111111111"


def key(seed: int) -> str:
    number = int.from_bytes(bytes([seed]) + bytes([7]) * 31, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _BASE58[remainder] + encoded
    return encoded


def data_api_flags(history: list[PoolSnapshot]) -> list[PoolSnapshot]:
    """The pools as the Data API reports them: not blacklisted, verified, freeze disabled."""

    rebuilt = []
    for item in history:
        data = item.model_dump()
        data.update(
            pool_blacklisted=False,
            base_token_verified=True,
            quote_token_verified=True,
            base_freeze_authority_disabled=True,
        )
        rebuilt.append(PoolSnapshot.model_validate(data))
    return rebuilt


class CleanTokenRpc:
    """JSON-RPC of a clean token: no authorities, eleven wallets of 1% (top-10 = 10%)."""

    def __init__(self) -> None:
        self.wallets = {key(100 + index): key(10 + index) for index in range(11)}
        self.methods: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        self.methods.append(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, **self.answer(body)})

    def answer(self, body: dict[str, Any]) -> dict[str, Any]:
        method, params = body["method"], body["params"]
        if method == "getAccountInfo":
            info = {"mintAuthority": None, "freezeAuthority": None}
            return {"result": {"value": {"data": {"parsed": {"info": info}}}}}
        if method == "getTokenSupply":
            return {"result": {"value": {"amount": "10000000", "decimals": 0}}}
        if method == "getTokenLargestAccounts":
            accounts = [{"address": account, "amount": "100000"} for account in self.wallets]
            return {"result": {"value": accounts}}
        if method == "getMultipleAccounts":
            requested = params[0]
            if requested and requested[0] in self.wallets:
                owners = [
                    {"data": {"parsed": {"info": {"owner": self.wallets[item]}}}}
                    for item in requested
                ]
                return {"result": {"value": owners}}
            programs = [{"owner": SYSTEM_PROGRAM, "executable": False} for _ in requested]
            return {"result": {"value": programs}}
        if method == "getSignaturesForAddress":
            return {"result": []}
        raise AssertionError(f"unexpected RPC method {method}")


def decide(tmp_path: Path, *, rpc: bool, mode: TradingMode = TradingMode.SHADOW) -> Any:
    overrides = tmp_path / "rules.yaml"
    overrides.write_text(
        'config_version: "d09-test"\n'
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
    node = CleanTokenRpc()
    runtime, clients = build_runtime(
        AppConfig.load(environ=environ),
        environ,
        clock=FrozenClock(NOW),
        rpc_transport=httpx.MockTransport(node),
    )

    async def run() -> tuple[CandidateDecision, TokenRiskSnapshot, Any]:
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
            record = await runtime.repositories.decisions.decision(outcome.decision_id)
            assert record is not None
            return record.decision, record.token, outcome
        finally:
            for client in clients:
                await client.aclose()
            runtime.close()

    decision, token, outcome = asyncio.run(run())
    return decision, token, outcome, node


def test_a_clean_token_read_by_the_real_rpc_provider_is_accepted_in_shadow(
    tmp_path: Path,
) -> None:
    decision, token, outcome, node = decide(tmp_path, rpc=True)

    assert "getAccountInfo" in node.methods
    assert token.freshness is Freshness.FRESH
    assert token.holder_concentration == Decimal("0.1")
    assert RejectionCode.TOKEN_RISK_STALE not in decision.reasons
    assert decision.status is CandidateStatus.ACCEPT
    assert decision.reasons == ()
    # What only an enhanced provider or GMGN would give is flagged, not rejected.
    assert {DecisionFlag.TOTAL_FEES_UNAVAILABLE, DecisionFlag.GMGN_UNAVAILABLE} <= set(
        decision.flags
    )
    assert DecisionFlag.TOKEN_RISK_UNAVAILABLE not in decision.flags
    assert outcome.plan_id is not None


def test_without_rpc_shadow_goes_on_with_token_risk_unavailable(tmp_path: Path) -> None:
    decision, token, outcome, node = decide(tmp_path, rpc=False)

    assert node.methods == []
    assert decision.status is CandidateStatus.ACCEPT
    assert DecisionFlag.TOKEN_RISK_UNAVAILABLE in decision.flags
    assert RejectionCode.TOKEN_RISK_STALE not in decision.reasons
    assert RejectionCode.TOKEN_RISK_UNKNOWN not in decision.reasons
    assert token.source_configured is False
    assert outcome.plan_id is not None


@pytest.mark.parametrize("mode", [TradingMode.APPROVAL, TradingMode.LIVE])
def test_without_rpc_approval_and_live_are_closed(tmp_path: Path, mode: TradingMode) -> None:
    decision, _, outcome, _ = decide(tmp_path, rpc=False, mode=mode)

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.TOKEN_RISK_UNAVAILABLE in decision.reasons
    assert DecisionFlag.TOKEN_RISK_UNAVAILABLE in decision.flags
    assert outcome.plan_id is None


def test_truly_stale_token_data_still_rejects_in_shadow() -> None:
    """Data that was received but is old is still ``TOKEN_RISK_STALE``, a terminal reason."""

    history = data_api_flags(guide_history())
    stale = guide_token().model_copy(update={"freshness": Freshness.STALE})
    policy = guide_policy(
        GuidePolicySettings(
            baseline_points=6, minimum_baseline_points=6, maximum_history_points=20
        ),
        gmgn_required_for_live=True,
    )

    decision = PoolScreener().evaluate(
        history[-1],
        stale,
        guide_features(history),
        policy.screening,
        context=DecisionContext(
            mode=TradingMode.SHADOW,
            ruleset_version=RULESET,
            config_version=CONFIG,
            correlation_id="cycle-1",
        ),
        estimated_price_impact=Decimal("0.001"),
    )

    assert decision.status is CandidateStatus.REJECT
    assert RejectionCode.TOKEN_RISK_STALE in decision.reasons
