"""Meteora DLMM Data API adapter (``dlmm.datapi.meteora.ag``) with strict normalization.

The live API (format recorded 2026-10-23, spec 6.1 D04) reports every number as a JSON
float, ``created_at`` as epoch milliseconds, volume and fees for 30m/1h/2h/4h/12h/24h (no
5m) and paginates by page number. It states no as-of time and serves only the present, so
the arrival of a page is the ``source_at`` of every pool on it. Floats become ``Decimal``
through their shortest round-trip string: no binary float reaches the domain.

A sweep (``list_pools(None)`` and the cursors it returns) keeps each listed pool until
``get_pool_snapshot`` turns it into a snapshot, so the universe costs one request per page,
not per pool. One sweep at a time per provider instance.

Pages are a moving window over a live index: a tie reorder or a pool indexed mid-sweep
shifts later pages, so one pool arrives twice and another never. A sweep that saw a repeat,
or fewer distinct pools than the reported ``total``, re-reads the pages around each repeat
and keeps what it had missed; if it is still short, it fails instead of publishing.
"""

import asyncio
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from src.core.clock import Clock, SystemClock
from src.domain.models.primitives import PublicKey
from src.market.models import (
    DataQuality,
    Freshness,
    PoolPage,
    PoolRef,
    PoolSnapshot,
    ProviderHealth,
    ProviderState,
    VolumeBucket,
    WindowMetrics,
)
from src.providers.contracts import (
    JsonClient,
    LbPairState,
    PoolStateReader,
    ProviderError,
    ProviderPayloadError,
)
from src.providers.resilience import IntervalRateLimiter

DATA_API_URL = "https://dlmm.datapi.meteora.ag"
MAX_PAGE_SIZE = 1_000
DEFAULT_REQUESTS_PER_SECOND = 2.0
DEFAULT_PAGE_CONCURRENCY = 4
# The default universe narrowing (D07): pools that traded in the last 24 hours, ~2.1k of
# ~139k. ``filter_by=None`` sweeps everything.
DEFAULT_UNIVERSE_FILTER = "volume_24h>0"
HISTORY_TIMEFRAME = "5m"
HISTORY_BUCKET = timedelta(minutes=5)
# A bucket is final only this long after it closes: the open one is served as 0.0 (live,
# 2026-10-23: a closed bucket kept its value from about a minute after closing on).
DEFAULT_HISTORY_SETTLE = timedelta(minutes=2)
MAX_CACHED_HISTORIES = 1_024
# Oldest first: pools created during a sweep land on the last page and shift nothing.
SORT_ORDER = "pool_created_at:asc"
# Shortest first. The API's 2h and 12h windows have no domain window and are not kept.
SOURCE_WINDOWS = ("30m", "1h", "4h", "24h")
PROVIDER_NAME = "meteora_data_api"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SweepReport:
    """What the current (or last) universe sweep saw; rejected pools never become snapshots."""

    pages: int
    reported_total: int | None
    listed: int
    accepted: int
    rejected: Mapping[str, int]
    anchored: int
    complete: bool
    duplicates: int = 0
    recovered: int = 0


@dataclass(frozen=True, slots=True)
class _PoolQuote:
    pool: PoolRef
    source_at: datetime
    created_at: datetime
    tvl: Decimal
    price: Decimal
    bin_step: int
    windows: Mapping[str, WindowMetrics]
    missing: tuple[str, ...]
    base_fee_rate: Decimal | None
    market_cap: Decimal | None
    base_decimals: int | None
    quote_decimals: int | None
    quote_price_usd: Decimal | None = None
    active_bin_id: int | None = None
    cumulative_volume: Decimal | None = None
    cumulative_fees: Decimal | None = None
    blacklisted: bool | None = None
    base_verified: bool | None = None
    quote_verified: bool | None = None
    base_freeze_disabled: bool | None = None
    base_name: str | None = None
    base_symbol: str | None = None


@dataclass(frozen=True, slots=True)
class _RawPage:
    payload: Any
    arrived_at: datetime


@dataclass(frozen=True, slots=True)
class _PageMeta:
    items: Sequence[Any]
    pages: int
    total: int


@dataclass(slots=True)
class _Sweep:
    pages: int = 0
    reported_total: int | None = None
    listed: int = 0
    accepted: int = 0
    anchored: int = 0
    complete: bool = False
    duplicates: int = 0
    recovered: int = 0
    unidentified: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    seen: set[str] = field(default_factory=set)
    shifted_pages: set[int] = field(default_factory=set)

    @property
    def distinct(self) -> int:
        return len(self.seen) + self.unidentified


@dataclass(frozen=True, slots=True)
class _History:
    """Closed buckets of one pool, final up to ``settled_until``, from ``first`` on."""

    first: datetime
    settled_until: datetime
    buckets: tuple[VolumeBucket, ...]


class _Rejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MeteoraDataApiProvider:
    """Expose the live Data API through the stable market-data contract."""

    def __init__(
        self,
        client: JsonClient,
        *,
        clock: Clock | None = None,
        stale_after: timedelta = timedelta(minutes=2),
        pools_path: str = "/pools",
        page_size: int = MAX_PAGE_SIZE,
        filter_by: str | None = None,
        rate_limiter: IntervalRateLimiter | None = None,
        page_concurrency: int = DEFAULT_PAGE_CONCURRENCY,
        pool_states: PoolStateReader | None = None,
        maximum_rejected_share: Decimal = Decimal("0.5"),
        history_settle: timedelta = DEFAULT_HISTORY_SETTLE,
        cached_histories: int = MAX_CACHED_HISTORIES,
    ) -> None:
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if page_concurrency < 1:
            raise ValueError("page_concurrency must be at least one")
        if not Decimal(0) <= maximum_rejected_share < Decimal(1):
            raise ValueError("maximum_rejected_share must be in [0, 1)")
        if history_settle < timedelta(0) or cached_histories < 1:
            raise ValueError("history settle delay and cache size must be valid")
        self._history_settle = history_settle
        self._cached_histories = cached_histories
        self._histories: OrderedDict[str, _History] = OrderedDict()
        self._client = client
        self._clock = clock or SystemClock()
        self._stale_after = stale_after
        self._pools_path = pools_path.rstrip("/")
        self._page_size = page_size
        self._filter_by = filter_by
        self._limiter = rate_limiter or IntervalRateLimiter(DEFAULT_REQUESTS_PER_SECOND)
        self._page_concurrency = page_concurrency
        self._pool_states = pool_states
        self._maximum_rejected_share = maximum_rejected_share
        self._sweep = _Sweep()
        self._quotes: dict[str, _PoolQuote] = {}
        self._prefetch: dict[int, asyncio.Task[_RawPage]] = {}
        self._known_pages: int | None = None
        self._payload_failures = 0
        self._last_payload_error: str | None = None

    @property
    def health(self) -> ProviderHealth:
        """Transport health, degraded further by pages that could not be normalized."""

        transport = self._client.health
        if transport.state is not ProviderState.HEALTHY or not self._payload_failures:
            return transport
        return ProviderHealth(
            ProviderState.DEGRADED, self._payload_failures, self._last_payload_error, None
        )

    @property
    def sweep(self) -> SweepReport:
        state = self._sweep
        return SweepReport(
            pages=state.pages,
            reported_total=state.reported_total,
            listed=state.listed,
            accepted=state.accepted,
            rejected=MappingProxyType(dict(state.rejected)),
            anchored=state.anchored,
            complete=state.complete,
            duplicates=state.duplicates,
            recovered=state.recovered,
        )

    async def list_pools(self, cursor: str | None) -> PoolPage:
        page = 1 if cursor is None else _page_number(cursor)
        if cursor is None:
            self._start_sweep()
        try:
            raw = await self._page(page)
            meta = _page_meta(raw.payload, page)
        except ProviderPayloadError as error:
            self._cancel_prefetch()
            self._degrade("broken_page", error)
            raise
        except BaseException:
            self._cancel_prefetch()
            raise
        self._known_pages = meta.pages
        self._schedule_ahead(page)
        state = self._sweep
        state.pages += 1
        state.reported_total = meta.total
        quotes = await self._anchor(self._accept(meta.items, raw.arrived_at, page))
        next_cursor = str(page + 1) if page < meta.pages else None
        if next_cursor is None:
            quotes.extend(await self._repair())
        for quote in quotes:
            self._quotes[quote.pool.address.root] = quote
        state.accepted += len(quotes)
        state.anchored += sum(1 for quote in quotes if quote.active_bin_id is not None)
        if next_cursor is None:
            self._finish_sweep()
        return PoolPage(items=tuple(quote.pool for quote in quotes), next_cursor=next_cursor)

    async def get_pool_snapshot(self, pool: PoolRef, at: datetime) -> PoolSnapshot:
        observed_at = _aware_datetime(at, field="observed_at")
        quote = self._quotes.pop(pool.address.root, None)
        if quote is None or quote.pool != pool:
            try:
                quote = await self._fetch_quote(pool)
            except ProviderPayloadError as error:
                self._degrade("pool_payload", error)
                raise
            # The API serves only the present: an observation never predates its source.
            observed_at = max(observed_at, quote.source_at)
        elif quote.source_at > observed_at:
            raise ProviderPayloadError("listed page arrived after the observation instant")
        try:
            return self._snapshot(quote, observed_at)
        except ValidationError as error:
            raise ProviderPayloadError("Meteora returned an invalid snapshot") from error

    async def current_snapshot(self, pool: PoolRef) -> PoolSnapshot:
        """``pool`` as ``/pools/{address}`` serves it now, observed on arrival (spec 6.8).

        The open positions' frequent read (every 30 s by default): one request under the
        same rate limit as the sweep, without the LB pair read (a position needs no active
        bin), and outside the sweep - the pools a sweep in progress listed stay as they are.
        The short window is the caller's to derive from its previous snapshot (F1).
        """

        try:
            quote = await self._fetch_quote(pool, anchor=False)
        except ProviderPayloadError as error:
            self._degrade("pool_payload", error)
            raise
        try:
            return self._snapshot(quote, quote.source_at)
        except ValidationError as error:
            raise ProviderPayloadError("Meteora returned an invalid snapshot") from error

    async def volume_history(
        self, pool: PoolRef, *, start: datetime, end: datetime
    ) -> tuple[VolumeBucket, ...]:
        """Final 5m buckets of ``pool`` that start at or after ``start`` and end by ``end``.

        ``/pools/{address}/volume/history`` warms up a new candidate's baseline (D07). The
        open bucket (served as 0.0) and buckets closed less than ``history_settle`` ago are
        never returned. One request per pool per new bucket at most: a range already
        covered is served from a bounded cache; requests share the sweep's rate limit.
        """

        start = _aware_datetime(start, field="start")
        end = _aware_datetime(end, field="end")
        horizon = min(_bucket_floor(end), _bucket_floor(self._clock.now() - self._history_settle))
        if horizon <= start:
            return ()
        address = pool.address.root
        cached = self._histories.get(address)
        if cached is None or start < cached.first or horizon > cached.settled_until:
            try:
                buckets = await self._fetch_history(pool, start, end)
            except ProviderPayloadError as error:
                self._degrade("history_payload", error)
                raise
            cached = _History(first=start, settled_until=horizon, buckets=buckets)
            self._histories[address] = cached
            while len(self._histories) > self._cached_histories:
                self._histories.popitem(last=False)
        self._histories.move_to_end(address)
        return tuple(
            bucket for bucket in cached.buckets if bucket.start >= start and bucket.end <= horizon
        )

    async def _fetch_history(
        self, pool: PoolRef, start: datetime, end: datetime
    ) -> tuple[VolumeBucket, ...]:
        payload = await self._get(
            f"{self._pools_path}/{pool.address.root}/volume/history",
            {
                "timeframe": HISTORY_TIMEFRAME,
                "start_time": str(int((start - _EPOCH).total_seconds())),
                "end_time": str(int((end - _EPOCH).total_seconds())),
            },
        )
        data = _mapping(payload, "volume history").get("data")
        if not isinstance(data, list):
            raise ProviderPayloadError("Meteora volume history must contain a data list")
        buckets: list[VolumeBucket] = []
        for item in data:
            buckets.append(_bucket(pool, item))
        return tuple(sorted(buckets, key=lambda bucket: bucket.start))

    # --- sweep ---------------------------------------------------------------------------

    def _start_sweep(self) -> None:
        self._cancel_prefetch()
        self._sweep = _Sweep()
        self._quotes.clear()
        self._known_pages = None

    async def _repair(self) -> list[_PoolQuote]:
        """Re-read the pages around each repeat and keep the pools the shift had hidden."""

        state = self._sweep
        if not state.shifted_pages:
            return []
        last = self._known_pages or 0
        pages = sorted(
            {near for page in state.shifted_pages for near in (page - 1, page) if 1 <= near <= last}
        )
        recovered: list[_PoolQuote] = []
        for page in pages:
            try:
                raw = await self._fetch_page(page)
                meta = _page_meta(raw.payload, page)
            except ProviderPayloadError as error:
                self._degrade("broken_page", error)
                raise
            state.reported_total = meta.total
            accepted = self._accept(meta.items, raw.arrived_at, page, rereading=True)
            recovered.extend(await self._anchor(accepted))
        return recovered

    def _finish_sweep(self) -> None:
        state = self._sweep
        total = state.reported_total or 0
        if state.distinct < total:
            error = ProviderPayloadError(
                f"Meteora sweep missed {total - state.distinct} of {total} pools after "
                f"re-reading the shifted pages {sorted(state.shifted_pages)}"
            )
            self._degrade("page_shift", error)
            raise error
        rejected = sum(state.rejected.values())
        if state.listed and Decimal(rejected) / Decimal(state.listed) > (
            self._maximum_rejected_share
        ):
            error = ProviderPayloadError(
                f"Meteora sweep rejected {rejected} of {state.listed} pools"
            )
            self._degrade("rejected_share", error)
            raise error
        state.complete = True
        self._cancel_prefetch()
        self._payload_failures = 0
        self._last_payload_error = None

    def _accept(
        self, items: Sequence[Any], arrived_at: datetime, page: int, *, rereading: bool = False
    ) -> list[_PoolQuote]:
        """New distinct pools of a page; a repeat marks the page as shifted, not rejected."""

        state = self._sweep
        accepted: list[_PoolQuote] = []
        for item in items:
            address = _address_of(item)
            if address is not None and address in state.seen:
                if not rereading:
                    state.duplicates += 1
                    state.shifted_pages.add(page)
                continue
            if address is None:
                if rereading:
                    continue  # cannot tell whether the first read counted it
                state.unidentified += 1
            else:
                state.seen.add(address)
            state.listed += 1
            if rereading:
                state.recovered += 1
            try:
                accepted.append(_quote(item, arrived_at))
            except _Rejected as rejection:
                state.rejected[rejection.reason] += 1
        return accepted

    async def _anchor(self, quotes: list[_PoolQuote]) -> list[_PoolQuote]:
        """Active bin from the LB pair account; decimals from mints when the API lacks them."""

        reader = self._pool_states
        if reader is None or not quotes:
            return quotes
        try:
            states = await reader.get_pool_states([quote.pool for quote in quotes])
        except ProviderError:
            states = {}
        missing = sorted(
            {
                mint.root
                for quote in quotes
                for mint, decimals in (
                    (quote.pool.base_mint, quote.base_decimals),
                    (quote.pool.quote_mint, quote.quote_decimals),
                )
                if decimals is None
            }
        )
        decimals: Mapping[str, int] = {}
        if missing:
            try:
                decimals = await reader.get_mint_decimals([PublicKey(mint) for mint in missing])
            except ProviderError:
                decimals = {}
        return [_anchored(quote, states.get(quote.pool.address.root), decimals) for quote in quotes]

    # --- requests ------------------------------------------------------------------------

    async def _page(self, page: int) -> _RawPage:
        task = self._prefetch.pop(page, None)
        if task is not None:
            return await task
        return await self._fetch_page(page)

    def _schedule_ahead(self, page: int) -> None:
        if self._known_pages is None:
            return
        last = min(self._known_pages, page + self._page_concurrency - 1)
        for ahead in range(page + 1, last + 1):
            if ahead not in self._prefetch:
                task = asyncio.create_task(self._fetch_page(ahead))
                task.add_done_callback(_retrieve)
                self._prefetch[ahead] = task

    def _cancel_prefetch(self) -> None:
        for task in self._prefetch.values():
            task.cancel()
        self._prefetch.clear()

    async def _fetch_page(self, page: int) -> _RawPage:
        params = {"page": str(page), "page_size": str(self._page_size), "sort_by": SORT_ORDER}
        if self._filter_by is not None:
            params["filter_by"] = self._filter_by
        payload = await self._get(self._pools_path, params)
        return _RawPage(payload, self._clock.now())

    async def _fetch_quote(self, pool: PoolRef, *, anchor: bool = True) -> _PoolQuote:
        payload = await self._get(f"{self._pools_path}/{pool.address.root}", None)
        arrived_at = self._clock.now()
        try:
            quote = _quote(payload, arrived_at)
        except _Rejected as rejection:
            raise ProviderPayloadError(f"Meteora pool rejected: {rejection.reason}") from None
        if quote.pool != pool:
            raise ProviderPayloadError("Meteora pool identity differs from the requested pool")
        if not anchor:
            return quote
        (anchored,) = await self._anchor([quote])
        return anchored

    async def _get(self, path: str, params: dict[str, str] | None) -> Any:
        await self._limiter.acquire()
        return await self._client.get_json(path, params=params)

    def _degrade(self, category: str, error: Exception) -> None:
        self._payload_failures += 1
        self._last_payload_error = f"{category}: {error}"

    # --- snapshot ------------------------------------------------------------------------

    def _snapshot(self, quote: _PoolQuote, observed_at: datetime) -> PoolSnapshot:
        stale = observed_at - quote.source_at > self._stale_after
        short_window = next((name for name in SOURCE_WINDOWS if name in quote.windows), None)
        return PoolSnapshot(
            pool=quote.pool,
            windows=quote.windows,
            tvl=quote.tvl,
            depth=None,
            price=quote.price,
            bin_step=quote.bin_step,
            created_at=quote.created_at,
            observed_at=observed_at,
            source_at=quote.source_at,
            ingested_at=self._clock.now(),
            freshness=Freshness.STALE if stale else Freshness.FRESH,
            # Complete when every field this API reports is there (D07): depth and the
            # 5m window are not Data API fields, they come from quotes and counters.
            data_quality=DataQuality.PARTIAL if quote.missing else DataQuality.COMPLETE,
            missing_fields=quote.missing,
            provider=PROVIDER_NAME,
            base_fee_rate=quote.base_fee_rate,
            base_token_market_cap=quote.market_cap,
            active_bin_id=quote.active_bin_id,
            base_decimals=quote.base_decimals,
            quote_decimals=quote.quote_decimals,
            quote_price_usd=quote.quote_price_usd,
            short_window_source=short_window,
            cumulative_volume=quote.cumulative_volume,
            cumulative_fees=quote.cumulative_fees,
            pool_blacklisted=quote.blacklisted,
            base_token_verified=quote.base_verified,
            quote_token_verified=quote.quote_verified,
            base_freeze_authority_disabled=quote.base_freeze_disabled,
            base_token_name=quote.base_name,
            base_token_symbol=quote.base_symbol,
        )


def _retrieve(task: "asyncio.Task[_RawPage]") -> None:
    """Mark a prefetch failure as seen; the consumer re-raises it when it awaits the page."""

    if not task.cancelled():
        task.exception()


def _anchored(
    quote: _PoolQuote, state: LbPairState | None, decimals: Mapping[str, int]
) -> _PoolQuote:
    pool = quote.pool
    active_bin_id = None
    if (
        state is not None
        and state.token_x_mint == pool.base_mint.root
        and state.token_y_mint == pool.quote_mint.root
        and state.bin_step == quote.bin_step
    ):
        active_bin_id = state.active_bin_id
    base = quote.base_decimals
    quote_decimals = quote.quote_decimals
    return replace(
        quote,
        active_bin_id=active_bin_id,
        base_decimals=base if base is not None else _u8(decimals.get(pool.base_mint.root)),
        quote_decimals=(
            quote_decimals
            if quote_decimals is not None
            else _u8(decimals.get(pool.quote_mint.root))
        ),
    )


def _u8(value: int | None) -> int | None:
    return value if value is not None and 0 <= value <= 255 else None


# --- page and pool normalization ---------------------------------------------------------


def _address_of(item: Any) -> str | None:
    value = item.get("address") if isinstance(item, Mapping) else None
    if not isinstance(value, str):
        return None
    try:
        return PublicKey(value).root
    except ValueError:
        return None


def _page_number(cursor: str) -> int:
    if not cursor.isdigit() or int(cursor) < 2:
        raise ProviderPayloadError("Meteora cursor must be a page number above one")
    return int(cursor)


def _page_meta(payload: Any, page: int) -> _PageMeta:
    data = _mapping(payload, "page")
    items = data.get("data")
    if not isinstance(items, list):
        raise ProviderPayloadError("Meteora page must contain a data list")
    current = _page_count(data, "current_page")
    pages = _page_count(data, "pages")
    total = _page_count(data, "total")
    size = _page_count(data, "page_size")
    if current != page:
        raise ProviderPayloadError(f"Meteora served page {current} for page {page}")
    if size < 1 or pages != -(-total // size):
        raise ProviderPayloadError("Meteora page count disagrees with the total")
    expected = size if page < pages else max(0, total - (pages - 1) * size)
    if page > pages:
        expected = 0
    if len(items) != expected:
        raise ProviderPayloadError(
            f"Meteora page {page} is partial: {len(items)} of {expected} pools"
        )
    return _PageMeta(items=items, pages=pages, total=total)


def _page_count(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderPayloadError(f"Meteora page {name} must be a non-negative integer")
    return value


def _quote(value: Any, arrived_at: datetime) -> _PoolQuote:
    """One pool of the live format, or ``_Rejected`` with the reason it cannot be used."""

    if not isinstance(value, Mapping):
        raise _Rejected("malformed")
    token_x = _section(value, "token_x")
    token_y = _section(value, "token_y")
    pool = PoolRef(
        address=_key(value, "address", "address"),
        base_mint=_key(token_x, "address", "token_x.address"),
        quote_mint=_key(token_y, "address", "token_y.address"),
    )
    created_ms = _non_negative_int(value.get("created_at"), "created_at")
    if created_ms == 0:
        raise _Rejected("created_at_unknown")
    try:
        created_at = _EPOCH + timedelta(milliseconds=created_ms)
    except OverflowError:
        raise _Rejected("invalid:created_at") from None
    config = _section(value, "pool_config")
    bin_step = _non_negative_int(config.get("bin_step"), "pool_config.bin_step")
    if bin_step == 0:
        raise _Rejected("invalid:pool_config.bin_step")
    windows, missing = _windows(value)
    cumulative_volume, cumulative_fees = _cumulative(value.get("cumulative_metrics"))
    if cumulative_volume is None or cumulative_fees is None:
        missing.append("cumulative_metrics")
    tokens = (token_x, token_y)
    return _PoolQuote(
        pool=pool,
        source_at=arrived_at,
        created_at=created_at,
        tvl=_amount(value.get("tvl"), "tvl"),
        price=_amount(value.get("current_price"), "current_price"),
        bin_step=bin_step,
        windows=windows,
        missing=tuple(sorted(set(missing))),
        base_fee_rate=_base_fee_rate(config),
        market_cap=_token_value(tokens, pool.base_mint, "market_cap", _optional_amount),
        base_decimals=_token_value(tokens, pool.base_mint, "decimals", _optional_decimals),
        quote_decimals=_token_value(tokens, pool.quote_mint, "decimals", _optional_decimals),
        quote_price_usd=_token_value(tokens, pool.quote_mint, "price", _optional_price),
        cumulative_volume=cumulative_volume,
        cumulative_fees=cumulative_fees,
        blacklisted=_flag(value.get("is_blacklisted")),
        base_verified=_token_value(tokens, pool.base_mint, "is_verified", _optional_flag),
        quote_verified=_token_value(tokens, pool.quote_mint, "is_verified", _optional_flag),
        base_freeze_disabled=_token_value(
            tokens, pool.base_mint, "freeze_authority_disabled", _optional_flag
        ),
        base_name=_token_value(tokens, pool.base_mint, "name", _optional_text),
        base_symbol=_token_value(tokens, pool.base_mint, "symbol", _optional_text),
    )


def _windows(payload: Mapping[str, Any]) -> tuple[dict[str, WindowMetrics], list[str]]:
    """Long windows with volume, LP fees and protocol fees; a gap is a missing field."""

    volume = _section(payload, "volume")
    fees = _section(payload, "fees")
    protocol = payload.get("protocol_fees")
    protocol_fees: Mapping[str, Any] = protocol if isinstance(protocol, Mapping) else {}
    windows: dict[str, WindowMetrics] = {}
    missing: list[str] = []
    for name in SOURCE_WINDOWS:
        if volume.get(name) is None:
            missing.append(f"windows.{name}")
            continue
        fee = _optional_amount(fees.get(name), f"fees.{name}")
        if fee is None:
            missing.append(f"windows.{name}.fees")
        protocol_fee = _lenient_amount(protocol_fees.get(name))
        if protocol_fee is None:
            missing.append(f"windows.{name}.protocol_fees")
        windows[name] = WindowMetrics(
            volume=_amount(volume[name], f"volume.{name}"), fees=fee, protocol_fees=protocol_fee
        )
    return windows, missing


def _cumulative(value: Any) -> tuple[Decimal | None, Decimal | None]:
    """Lifetime counters; an unusable counter is a missing field, not a rejected pool."""

    if not isinstance(value, Mapping):
        return None, None
    return _lenient_amount(value.get("volume")), _lenient_amount(value.get("fees"))


def _lenient_amount(value: Any) -> Decimal | None:
    try:
        return _optional_amount(value, "lenient")
    except _Rejected:
        return None


def _optional_price(value: Any, label: str) -> Decimal | None:
    """A token's USD price; zero, absent or unusable is unknown and never rejects the pool."""

    del label
    price = _lenient_amount(value)
    return price if price is not None and price > 0 else None


def _flag(value: Any) -> bool | None:
    """A JSON boolean; anything else is unknown, never ``True``."""

    return value if isinstance(value, bool) else None


def _optional_flag(value: Any, label: str) -> bool | None:
    del label
    return _flag(value)


def _optional_text(value: Any, label: str) -> str | None:
    """A token's name or ticker; anything but non-empty text is unknown, never a reject."""

    del label
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _base_fee_rate(config: Mapping[str, Any]) -> Decimal | None:
    """Base fee as a fraction; the API reports it in percent (``0.04`` means 0.04%)."""

    percent = _optional_amount(config.get("base_fee_pct"), "pool_config.base_fee_pct")
    if percent is None:
        return None
    if percent > Decimal(100):
        raise _Rejected("invalid:pool_config.base_fee_pct")
    return percent / Decimal(100)


def _token_value[ValueT](
    tokens: tuple[Mapping[str, Any], Mapping[str, Any]],
    mint: PublicKey,
    name: str,
    parse: Callable[[Any, str], ValueT | None],
) -> ValueT | None:
    """A token figure taken only from the side whose address is ``mint``, never by position."""

    for side, token in zip(("token_x", "token_y"), tokens, strict=True):
        if token.get("address") == mint.root:
            return parse(token.get(name), f"{side}.{name}")
    return None


def _section(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = payload.get(name)
    if not isinstance(value, Mapping):
        raise _Rejected(f"missing:{name}")
    return value


def _key(payload: Mapping[str, Any], name: str, label: str) -> PublicKey:
    value = payload.get(name)
    if not isinstance(value, str):
        raise _Rejected(f"missing:{label}")
    try:
        return PublicKey(value)
    except ValueError:
        raise _Rejected(f"invalid:{label}") from None


def _amount(value: Any, label: str) -> Decimal:
    parsed = _optional_amount(value, label)
    if parsed is None:
        raise _Rejected(f"missing:{label}")
    return parsed


def _optional_amount(value: Any, label: str) -> Decimal | None:
    """A non-negative finite number; a float goes through its shortest round-trip string."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise _Rejected(f"invalid:{label}")
    try:
        parsed = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except InvalidOperation:
        raise _Rejected(f"invalid:{label}") from None
    if not parsed.is_finite() or parsed < 0:
        raise _Rejected(f"invalid:{label}")
    return parsed


def _optional_decimals(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise _Rejected(f"invalid:{label}")
    return int(value)


def _non_negative_int(value: Any, label: str) -> int:
    if value is None:
        raise _Rejected(f"missing:{label}")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _Rejected(f"invalid:{label}")
    return int(value)


def _bucket_floor(instant: datetime) -> datetime:
    """The latest 5m bucket boundary at or before ``instant`` (buckets are epoch-aligned)."""

    width = int(HISTORY_BUCKET.total_seconds())
    seconds = int((instant - _EPOCH).total_seconds())
    return _EPOCH + timedelta(seconds=seconds - seconds % width)


def _bucket(pool: PoolRef, value: Any) -> VolumeBucket:
    item = _mapping(value, "volume bucket")
    timestamp = item.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise ProviderPayloadError("Meteora bucket timestamp must be epoch seconds")
    try:
        volume = _amount(item.get("volume"), "volume")
        return VolumeBucket(
            pool_address=pool.address,
            start=_EPOCH + timedelta(seconds=timestamp),
            duration=HISTORY_BUCKET,
            volume=volume,
            fees=_lenient_amount(item.get("fees")),
            protocol_fees=_lenient_amount(item.get("protocol_fees")),
        )
    except (_Rejected, ValidationError, OverflowError) as error:
        raise ProviderPayloadError("Meteora returned an invalid volume bucket") from error


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderPayloadError(f"Meteora {label} must be an object")
    return value


def _aware_datetime(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderPayloadError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
