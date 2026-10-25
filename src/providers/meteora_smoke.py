"""Manual smoke check: one read-only pass over the live Meteora DLMM universe.

    uv run --frozen python -m src.providers.meteora_smoke [--max-pages N] [--filter-by EXPR]

Only keyless public GETs to the Data API (``METEORA_DATA_API_BASE_URL`` or the public host);
with ``--rpc`` it also reads LB pair accounts through ``SOLANA_RPC_URL`` (never printed).
Nothing is stored. The test suite never runs it against the network.
"""

import argparse
import asyncio
import os
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

import httpx

from src.core.clock import Clock, SystemClock
from src.market.contracts import MarketDataProvider
from src.market.models import PoolPage, PoolRef, PoolSnapshot, ProviderHealth
from src.market.universe import InMemoryUniverseStore, UniverseSync
from src.providers.contracts import JsonClient, PoolStateReader
from src.providers.meteora import DATA_API_URL, MAX_PAGE_SIZE, MeteoraDataApiProvider
from src.providers.resilience import (
    IntervalRateLimiter,
    ResilientHttpClient,
    json_rpc_http_client,
)


@dataclass(frozen=True, slots=True)
class SmokeSummary:
    status: str
    error_code: str | None
    limited_to_pages: int | None
    pages: int
    reported_total: int | None
    listed: int
    accepted: int
    rejected: Mapping[str, int]
    duplicates: int
    recovered: int
    snapshots: int
    freshness: Mapping[str, int]
    short_window_sources: Mapping[str, int]
    with_base_fee: int
    with_market_cap: int
    with_decimals: int
    with_active_bin: int
    provider_state: str
    seconds: float


class _PageLimit:
    """Ends a sweep after ``max_pages`` so a smoke pass can stay small."""

    def __init__(self, inner: MarketDataProvider, max_pages: int) -> None:
        self._inner = inner
        self._max_pages = max_pages
        self._pages = 0

    @property
    def health(self) -> ProviderHealth:
        return self._inner.health

    async def list_pools(self, cursor: str | None) -> PoolPage:
        if cursor is None:
            self._pages = 0
        page = await self._inner.list_pools(cursor)
        self._pages += 1
        if self._pages >= self._max_pages:
            return PoolPage(items=page.items, next_cursor=None)
        return page

    async def get_pool_snapshot(self, pool: PoolRef, at: datetime) -> PoolSnapshot:
        return await self._inner.get_pool_snapshot(pool, at)


async def run_smoke(
    client: JsonClient,
    *,
    clock: Clock | None = None,
    page_size: int = MAX_PAGE_SIZE,
    max_pages: int | None = None,
    filter_by: str | None = None,
    rate_limiter: IntervalRateLimiter | None = None,
    pool_states: PoolStateReader | None = None,
) -> SmokeSummary:
    clock = clock or SystemClock()
    provider = MeteoraDataApiProvider(
        client,
        clock=clock,
        page_size=page_size,
        filter_by=filter_by,
        rate_limiter=rate_limiter,
        pool_states=pool_states,
    )
    market: MarketDataProvider = provider if max_pages is None else _PageLimit(provider, max_pages)
    store = InMemoryUniverseStore()
    started = time.monotonic()
    result = await UniverseSync(market, store, clock=clock).run(uuid4())
    seconds = time.monotonic() - started
    snapshots = store.current.snapshots if store.current is not None else ()
    sweep = provider.sweep
    return SmokeSummary(
        status=result.status.value,
        error_code=result.error_code,
        limited_to_pages=max_pages,
        pages=sweep.pages,
        reported_total=sweep.reported_total,
        listed=sweep.listed,
        accepted=sweep.accepted,
        rejected=dict(sweep.rejected),
        duplicates=sweep.duplicates,
        recovered=sweep.recovered,
        snapshots=len(snapshots),
        freshness=dict(Counter(item.freshness.value for item in snapshots)),
        short_window_sources=dict(
            Counter(item.short_window_source or "5m" for item in snapshots)
        ),
        with_base_fee=sum(1 for item in snapshots if item.base_fee_rate is not None),
        with_market_cap=sum(1 for item in snapshots if item.base_token_market_cap is not None),
        with_decimals=sum(
            1
            for item in snapshots
            if item.base_decimals is not None and item.quote_decimals is not None
        ),
        with_active_bin=sum(1 for item in snapshots if item.active_bin_id is not None),
        provider_state=provider.health.state.value,
        seconds=seconds,
    )


def format_summary(summary: SmokeSummary) -> str:
    limit = (
        ""
        if summary.limited_to_pages is None
        else f" (проход ограничен {summary.limited_to_pages} стр.)"
    )
    lines = [
        f"Meteora DLMM Data API: один проход universe, только чтение{limit}",
        f"статус: {summary.status}"
        + (f", код ошибки {summary.error_code}" if summary.error_code else ""),
        f"страниц: {summary.pages}, API сообщает пулов: {summary.reported_total}",
        f"получено: {summary.listed}, принято: {summary.accepted}, "
        f"отклонено адаптером: {sum(summary.rejected.values())}",
    ]
    lines.extend(
        f"  {reason}: {count}"
        for reason, count in sorted(summary.rejected.items(), key=lambda item: (-item[1], item[0]))
    )
    lines.extend(
        [
            f"повторов из-за сдвига страниц: {summary.duplicates}, "
            f"добрано перечитыванием: {summary.recovered}",
            f"снимков в версии: {summary.snapshots}; свежесть: {_counts(summary.freshness)}",
            f"короткое окно (источник): {_counts(summary.short_window_sources)}",
            f"есть base fee: {summary.with_base_fee}, капа base-токена: "
            f"{summary.with_market_cap}, decimals обоих токенов: {summary.with_decimals}, "
            f"active bin: {summary.with_active_bin}",
            f"состояние провайдера: {summary.provider_state}; время: {summary.seconds:.1f} с",
        ]
    )
    return "\n".join(lines)


def _counts(values: Mapping[str, int]) -> str:
    return ", ".join(f"{key} {count}" for key, count in sorted(values.items())) or "-"


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.providers.meteora_smoke",
        description="Один read-only проход universe по живому Meteora DLMM Data API.",
    )
    parser.add_argument("--page-size", type=int, default=MAX_PAGE_SIZE)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--filter-by", default=None, help="например: volume_24h>0")
    parser.add_argument("--requests-per-second", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--rpc", action="store_true", help="читать active bin через SOLANA_RPC_URL"
    )
    arguments = parser.parse_args(argv)
    if arguments.max_pages is not None and arguments.max_pages < 1:
        parser.error("--max-pages must be at least one")
    return arguments


async def _main(arguments: argparse.Namespace) -> int:
    base_url = os.environ.get("METEORA_DATA_API_BASE_URL") or DATA_API_URL
    rpc_url = os.environ.get("SOLANA_RPC_URL")
    if arguments.rpc and not rpc_url:
        print("SOLANA_RPC_URL не задан: --rpc недоступен", file=sys.stderr)
        return 2
    async with httpx.AsyncClient(base_url=base_url) as http:
        pool_states: PoolStateReader | None = None
        rpc_client: httpx.AsyncClient | None = None
        if arguments.rpc and rpc_url:
            from src.providers.solana import SolanaRpcProvider

            rpc_client = json_rpc_http_client(rpc_url)
            pool_states = SolanaRpcProvider(
                ResilientHttpClient(rpc_client, timeout=arguments.timeout)
            )
        try:
            summary = await run_smoke(
                ResilientHttpClient(http, timeout=arguments.timeout),
                page_size=arguments.page_size,
                max_pages=arguments.max_pages,
                filter_by=arguments.filter_by,
                rate_limiter=IntervalRateLimiter(arguments.requests_per_second),
                pool_states=pool_states,
            )
        finally:
            if rpc_client is not None:
                await rpc_client.aclose()
    print(format_summary(summary))
    return 0 if summary.status == "COMPLETED" else 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(_arguments(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
