"""The pipeline on a recorded series of live Data API sweeps (D07, probe of the T11 review).

``providers/fixtures/meteora_live/series`` holds ten sweeps of
``/pools?filter_by=volume_24h>=2000000&page=1&page_size=50&sort_by=pool_created_at:asc``
recorded 90 seconds apart on 2026-10-23 (15:37-15:51 UTC, 17 pools, public keyless GETs),
cut to the fields the adapter reads, and ``/pools/{address}/volume/history?timeframe=5m``
of two pools recorded right after. Every value is as the API sent it. Token risk and the
executor quote are fakes: no network.
"""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from src.core.clock import FrozenClock
from src.domain.features import SignalReadiness
from src.domain.screening import CandidateStatus, DecisionFlag, DepthSource, RejectionCode
from src.execution.contracts import QuoteRequestV1, QuoteResultV1
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    PoolSnapshot,
    RiskSignal,
    TokenRiskSnapshot,
    WindowOrigin,
)
from src.market.universe import InMemoryUniverseStore, SyncStatus, UniverseSync
from src.persistence.memory import InMemorySnapshotHistory
from src.providers.meteora import MeteoraDataApiProvider
from src.providers.resilience import IntervalRateLimiter, ResilientHttpClient, RetryPolicy
from src.worker.policy import GuidePolicySettings, PipelinePolicy, guide_policy
from tests.worker.harness import Harness

LIVE = Path(__file__).parents[1] / "providers" / "fixtures" / "meteora_live"
SERIES = LIVE / "series"
ARRIVALS: dict[str, str] = json.loads((SERIES / "arrivals.json").read_text(encoding="utf-8"))
SWEEPS = sorted(ARRIVALS)
CADENCE = timedelta(seconds=90)
SOL_USDC = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"
RELAXED = GuidePolicySettings(
    minimum_volume_per_minute=Decimal("10"),
    # G24: the hour's average is a second volume minimum; opened like the current one.
    minimum_hourly_volume_per_minute=Decimal("10"),
    overvolume_volume_per_minute=Decimal("100000000"),
    minimum_volume_to_liquidity=Decimal("0"),
    minimum_short_ratio=Decimal("0"),
    minimum_long_ratio=Decimal("0"),
    minimum_robust_z_score=Decimal("0"),
    minimum_fee_tvl=Decimal("0"),
    minimum_score=Decimal("0"),
    # T19: live pools of every bin step and price path may open a Bid-Ask here.
    bid_ask_minimum_depth=Decimal("0.0001"),
    bid_ask_local_high_fraction=Decimal("0.01"),
)


def at(sweep: str) -> datetime:
    return datetime.fromisoformat(ARRIVALS[sweep])


def recorded_prices() -> dict[str, Any]:
    """Token USD prices the Data API sent the same day (``pool_single.json`` first, then the
    pages), by mint, as sent.

    The series was cut to the fields the adapter read when it was recorded, before it read
    the quote token's price (A3); these stand in for it. HYPE has no recorded price, so a
    HYPE-quoted pool stays unpriced and fails closed.
    """

    prices: dict[str, Any] = {}
    pools = [json.loads((LIVE / "pool_single.json").read_text(encoding="utf-8"))]
    for page in ("pools_page1.json", "pools_page2.json"):
        pools.extend(json.loads((LIVE / page).read_text(encoding="utf-8"))["data"])
    for pool in pools:
        for side in ("token_x", "token_y"):
            token = pool[side]
            prices.setdefault(token["address"], token["price"])
    return prices


PRICES = recorded_prices()


def priced(body: bytes) -> bytes:
    """The sweep with each token's recorded price where the series has none."""

    sweep = json.loads(body)
    for pool in sweep["data"]:
        for side in ("token_x", "token_y"):
            token = pool[side]
            if "price" not in token and token["address"] in PRICES:
                token["price"] = PRICES[token["address"]]
    return json.dumps(sweep).encode()


class SeriesApi:
    """Serves the current recorded sweep and the recorded volume histories."""

    def __init__(self) -> None:
        self.current = SWEEPS[0]
        self.history_requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/pools":
            body = priced((SERIES / f"{self.current}.json").read_bytes())
        else:
            address = path.removeprefix("/pools/").removesuffix("/volume/history")
            self.history_requests.append(address)
            history = SERIES / f"history_{address}.json"
            if not history.exists():
                return httpx.Response(404, json={"message": "not recorded"})
            body = history.read_bytes()
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})


class FlatQuotes:
    """The executor's read-only quote: 10 bps for the reference size, for every pool."""

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.requests: list[QuoteRequestV1] = []

    async def quote(self, request: QuoteRequestV1) -> QuoteResultV1:
        self.requests.append(request)
        return QuoteResultV1(
            pool_address=request.pool_address,
            direction=request.direction,
            in_amount=request.in_amount,
            quoted_at=self.clock.now(),
            success=True,
            consumed_in_amount=request.in_amount,
            out_amount="1",
            fee_amount="1",
            price_impact_bps=10,
            active_bin_id=1,
            token_x_decimals=6,
            token_y_decimals=6,
            error=None,
        )


def rpc_token(snapshot: PoolSnapshot, observed_at: datetime) -> TokenRiskSnapshot:
    """What RPC alone knows: authorities and holders, no blacklist/verification/wash."""

    return TokenRiskSnapshot(
        mint=snapshot.pool.base_mint,
        mint_authority=AuthorityStatus.REVOKED,
        freeze_authority=AuthorityStatus.REVOKED,
        blacklisted=RiskSignal.UNKNOWN,
        verified=RiskSignal.UNKNOWN,
        holder_concentration=Decimal("0.10"),
        wash_trading=RiskSignal.UNKNOWN,
        observed_at=observed_at,
        source_at=observed_at,
        ingested_at=observed_at,
        data_quality=DataQuality.PARTIAL,
        missing_signals=("blacklisted", "verified", "wash_trading"),
    )


def provider(api: SeriesApi, clock: FrozenClock) -> tuple[MeteoraDataApiProvider, Any]:
    client = httpx.AsyncClient(
        base_url="https://dlmm.datapi.meteora.ag", transport=httpx.MockTransport(api)
    )
    resilient = ResilientHttpClient(client, retry_policy=RetryPolicy(max_attempts=1))
    return (
        MeteoraDataApiProvider(
            resilient,
            clock=clock,
            page_size=50,
            filter_by="volume_24h>=2000000",
            rate_limiter=IntervalRateLimiter(1_000_000),
        ),
        client,
    )


def sync_series(
    store: InMemoryUniverseStore, policy: PipelinePolicy, sweeps: list[str]
) -> tuple[SeriesApi, MeteoraDataApiProvider, FrozenClock]:
    clock = FrozenClock(at(sweeps[0]))
    api = SeriesApi()
    data_api, client = provider(api, clock)

    async def run() -> None:
        sync = UniverseSync(
            data_api,
            store,
            clock=clock,
            previous=InMemorySnapshotHistory(store),
            short_window=policy.short_window,
        )
        for sweep in sweeps:
            clock.advance(at(sweep) - clock.now())
            api.current = sweep
            result = await sync.run(uuid4())
            assert result.status is SyncStatus.COMPLETED and result.pool_count == 17

    asyncio.run(run())
    del client  # stays open: the warm-up reads volume history through the same provider
    return api, data_api, clock


def live_harness(
    settings: GuidePolicySettings | None, sweeps: list[str], *, warmup: bool = False
) -> tuple[Harness, FlatQuotes, SeriesApi, list[PoolSnapshot]]:
    policy = guide_policy(settings, universe_interval=CADENCE)
    store = InMemoryUniverseStore()
    api, data_api, _ = sync_series(store, policy, sweeps)
    assert store.current is not None
    universe = list(store.current.snapshots)
    clock = FrozenClock(at(sweeps[-1]))
    quotes = FlatQuotes(clock)
    harness = Harness(
        clock=clock,
        universe=store,
        policy=policy,
        token_risks={item.pool.base_mint.root: rpc_token(item, clock.now()) for item in universe},
        quotes=quotes,
        warmup=data_api if warmup else None,
    )
    return harness, quotes, api, universe


def decide_all(harness: Harness, universe: list[PoolSnapshot]) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        harness.keeper.ensure_open()
        outcomes = {}
        for item in universe:
            address = item.pool.address.root
            outcomes[address] = await harness.pipeline.evaluate(address, harness.clock.now())
        return outcomes

    outcomes = asyncio.run(run())
    return {
        address: harness.decisions.decisions[outcome.decision_id].decision
        for address, outcome in outcomes.items()
    }


def test_live_snapshots_leave_warm_up_on_baselines_of_derived_short_windows() -> None:
    harness, _, _, universe = live_harness(None, SWEEPS)

    decisions = decide_all(harness, universe)

    assert len(decisions) == 17
    incomplete = [
        item for item in decisions.values() if RejectionCode.INCOMPLETE_MARKET_DATA in item.reasons
    ]
    # Only a pool whose short windows mostly had no trade (median 0: no ratio exists) is
    # incomplete; before D07 every live pool was.
    assert len(incomplete) < len(decisions) // 4
    assert all(item.features.short_median == 0 for item in incomplete)
    for decision in decisions.values():
        features = decision.features
        assert RejectionCode.LOW_DEPTH not in decision.reasons
        assert features.readiness is SignalReadiness.READY
        assert features.baseline_points >= 6
        assert features.short_window_origin is WindowOrigin.CUMULATIVE_DELTA
        assert features.baseline_origin is WindowOrigin.CUMULATIVE_DELTA
    # A pool that traded in every interval has a ratio and a robust z-score.
    sol = decisions[SOL_USDC].features
    assert sol.short_ratio is not None and sol.robust_z_score is not None
    assert sol.short_median is not None and sol.short_median > 0
    assert {item.name: item.value for item in decisions[SOL_USDC].evidence}[
        "short_window_source"
    ].startswith("CUMULATIVE_DELTA:")


def test_on_live_data_some_pools_become_candidates_and_shadow_accepts() -> None:
    harness, quotes, _, universe = live_harness(RELAXED, SWEEPS)

    decisions = decide_all(harness, universe)

    accepted = [item for item in decisions.values() if item.status is CandidateStatus.ACCEPT]
    assert accepted, {key[:6]: [r.value for r in item.reasons] for key, item in decisions.items()}
    assert len(accepted) < len(decisions)  # the hard filters still reject live pools
    assert quotes.requests  # depth was measured for the candidates only
    assert len(quotes.requests) < len(decisions)
    quote_mints = {item.pool.address.root: item.pool.quote_mint.root for item in universe}
    for decision in accepted:
        depth = decision.depth
        assert depth is not None
        if quote_mints[decision.pool_address.root] in PRICES:
            assert depth.source is DepthSource.QUOTE
            assert depth.quote_price_usd == Decimal(
                repr(PRICES[quote_mints[decision.pool_address.root]])
            )
        else:  # the HYPE-quoted pool: no USD price, so nothing to quote or size with
            assert (depth.source, depth.detail) == (DepthSource.UNAVAILABLE, "QUOTE_PRICE_UNKNOWN")
            assert DecisionFlag.DEPTH_UNAVAILABLE in decision.flags
        # The Data API has no participant counts or total fees: flagged, not UNKNOWN.
        assert DecisionFlag.VOLUME_PARTICIPANTS_UNAVAILABLE in decision.flags
        assert DecisionFlag.TOTAL_FEES_UNAVAILABLE in decision.flags
    # USDC keeps its freeze authority (``freeze_authority_disabled: false``).
    usdc_sol = "HRYEjwdo3bZ1TpXKWKcezqiwSV2Ywuh4LxMa2PzoCnG6"
    assert RejectionCode.FREEZE_AUTHORITY_PRESENT in decisions[usdc_sol].reasons
    opened = harness.modes.open_positions()
    assert opened
    # Positions only where the quote token's price turned USD into its units.
    for position_id in opened:
        position = harness.accounting.position_for(position_id)
        assert position is not None and quote_mints[position.pool_address] in PRICES


def test_a_new_live_pool_is_decided_from_its_volume_history() -> None:
    harness, _, api, universe = live_harness(RELAXED, SWEEPS[:2], warmup=True)

    decisions = decide_all(harness, universe)

    sol = decisions[SOL_USDC]
    assert sol.features.readiness is SignalReadiness.READY
    assert sol.features.baseline_origin is WindowOrigin.VOLUME_HISTORY
    assert sol.status is not CandidateStatus.WARMING_UP
    assert api.history_requests.count(SOL_USDC) == 1
