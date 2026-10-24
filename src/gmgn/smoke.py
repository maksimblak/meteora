"""Manual smoke check: one token through the official GMGN OpenAPI with your key (G20).

    uv run --frozen python -m src.gmgn.smoke --mint <token address> [--plan free] [--timeout 10]

The key comes only from ``GMGN_API_KEY`` or ``GMGN_API_KEY_FILE`` - never from an argument,
so it stays out of the shell history - and is never printed; ``GMGN_API_URL`` overrides the
address. Two read-only GETs (``/v1/token/info`` and ``/v1/token/security``, weight 1 each)
over IPv4, the same ``GmgnOpenApiProvider`` the worker uses. Nothing is stored. The test
suite never runs it against the network.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx
from pydantic import SecretStr, ValidationError

from src.core.clock import Clock, SystemClock
from src.domain.models.primitives import PublicKey
from src.gmgn.client import AMOUNT_SIGNALS, SHARE_SIGNALS
from src.gmgn.openapi import GmgnOpenApiProvider, GmgnPlan, OpenApiSettings, openapi_client
from src.market.models import EnhancedTokenSignals


@dataclass(frozen=True, slots=True)
class SmokeResult:
    mint: str
    base_url: str
    plan: GmgnPlan
    reading: EnhancedTokenSignals
    provider_state: str

    @property
    def ok(self) -> bool:
        return self.reading.detail is None and not self.reading.missing_signals


async def run_smoke(
    client: httpx.AsyncClient,
    *,
    api_key: SecretStr,
    mint: PublicKey,
    clock: Clock | None = None,
    plan: GmgnPlan = GmgnPlan.FREE,
) -> SmokeResult:
    provider = GmgnOpenApiProvider(client, api_key=api_key, clock=clock or SystemClock(), plan=plan)
    reading = await provider.get_token_signals(mint)
    return SmokeResult(
        mint=mint.root,
        base_url=str(client.base_url).rstrip("/"),
        plan=plan,
        reading=reading,
        provider_state=provider.health.state.value,
    )


def format_result(result: SmokeResult) -> str:
    reading = result.reading
    status = reading.freshness.value if reading.detail is None else f"UNAVAILABLE:{reading.detail}"
    lines = [
        f"GMGN OpenAPI: token info + security для {result.mint} (только чтение, IPv4)",
        f"адрес: {result.base_url}, тариф: {result.plan.value}",
        f"статус: {status}; время данных: {reading.source_at.isoformat()}",
    ]
    for name in (*SHARE_SIGNALS, *AMOUNT_SIGNALS):
        value = getattr(reading, name)
        lines.append(f"  {name}: {'нет данных' if value is None else value}")
    lines.append(f"состояние провайдера: {result.provider_state}")
    return "\n".join(lines)


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.gmgn.smoke",
        description="Метрики GMGN одного токена через официальное GMGN OpenAPI (ключ из env).",
    )
    parser.add_argument("--mint", required=True, help="адрес токена в Solana")
    parser.add_argument("--plan", choices=[item.value for item in GmgnPlan], default=None)
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser.parse_args(argv)


async def _main(arguments: argparse.Namespace, environ: Mapping[str, str]) -> int:
    try:
        settings = OpenApiSettings.from_environment(environ)
        mint = PublicKey(arguments.mint)
    except (OSError, ValueError, ValidationError) as error:
        message = error.errors()[0]["msg"] if isinstance(error, ValidationError) else str(error)
        print(f"ошибка настройки: {message}", file=sys.stderr)
        return 2
    if settings is None:
        print(
            "GMGN_API_KEY (или GMGN_API_KEY_FILE) не задан: ключ создаётся на gmgn.ai/ai",
            file=sys.stderr,
        )
        return 2
    plan = GmgnPlan(arguments.plan) if arguments.plan else settings.plan
    async with openapi_client(settings, timeout=arguments.timeout) as client:
        result = await run_smoke(client, api_key=settings.api_key, mint=mint, plan=plan)
    print(format_result(result))
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    return asyncio.run(_main(_arguments(argv), os.environ if environ is None else environ))


if __name__ == "__main__":
    raise SystemExit(main())
