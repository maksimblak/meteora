"""T18: the base token's name and ticker reach the snapshot, for the theme filter (G24).

Recorded live pages (``fixtures/meteora_live``): TRUMP is ``token_x`` "OFFICIAL TRUMP" /
"TRUMP" of its USDC pool; the series sweeps carry a symbol but no name.
"""

import asyncio
import json
from typing import Any

from src.core.clock import FrozenClock
from tests.providers.test_meteora import (
    NOW,
    TRUMP_POOL,
    _encoded,
    _parsed,
    _provider,
    _snapshots,
    _sweep_api,
)

MELANIA_POOL = "9DiruRpjnAnzhn6ts5HGLouHtJrT1JGsPbXNYCrFz2ad"


def snapshots(**overrides: bytes) -> dict[str, Any]:
    async def exercise() -> dict[str, Any]:
        provider, client = _provider(_sweep_api(**overrides), FrozenClock(NOW))
        async with client:
            return await _snapshots(provider, NOW)

    return asyncio.run(exercise())


def test_the_name_and_ticker_come_from_the_token_whose_mint_is_the_base() -> None:
    found = snapshots()

    assert (found[TRUMP_POOL].base_token_name, found[TRUMP_POOL].base_token_symbol) == (
        "OFFICIAL TRUMP",
        "TRUMP",
    )
    assert found[MELANIA_POOL].base_token_symbol == "MELANIA"  # noqa: S105 - a ticker


def test_a_missing_or_non_text_name_is_unknown_and_the_pool_is_kept() -> None:
    page = _parsed("pools_page1.json")
    trump = next(item for item in page["data"] if item["address"] == TRUMP_POOL)
    del trump["token_x"]["name"]
    trump["token_x"]["symbol"] = 7
    trump["token_y"]["name"] = "a quote token's name is never the base's"

    found = snapshots(page1=_encoded(page))

    assert found[TRUMP_POOL].base_token_name is None
    assert found[TRUMP_POOL].base_token_symbol is None
    assert json.dumps(found[TRUMP_POOL].model_dump(mode="json"))  # still a valid snapshot
