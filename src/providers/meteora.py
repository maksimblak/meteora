"""Meteora Data API adapter with strict payload normalization."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from src.core.clock import Clock, SystemClock
from src.domain.models.primitives import PublicKey
from src.market.models import (
    WINDOW_NAMES,
    DataQuality,
    Freshness,
    PoolPage,
    PoolRef,
    PoolSnapshot,
    ProviderHealth,
    WindowMetrics,
)
from src.providers.contracts import JsonClient, ProviderPayloadError


class MeteoraDataApiProvider:
    """Expose provider JSON through the stable market-data contract."""

    def __init__(
        self,
        client: JsonClient,
        *,
        clock: Clock | None = None,
        stale_after: timedelta = timedelta(minutes=2),
        pools_path: str = "/pools",
    ) -> None:
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        self._client = client
        self._clock = clock or SystemClock()
        self._stale_after = stale_after
        self._pools_path = pools_path.rstrip("/")

    @property
    def health(self) -> ProviderHealth:
        return self._client.health

    async def list_pools(self, cursor: str | None) -> PoolPage:
        params = {"cursor": cursor} if cursor is not None else None
        payload = _mapping(await self._client.get_json(self._pools_path, params=params))
        raw_items = payload.get("items", payload.get("pairs", payload.get("data")))
        if not isinstance(raw_items, list):
            raise ProviderPayloadError("Meteora pool page must contain an items list")
        items = tuple(_pool_ref(item) for item in raw_items)
        next_cursor = payload.get("next_cursor", payload.get("nextCursor"))
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise ProviderPayloadError("Meteora next cursor must be a string or null")
        return PoolPage(items=items, next_cursor=next_cursor)

    async def get_pool_snapshot(self, pool: PoolRef, at: datetime) -> PoolSnapshot:
        observed_at = _aware_datetime(at, field="observed_at")
        payload = _mapping(await self._client.get_json(f"{self._pools_path}/{pool.address.root}"))
        source_at = _timestamp(
            _required(payload, "source_at", "updated_at", "updatedAt"),
            field="source_at",
        )
        created_at = _timestamp(
            _required(payload, "created_at", "createdAt", "pool_created_at"),
            field="created_at",
        )
        try:
            windows, missing_fields = _windows(payload)
            tvl = _decimal(_required(payload, "tvl", "liquidity"), field="tvl")
            depth_raw = payload.get("depth", payload.get("active_liquidity"))
            depth = _decimal(depth_raw, field="depth") if depth_raw is not None else None
            if depth is None:
                missing_fields.append("depth")
            price = _decimal(
                _required(payload, "price", "current_price", "currentPrice"),
                field="price",
            )
            bin_step = _integer(_required(payload, "bin_step", "binStep"), field="bin_step")
            base_fee_rate = _base_fee_rate(payload)
            market_cap = _base_market_cap(payload, pool)
            freshness = (
                Freshness.STALE if observed_at - source_at > self._stale_after else Freshness.FRESH
            )
            quality = DataQuality.PARTIAL if missing_fields else DataQuality.COMPLETE
            return PoolSnapshot(
                pool=pool,
                windows=windows,
                tvl=tvl,
                depth=depth,
                price=price,
                bin_step=bin_step,
                created_at=created_at,
                observed_at=observed_at,
                source_at=source_at,
                ingested_at=self._clock.now(),
                freshness=freshness,
                data_quality=quality,
                missing_fields=tuple(sorted(missing_fields)),
                provider="meteora_data_api",
                base_fee_rate=base_fee_rate,
                base_token_market_cap=market_cap,
            )
        except ValidationError as error:
            raise ProviderPayloadError("Meteora returned an invalid snapshot") from error


def _base_fee_rate(payload: Mapping[str, Any]) -> Decimal | None:
    """Base fee as a fraction; the API reports it in percent (``0.04`` means 0.04%)."""

    config = payload.get("pool_config")
    raw = _mapping(config).get("base_fee_pct") if config is not None else None
    if raw is None:
        raw = payload.get("base_fee_pct", payload.get("base_fee_percentage"))
    if raw is None:
        return None
    percent = _decimal(raw, field="base_fee_pct")
    if not Decimal(0) <= percent <= Decimal(100):
        raise ProviderPayloadError("base_fee_pct must be a percentage in [0, 100]")
    return percent / Decimal(100)


def _base_market_cap(payload: Mapping[str, Any], pool: PoolRef) -> Decimal | None:
    """Market cap of the pool's base token, matched by mint and never guessed by side."""

    for side in ("token_x", "token_y"):
        token = payload.get(side)
        if token is None:
            continue
        details = _mapping(token)
        if details.get("address") == pool.base_mint.root and details.get("market_cap") is not None:
            return _decimal(details["market_cap"], field=f"{side}.market_cap")
    raw = payload.get("base_market_cap", payload.get("market_cap"))
    return None if raw is None else _decimal(raw, field="market_cap")


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderPayloadError("provider payload must be an object")
    return value


def _required(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    raise ProviderPayloadError(f"provider payload is missing required field {names[0]}")


def _pool_ref(value: Any) -> PoolRef:
    payload = _mapping(value)
    try:
        return PoolRef(
            address=PublicKey(_required(payload, "address", "pair_address", "pairAddress")),
            base_mint=PublicKey(_required(payload, "base_mint", "mint_x", "mintX", "token_x_mint")),
            quote_mint=PublicKey(
                _required(payload, "quote_mint", "mint_y", "mintY", "token_y_mint")
            ),
        )
    except (TypeError, ValueError) as error:
        raise ProviderPayloadError("Meteora pool identity is invalid") from error


def _windows(payload: Mapping[str, Any]) -> tuple[dict[str, WindowMetrics], list[str]]:
    raw_windows = payload.get("windows")
    windows: dict[str, WindowMetrics] = {}
    missing: list[str] = []
    if raw_windows is not None:
        raw_mapping = _mapping(raw_windows)
        for name in sorted(WINDOW_NAMES):
            raw_window = raw_mapping.get(name)
            if raw_window is None:
                missing.append(f"windows.{name}")
                continue
            metric = _mapping(raw_window)
            fee_raw = metric.get("fees")
            if fee_raw is None:
                missing.append(f"windows.{name}.fees")
            windows[name] = WindowMetrics(
                volume=_decimal(_required(metric, "volume"), field=f"windows.{name}.volume"),
                fees=(
                    _decimal(fee_raw, field=f"windows.{name}.fees") if fee_raw is not None else None
                ),
                transaction_count=_optional_integer(
                    metric.get("transaction_count"), field=f"windows.{name}.transaction_count"
                ),
                unique_participants=_optional_integer(
                    metric.get("unique_participants"),
                    field=f"windows.{name}.unique_participants",
                ),
            )
        return windows, missing

    for name in sorted(WINDOW_NAMES):
        volume_raw = payload.get(f"volume_{name}")
        if volume_raw is None:
            missing.append(f"windows.{name}")
            continue
        fee_raw = payload.get(f"fees_{name}")
        if fee_raw is None:
            missing.append(f"windows.{name}.fees")
        windows[name] = WindowMetrics(
            volume=_decimal(volume_raw, field=f"volume_{name}"),
            fees=_decimal(fee_raw, field=f"fees_{name}") if fee_raw is not None else None,
        )
    return windows, missing


def _decimal(value: Any, *, field: str) -> Decimal:
    if isinstance(value, (float, bool)):
        raise ProviderPayloadError(f"{field} must be an exact decimal, not binary float")
    if not isinstance(value, (str, int, Decimal)):
        raise ProviderPayloadError(f"{field} must be a decimal string or integer")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ProviderPayloadError(f"{field} must be a finite decimal") from error
    if not parsed.is_finite():
        raise ProviderPayloadError(f"{field} must be a finite decimal")
    return parsed


def _integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ProviderPayloadError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except ValueError as error:
        raise ProviderPayloadError(f"{field} must be an integer") from error
    if isinstance(value, str) and str(parsed) != value:
        raise ProviderPayloadError(f"{field} must be an integer")
    return parsed


def _optional_integer(value: Any, *, field: str) -> int | None:
    return None if value is None else _integer(value, field=field)


def _timestamp(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return _aware_datetime(value, field=field)
    if not isinstance(value, str):
        raise ProviderPayloadError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderPayloadError(f"{field} must be an ISO-8601 timestamp") from error
    return _aware_datetime(parsed, field=field)


def _aware_datetime(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderPayloadError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
