"""D06 anchor from Solana RPC on recorded mainnet responses (2026-10-23, public endpoint).

- ``rpc_lb_pairs.json``: ``getMultipleAccounts`` (base64, ``dataSlice`` 0..152) for the
  arc-USDC and BP-USDC LB pairs and, as a non-pair control, the USDC mint;
- ``rpc_mints.json``: ``getMultipleAccounts`` (jsonParsed) for the BP and USDC mints and,
  as a non-mint control, the TRUMP-USDC LB pair.

The active bin ids are checked against the Data API price recorded the same minute, an
independent source: ``price = (1 + bin_step / 10_000) ** active_id * 10 ** (dx - dy)``.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.domain.models.primitives import PublicKey
from src.market.models import PoolRef, ProviderHealth, ProviderState
from src.providers.contracts import LbPairState, ProviderPayloadError
from src.providers.solana import SolanaRpcProvider

FIXTURES = Path(__file__).parent / "fixtures" / "meteora_live"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ARC_POOL, ARC_MINT = (
    "DW2QC5ychRKmA3YpY6eWetxt1YdMK8hir3vNjNjRW6i9",
    "61V8vBaqAGMpgDQi4JcAwo1dmBGHsyhzodcPqnEVpump",
)
BP_POOL, BP_MINT = (
    "6qz7THwQvcjF3HyDGLuKaLBUk6EyJKeZXZMWLAeiwfjd",
    "BPxxfRCXkUVhig4HS1Lh7kZqV6SPJhzfEk4x6fVBjPCy",
)
TRUMP_POOL = "9d9mb8kooFfaD3SctgZtkxQypkshx6ezhbKio89ixyy2"


class RecordedRpc:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.health = ProviderHealth(ProviderState.HEALTHY, 0, None, None)

    async def post_json(self, payload: dict[str, Any]) -> Any:
        self.requests.append(payload)
        return self.responses.pop(0)


def _recorded(name: str) -> Any:
    return json.loads((FIXTURES / name).read_bytes())


def _ref(address: str, base: str, quote: str = USDC) -> PoolRef:
    return PoolRef(
        address=PublicKey(address), base_mint=PublicKey(base), quote_mint=PublicKey(quote)
    )


def test_lb_pair_accounts_give_the_active_bin_that_prices_the_pool() -> None:
    async def exercise() -> None:
        rpc = RecordedRpc(_recorded("rpc_lb_pairs.json"))
        provider = SolanaRpcProvider(rpc)
        pools = [_ref(ARC_POOL, ARC_MINT), _ref(BP_POOL, BP_MINT), _ref(USDC, BP_MINT)]

        states = await provider.get_pool_states(pools)

        assert states == {
            ARC_POOL: LbPairState(-1387, 20, ARC_MINT, USDC),
            BP_POOL: LbPairState(-1381, 50, BP_MINT, USDC),
        }  # the USDC mint is owned by the token program, so it is no LB pair
        assert rpc.requests[0]["method"] == "getMultipleAccounts"
        assert rpc.requests[0]["params"] == [
            [ARC_POOL, BP_POOL, USDC],
            {
                "encoding": "base64",
                "commitment": "confirmed",
                "dataSlice": {"offset": 0, "length": 152},
            },
        ]
        # Data API current_price the same minute: arc 0.06258499706317705 (6/6 decimals),
        # BP 1.020160994144142 (9/6 decimals); one bin is 0.2% and 0.5% wide.
        arc = (1 + 20 / 10_000) ** states[ARC_POOL].active_bin_id
        bp = (1 + 50 / 10_000) ** states[BP_POOL].active_bin_id * 10**3
        assert abs(arc / 0.06258499706317705 - 1) < 0.002
        assert abs(bp / 1.020160994144142 - 1) < 0.005

    asyncio.run(exercise())


def test_pool_states_are_read_in_chunks_of_one_hundred_accounts() -> None:
    async def exercise() -> None:
        pools = [_ref(ARC_POOL, ARC_MINT)] * 150
        empty = {"jsonrpc": "2.0", "result": {"context": {"slot": 1}, "value": [None] * 100}}
        rest = {"jsonrpc": "2.0", "result": {"context": {"slot": 1}, "value": [None] * 50}}
        rpc = RecordedRpc(empty, rest)

        states = await SolanaRpcProvider(rpc).get_pool_states(pools)

        assert states == {}
        assert [len(request["params"][0]) for request in rpc.requests] == [100, 50]

    asyncio.run(exercise())


def test_a_short_account_list_is_a_payload_error_not_a_guess() -> None:
    async def exercise() -> None:
        recorded = _recorded("rpc_lb_pairs.json")
        recorded["result"]["value"] = recorded["result"]["value"][:2]
        provider = SolanaRpcProvider(RecordedRpc(recorded))

        with pytest.raises(ProviderPayloadError):
            await provider.get_pool_states(
                [_ref(ARC_POOL, ARC_MINT), _ref(BP_POOL, BP_MINT), _ref(USDC, BP_MINT)]
            )

    asyncio.run(exercise())


def test_mint_decimals_come_from_parsed_mint_accounts_only() -> None:
    async def exercise() -> None:
        rpc = RecordedRpc(_recorded("rpc_mints.json"))
        mints = [PublicKey(BP_MINT), PublicKey(USDC), PublicKey(TRUMP_POOL)]

        decimals = await SolanaRpcProvider(rpc).get_mint_decimals(mints)

        assert decimals == {BP_MINT: 9, USDC: 6}  # the LB pair account is no mint
        assert rpc.requests[0]["params"][1] == {"encoding": "jsonParsed", "commitment": "confirmed"}

    asyncio.run(exercise())
