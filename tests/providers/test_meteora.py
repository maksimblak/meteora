"""Meteora Data API adapter on recorded, unmodified responses of ``dlmm.datapi.meteora.ag``.

Fixtures in ``fixtures/meteora_live`` were recorded on 2026-10-23 with public, keyless GETs:

- ``pools_page1.json`` / ``pools_page2.json``: ``/pools?filter_by=tvl>=6000000&page=N
  &page_size=4&sort_by=pool_created_at:asc`` - a whole two-page sweep (6 pools);
- ``pools_created_at_zero.json``: ``/pools?page=1&page_size=2&sort_by=pool_created_at:asc`` -
  the oldest pools, whose ``created_at`` is ``0`` (unknown);
- ``pool_single.json``: ``/pools/5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6`` (SOL-USDC).

Expected values are copied from the recorded JSON text, not computed by the adapter. Tests
that need a broken page corrupt a parsed copy of a recorded one.
"""

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.market.models import DataQuality, Freshness, PoolRef, PoolSnapshot, ProviderState
from src.market.universe import InMemoryUniverseStore, SyncStatus, UniverseSync
from src.providers.contracts import LbPairState, ProviderPayloadError, ProviderUnavailableError
from src.providers.meteora import MeteoraDataApiProvider
from src.providers.resilience import (
    IntervalRateLimiter,
    ProviderHealth,
    ResilientHttpClient,
    RetryPolicy,
)

FIXTURES = Path(__file__).parent / "fixtures" / "meteora_live"
NOW = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
TRUMP_POOL = "9d9mb8kooFfaD3SctgZtkxQypkshx6ezhbKio89ixyy2"
TRUMP_MINT = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
BP_POOL = "6qz7THwQvcjF3HyDGLuKaLBUk6EyJKeZXZMWLAeiwfjd"
SOL_POOL = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
FILTER = "tvl>=6000000"


def _raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _parsed(name: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(_raw(name))
    return value


def _encoded(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload).encode()


class LiveApi:
    """Serves recorded bodies like the real host and records every request."""

    def __init__(
        self,
        pages: Mapping[int, bytes],
        single: Mapping[str, bytes] | None = None,
        *,
        on_request: Callable[[], None] | None = None,
    ) -> None:
        self.pages = dict(pages)
        self.single = dict(single or {})
        self.requests: list[httpx.Request] = []
        self.on_request = on_request

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request()
        if request.url.path == "/pools":
            body = self.pages[int(request.url.params["page"])]
        else:
            body = self.single[request.url.path.removeprefix("/pools/")]
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})


def _sweep_api(**overrides: bytes) -> LiveApi:
    pages = {1: _raw("pools_page1.json"), 2: _raw("pools_page2.json")}
    pages.update({int(key.removeprefix("page")): body for key, body in overrides.items()})
    return LiveApi(pages, {SOL_POOL: _raw("pool_single.json")})


class RecordedSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _limiter(sleep: RecordedSleep | None = None) -> IntervalRateLimiter:
    return IntervalRateLimiter(2, monotonic=lambda: 0.0, sleep=sleep or RecordedSleep())


def _provider(
    api: LiveApi, clock: FrozenClock, **options: Any
) -> tuple[MeteoraDataApiProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        base_url="https://dlmm.datapi.meteora.ag", transport=httpx.MockTransport(api)
    )
    resilient = ResilientHttpClient(client, retry_policy=RetryPolicy(max_attempts=1))
    settings: dict[str, Any] = {
        "page_size": 4,
        "filter_by": FILTER,
        "rate_limiter": _limiter(),
        "page_concurrency": 1,
        **options,
    }
    return MeteoraDataApiProvider(resilient, clock=clock, **settings), client


async def _snapshots(provider: MeteoraDataApiProvider, at: datetime) -> dict[str, PoolSnapshot]:
    refs: list[PoolRef] = []
    cursor: str | None = None
    while True:
        page = await provider.list_pools(cursor)
        refs.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return {ref.address.root: await provider.get_pool_snapshot(ref, at) for ref in refs}


def test_universe_is_swept_by_page_number_in_creation_order() -> None:
    async def exercise() -> None:
        api = _sweep_api()
        provider, client = _provider(api, FrozenClock(NOW))
        async with client:
            first = await provider.list_pools(None)
            second = await provider.list_pools(first.next_cursor)

        assert [ref.address.root for ref in first.items][1] == TRUMP_POOL
        assert len(first.items) == 4 and first.next_cursor == "2"
        assert [ref.address.root for ref in second.items][0] == BP_POOL
        assert len(second.items) == 2 and second.next_cursor is None
        assert [dict(request.url.params) for request in api.requests] == [
            {"page": page, "page_size": "4", "sort_by": "pool_created_at:asc", "filter_by": FILTER}
            for page in ("1", "2")
        ]
        assert provider.sweep.complete and provider.sweep.accepted == 6
        assert (provider.sweep.duplicates, provider.sweep.recovered) == (0, 0)
        assert dict(provider.sweep.rejected) == {}

    asyncio.run(exercise())


def test_live_floats_become_exact_decimals_and_epoch_milliseconds_utc() -> None:
    async def exercise() -> None:
        provider, client = _provider(_sweep_api(), FrozenClock(NOW))
        async with client:
            snapshots = await _snapshots(provider, NOW)

        trump = snapshots[TRUMP_POOL]
        assert trump.pool == PoolRef(
            address=PublicKey(TRUMP_POOL),
            base_mint=PublicKey(TRUMP_MINT),
            quote_mint=PublicKey(USDC_MINT),
        )
        assert trump.tvl == Decimal("9964016.373043053")
        assert trump.price == Decimal("1.792376435224221")
        assert trump.created_at == datetime(2025, 1, 18, 10, 38, 41, tzinfo=UTC)
        assert trump.bin_step == 50
        assert set(trump.windows) == {"30m", "1h", "4h", "24h"}
        assert trump.windows["30m"].volume == Decimal("27764.33501726491")
        assert trump.windows["30m"].fees == Decimal("25.390220060333228")
        assert trump.windows["24h"].volume == Decimal("645852.2768970712")
        assert trump.base_fee_rate == Decimal("0.001")  # base_fee_pct 0.1 (%)
        assert trump.base_token_market_cap == Decimal("503665849.2332783")
        assert (trump.base_decimals, trump.quote_decimals) == (6, 6)
        assert (snapshots[BP_POOL].base_decimals, snapshots[BP_POOL].quote_decimals) == (9, 6)
        assert all(
            isinstance(value, Decimal)
            for value in (trump.tvl, trump.price, trump.windows["1h"].volume)
        )
        assert trump.source_at == trump.observed_at == NOW
        assert trump.freshness is Freshness.FRESH
        assert trump.depth is None and trump.active_bin_id is None
        assert trump.short_window_source == "30m"
        assert trump.data_quality is DataQuality.PARTIAL
        assert trump.missing_fields == ("depth", "windows.5m")
        assert trump.provider == "meteora_data_api"

    asyncio.run(exercise())


def test_market_cap_comes_only_from_the_token_whose_mint_is_the_base() -> None:
    async def exercise() -> None:
        page = _parsed("pools_page1.json")
        trump = next(item for item in page["data"] if item["address"] == TRUMP_POOL)
        del trump["token_x"]["market_cap"]
        trump["market_cap"] = 1_000_000.0  # a top-level figure names no mint: never used
        provider, client = _provider(_sweep_api(page1=_encoded(page)), FrozenClock(NOW))
        async with client:
            snapshots = await _snapshots(provider, NOW)

        assert snapshots[TRUMP_POOL].base_token_market_cap is None
        assert snapshots[TRUMP_POOL].base_decimals == 6

    asyncio.run(exercise())


def test_invalid_pools_are_rejected_with_a_reason_and_the_page_survives() -> None:
    async def exercise() -> None:
        zero = LiveApi({1: _raw("pools_created_at_zero.json")})
        zero_provider, zero_client = _provider(zero, FrozenClock(NOW), page_size=2)
        page = _parsed("pools_page1.json")
        page["data"][0]["tvl"] = -1.0
        page["data"][2]["volume"]["1h"] = float("nan")
        broken_provider, broken_client = _provider(
            _sweep_api(page1=_encoded(page)), FrozenClock(NOW)
        )
        async with zero_client, broken_client:
            oldest = await zero_provider.list_pools(None)
            mixed = await broken_provider.list_pools(None)

        assert oldest.items == () and oldest.next_cursor == "2"
        assert dict(zero_provider.sweep.rejected) == {"created_at_unknown": 2}
        assert [ref.address.root for ref in mixed.items] == [
            page["data"][1]["address"],
            page["data"][3]["address"],
        ]
        assert dict(broken_provider.sweep.rejected) == {"invalid:tvl": 1, "invalid:volume.1h": 1}
        assert broken_provider.health.state is ProviderState.HEALTHY

    asyncio.run(exercise())


def _partial_page() -> bytes:
    page = _parsed("pools_page1.json")
    page["data"] = page["data"][:3]  # page 1 of 2 must carry page_size items
    return _encoded(page)


def _unpaged() -> bytes:
    page = _parsed("pools_page2.json")
    del page["data"]
    return _encoded(page)


@pytest.mark.parametrize(
    ("override", "page"),
    [({"page1": _partial_page()}, "1"), ({"page2": _unpaged()}, "2")],
    ids=["partial", "no-data"],
)
def test_broken_page_degrades_the_provider_and_fails_the_sync_not_the_worker(
    override: dict[str, bytes], page: str
) -> None:
    async def exercise() -> None:
        clock = FrozenClock(NOW)
        store = InMemoryUniverseStore()
        good, good_client = _provider(_sweep_api(), clock)
        broken, broken_client = _provider(_sweep_api(**override), clock)
        async with good_client, broken_client:
            completed = await UniverseSync(good, store, clock=clock).run(UUID(int=1))
            version = store.current
            clock.advance(timedelta(minutes=5))
            failed = await UniverseSync(broken, store, clock=clock).run(UUID(int=2))

        assert completed.status is SyncStatus.COMPLETED and completed.pool_count == 6
        assert failed.status is SyncStatus.FAILED
        assert failed.error_code == "PROVIDER_PAYLOAD"
        assert store.current == version
        assert broken.health.state is ProviderState.DEGRADED
        assert broken.health.last_error is not None
        assert good.health.state is ProviderState.HEALTHY
        assert not broken.sweep.complete and broken.sweep.pages == int(page) - 1

    asyncio.run(exercise())


def test_sweep_fails_when_the_adapter_rejects_most_of_the_universe() -> None:
    """A format change must not publish an empty universe over the last good one."""

    async def exercise() -> None:
        page = _parsed("pools_page1.json")
        for item in page["data"]:
            item["created_at"] = 0
        provider, client = _provider(_sweep_api(page1=_encoded(page)), FrozenClock(NOW))
        async with client:
            first = await provider.list_pools(None)
            with pytest.raises(ProviderPayloadError, match="rejected"):
                await provider.list_pools(first.next_cursor)

        assert provider.health.state is ProviderState.DEGRADED
        assert dict(provider.sweep.rejected) == {"created_at_unknown": 4}

    asyncio.run(exercise())


def test_universe_observation_follows_the_last_page_so_no_source_is_newer() -> None:
    async def exercise() -> None:
        clock = FrozenClock(NOW)
        api = _sweep_api()
        api.on_request = lambda: clock.advance(timedelta(seconds=30))
        provider, client = _provider(api, clock)
        store = InMemoryUniverseStore()
        async with client:
            result = await UniverseSync(provider, store, clock=clock).run(UUID(int=3))

        assert result.status is SyncStatus.COMPLETED and result.pool_count == 6
        assert store.current is not None
        observed = store.current.observed_at
        assert observed == NOW + timedelta(seconds=60)
        sources = {snapshot.source_at for snapshot in store.current.snapshots}
        assert sources == {NOW + timedelta(seconds=30), NOW + timedelta(seconds=60)}
        assert all(snapshot.observed_at == observed for snapshot in store.current.snapshots)

    asyncio.run(exercise())


def test_page_requests_are_spaced_by_the_rate_limit() -> None:
    async def exercise() -> None:
        sleep = RecordedSleep()
        provider, client = _provider(_sweep_api(), FrozenClock(NOW), rate_limiter=_limiter(sleep))
        async with client:
            await _snapshots(provider, NOW)

        assert sleep.delays == [0.5]  # 2 requests a second

    asyncio.run(exercise())


class SlowPages:
    """Recorded pools re-paged one per page; counts requests in flight."""

    def __init__(self) -> None:
        pools = [*_parsed("pools_page1.json")["data"], *_parsed("pools_page2.json")["data"]]
        self.pages = {
            index: {
                "total": len(pools),
                "pages": len(pools),
                "current_page": index,
                "page_size": 1,
                "data": [pool],
            }
            for index, pool in enumerate(pools, start=1)
        }
        self.active = 0
        self.max_active = 0
        self.health = ProviderHealth(ProviderState.HEALTHY, 0, None, None)

    async def get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        assert path == "/pools" and params is not None
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return json.loads(json.dumps(self.pages[int(params["page"])]))


def test_pages_are_fetched_ahead_with_bounded_concurrency() -> None:
    async def exercise() -> None:
        api = SlowPages()
        provider = MeteoraDataApiProvider(
            api,
            clock=FrozenClock(NOW),
            page_size=1,
            rate_limiter=IntervalRateLimiter(1_000_000),
            page_concurrency=3,
        )
        snapshots = await _snapshots(provider, NOW)

        assert len(snapshots) == 6
        assert 1 < api.max_active <= 3

    asyncio.run(exercise())


def test_a_single_pool_is_read_from_its_own_endpoint_and_never_predates_it() -> None:
    async def exercise() -> None:
        clock = FrozenClock(NOW)
        api = _sweep_api()
        api.on_request = lambda: clock.advance(timedelta(seconds=2))
        provider, client = _provider(api, clock)
        sol = PoolRef(
            address=PublicKey(SOL_POOL),
            base_mint=PublicKey(SOL_MINT),
            quote_mint=PublicKey(USDC_MINT),
        )
        swapped = PoolRef(address=sol.address, base_mint=sol.quote_mint, quote_mint=sol.base_mint)
        async with client:
            snapshot = await provider.get_pool_snapshot(sol, NOW)
            with pytest.raises(ProviderPayloadError, match="identity"):
                await provider.get_pool_snapshot(swapped, NOW)

        assert api.requests[0].url.path == f"/pools/{SOL_POOL}"
        assert snapshot.source_at == snapshot.observed_at == NOW + timedelta(seconds=2)
        assert snapshot.created_at == datetime(2024, 3, 30, 2, 47, 42, tzinfo=UTC)
        assert snapshot.bin_step == 4
        assert snapshot.base_fee_rate == Decimal("0.0004")  # base_fee_pct 0.04 (%)
        assert (snapshot.base_decimals, snapshot.quote_decimals) == (9, 6)

    asyncio.run(exercise())


class StubPoolStates:
    def __init__(
        self,
        states: Mapping[str, LbPairState],
        decimals: Mapping[str, int] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.states = dict(states)
        self.decimals = dict(decimals or {})
        self.fail = fail
        self.decimal_requests: list[tuple[str, ...]] = []

    async def get_pool_states(self, pools: Sequence[PoolRef]) -> Mapping[str, LbPairState]:
        if self.fail:
            raise ProviderUnavailableError("rpc down")
        return {
            ref.address.root: self.states[ref.address.root]
            for ref in pools
            if ref.address.root in self.states
        }

    async def get_mint_decimals(self, mints: Sequence[PublicKey]) -> Mapping[str, int]:
        self.decimal_requests.append(tuple(mint.root for mint in mints))
        return {mint.root: self.decimals[mint.root] for mint in mints if mint.root in self.decimals}


def test_active_bin_comes_from_the_lb_pair_only_when_it_matches_the_pool() -> None:
    async def exercise() -> None:
        page = _parsed("pools_page2.json")
        del page["data"][0]["token_x"]["decimals"]  # BP: decimals fall back to the mint
        states = StubPoolStates(
            {
                BP_POOL: LbPairState(-1381, 50, page["data"][0]["token_x"]["address"], USDC_MINT),
                page["data"][1]["address"]: LbPairState(7, 16, USDC_MINT, USDC_MINT),
            },
            {page["data"][0]["token_x"]["address"]: 9},
        )
        provider, client = _provider(
            _sweep_api(page2=_encoded(page)), FrozenClock(NOW), pool_states=states
        )
        down, down_client = _provider(
            _sweep_api(page2=_encoded(page)),
            FrozenClock(NOW),
            pool_states=StubPoolStates({}, fail=True),
        )
        async with client, down_client:
            snapshots = await _snapshots(provider, NOW)
            unanchored = await _snapshots(down, NOW)

        bp = snapshots[BP_POOL]
        assert (bp.active_bin_id, bp.base_decimals, bp.quote_decimals) == (-1381, 9, 6)
        assert snapshots[page["data"][1]["address"]].active_bin_id is None  # mints disagree
        assert snapshots[TRUMP_POOL].active_bin_id is None  # no account read
        assert states.decimal_requests == [(page["data"][0]["token_x"]["address"],)]
        assert provider.sweep.anchored == 1
        assert unanchored[BP_POOL].active_bin_id is None
        assert unanchored[BP_POOL].base_decimals is None
        assert len(unanchored) == 6

    asyncio.run(exercise())


class ShiftingApi:
    """Serves each page's recorded bodies in turn, the last one for every later request."""

    def __init__(self, pages: Mapping[int, Sequence[bytes]]) -> None:
        self.pages = {page: list(bodies) for page, bodies in pages.items()}
        self.requested: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        self.requested.append(page)
        bodies = self.pages[page]
        body = bodies.pop(0) if len(bodies) > 1 else bodies[0]
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})


def _tie_reordered_page2() -> bytes:
    """Page 2 after a tie reorder at the boundary: YZY (page 1) again, BP never served."""

    page1, page2 = _parsed("pools_page1.json"), _parsed("pools_page2.json")
    page2["data"] = [page1["data"][3], page2["data"][1]]
    return _encoded(page2)


async def _sync_once(api: ShiftingApi) -> tuple[Any, MeteoraDataApiProvider, InMemoryUniverseStore]:
    clock = FrozenClock(NOW)
    client = httpx.AsyncClient(
        base_url="https://dlmm.datapi.meteora.ag", transport=httpx.MockTransport(api)
    )
    provider = MeteoraDataApiProvider(
        ResilientHttpClient(client, retry_policy=RetryPolicy(max_attempts=1)),
        clock=clock,
        page_size=4,
        filter_by=FILTER,
        rate_limiter=_limiter(),
        page_concurrency=1,
    )
    store = InMemoryUniverseStore()
    async with client:
        result = await UniverseSync(provider, store, clock=clock).run(UUID(int=7))
    return result, provider, store


def test_a_page_shift_is_repaired_by_rereading_the_pages_around_it() -> None:
    async def exercise() -> None:
        api = ShiftingApi(
            {1: [_raw("pools_page1.json")], 2: [_tie_reordered_page2(), _raw("pools_page2.json")]}
        )

        result, provider, store = await _sync_once(api)

        assert result.status is SyncStatus.COMPLETED and result.pool_count == 6
        assert store.current is not None
        assert BP_POOL in {item.pool.address.root for item in store.current.snapshots}
        assert api.requested == [1, 2, 1, 2]  # the boundary pages are read again
        sweep = provider.sweep
        assert (sweep.complete, sweep.duplicates, sweep.recovered) == (True, 1, 1)
        assert dict(sweep.rejected) == {}
        assert provider.health.state is ProviderState.HEALTHY

    asyncio.run(exercise())


def test_a_pool_inserted_before_the_cursor_is_recovered_from_earlier_pages() -> None:
    """An older pool indexed mid-sweep pushes YZY onto page 2 and itself onto page 1."""

    async def exercise() -> None:
        sol = _parsed("pool_single.json")
        page1, page2 = _parsed("pools_page1.json"), _parsed("pools_page2.json")
        grown1 = {**page1, "total": 7, "data": [sol, *page1["data"][:3]]}
        grown2 = {**page2, "total": 7, "data": [page1["data"][3], *page2["data"]]}
        api = ShiftingApi(
            {1: [_raw("pools_page1.json"), _encoded(grown1)], 2: [_encoded(grown2)]}
        )

        result, provider, store = await _sync_once(api)

        assert result.status is SyncStatus.COMPLETED and result.pool_count == 7
        assert store.current is not None
        assert SOL_POOL in {item.pool.address.root for item in store.current.snapshots}
        assert (provider.sweep.duplicates, provider.sweep.recovered) == (1, 1)

    asyncio.run(exercise())


def test_a_shift_that_cannot_be_repaired_is_not_published_as_a_universe() -> None:
    async def exercise() -> None:
        api = ShiftingApi({1: [_raw("pools_page1.json")], 2: [_tie_reordered_page2()]})

        result, provider, store = await _sync_once(api)

        assert result.status is SyncStatus.FAILED
        assert result.error_code == "PROVIDER_PAYLOAD"
        assert store.current is None
        assert not provider.sweep.complete and provider.sweep.duplicates == 1
        assert provider.health.state is ProviderState.DEGRADED
        assert provider.health.last_error is not None
        assert "missed" in provider.health.last_error

    asyncio.run(exercise())
