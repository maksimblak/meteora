"""T18 (G24) in the candidate pipeline: with GMGN configured the token's own volume decides,
so thin DLMM pools of a busy token are not thrown away by the cheap filters."""

import asyncio
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from typing import Any

from src.domain.models.primitives import PublicKey
from src.domain.screening import CandidateStatus, RejectionCode, merge_enhanced_signals
from src.market.models import EnhancedTokenSignals, Freshness, TokenRiskSnapshot
from src.providers.fixtures import FixtureOnChainProvider
from src.worker.pipeline import CandidatePipeline
from src.worker.policy import GuidePolicySettings, guide_policy
from tests.domain.guide_fixtures import NOW, POOL, guide_history, guide_token
from tests.worker.harness import Harness


class CountingOnChain(FixtureOnChainProvider):
    def __init__(self, token: TokenRiskSnapshot, clock: Any) -> None:
        super().__init__(token_risks={POOL.base_mint.root: token}, clock=clock)
        self.reads: Counter[str] = Counter()

    async def get_token_risk(self, mint: PublicKey) -> TokenRiskSnapshot:
        self.reads[mint.root] += 1
        return await super().get_token_risk(mint)


def busy_token() -> TokenRiskSnapshot:
    """GMGN over every pool: 100 000 in 5m and 1 200 000 in the hour (20 000 a minute)."""

    reading = EnhancedTokenSignals(
        volume_5m_usd=Decimal("100000"),
        volume_1h_usd=Decimal("1200000"),
        total_fees_sol=Decimal("45"),
        provider="gmgn-openapi",
        source_at=NOW - timedelta(minutes=1),
        freshness=Freshness.FRESH,
    )
    return merge_enhanced_signals(guide_token(total_fees_sol=None), reading)


def decide(*, enhanced: bool) -> tuple[Any, Any, CountingOnChain]:
    policy = guide_policy(
        GuidePolicySettings(baseline_points=6, minimum_baseline_points=6, maximum_history_points=20)
    )
    harness = Harness(policy=policy)
    # The token's one DLMM pool is thin: 10 000 in 5m (2 000/min), 120 000 in the hour.
    harness.ingest(guide_history(short="10000", hour="120000", tvl="20000"))
    onchain = CountingOnChain(busy_token(), harness.clock)
    pipeline = CandidatePipeline(
        history=harness.history,
        onchain=onchain,
        decisions=harness.decisions,
        modes=harness.modes,
        entries=harness.entries,
        portfolio=harness.portfolio,
        audit=harness.audit,
        alerts=harness.alerts,
        policy=policy,
        clock=harness.clock,
        metrics=harness.metrics,
        enhanced_signals=enhanced,
    )

    async def run() -> Any:
        harness.keeper.ensure_open()
        return await pipeline.evaluate(POOL.address.root, NOW)

    outcome = asyncio.run(run())
    return outcome, harness.decisions.decisions[outcome.decision_id].decision, onchain


def test_with_gmgn_a_thin_pool_of_a_busy_token_is_checked_and_judged_by_the_token() -> None:
    outcome, decision, onchain = decide(enhanced=True)

    assert onchain.reads == Counter({POOL.base_mint.root: 1})
    assert outcome.status is CandidateStatus.ACCEPT, decision.reasons
    found = {item.name: item.value for item in decision.evidence}
    assert found["token_volume_source"] == "GMGN"  # noqa: S105 - an evidence row
    assert found["volume_per_minute"] == "20000"


def test_without_gmgn_the_same_pool_fails_the_cheap_filters_on_its_dlmm_volume() -> None:
    outcome, decision, onchain = decide(enhanced=False)

    assert onchain.reads == Counter()
    assert outcome.status is CandidateStatus.REJECT
    assert RejectionCode.VOLUME_PER_MINUTE_BELOW_THRESHOLD in decision.reasons
    assert RejectionCode.HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD in decision.reasons
