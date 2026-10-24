"""F5 (T18 review): the token of a DLMM pair is the side that is not a quote token.

``PoolRef.base_mint``/``quote_mint`` are the pool's ``token_x``/``token_y``. A pool where the
coin stands second (SOL-MEME) is still a pool of that coin: it joins the coin's group, adds
its volume and is held with it - and, since prices, bins and quotes here are taken from
``token_x``, guide-v1 does not enter it and says why.
"""

from decimal import Decimal

import pytest

from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode
from src.domain.screening import CandidateStatus, RejectionCode
from src.domain.screening.pairs import PairShape, pair_token
from src.domain.screening.selection import token_groups
from src.market.models import PoolRef, PoolSnapshot
from tests.domain.guide_fixtures import NOW, guide_history, guide_snapshot, guide_token
from tests.domain.test_t18_selection import evidence, screen

# Mainnet mints: wrapped SOL (the SPL Token native mint), Circle's USDC, Tether's USDT.
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
MEME = "11111111111111111111111111111111"
JUP = "Stake11111111111111111111111111111111111111"
ADDRESS = "SysvarRent111111111111111111111111111111111"


def ref(x: str, y: str, address: str = ADDRESS) -> PoolRef:
    return PoolRef(address=PublicKey(address), base_mint=PublicKey(x), quote_mint=PublicKey(y))


@pytest.mark.parametrize(
    ("x", "y", "mint", "shape"),
    [
        (MEME, WSOL, MEME, PairShape.TOKEN_FIRST),
        (MEME, USDT, MEME, PairShape.TOKEN_FIRST),
        (WSOL, MEME, MEME, PairShape.TOKEN_SECOND),
        (USDC, MEME, MEME, PairShape.TOKEN_SECOND),
        (WSOL, USDC, WSOL, PairShape.QUOTE_ONLY),
        (USDC, WSOL, USDC, PairShape.QUOTE_ONLY),
        (MEME, JUP, MEME, PairShape.NO_QUOTE),
    ],
)
def test_the_token_of_a_pair_is_the_side_that_is_not_a_quote_token(
    x: str, y: str, mint: str, shape: PairShape
) -> None:
    token = pair_token(ref(x, y))

    assert (token.mint, token.shape) == (mint, shape)


def pool(x: str, y: str, address: str, *, hour: str, hour_fees: str) -> PoolSnapshot:
    return guide_snapshot(NOW, hour=hour, hour_fees=hour_fees).model_copy(
        update={"pool": ref(x, y, address)}
    )


def test_a_pool_where_the_coin_stands_second_belongs_to_the_coins_group() -> None:
    first = pool(MEME, WSOL, "SysvarC1ock11111111111111111111111111111111",
                 hour="200000", hour_fees="100")
    second = pool(WSOL, MEME, "Vote111111111111111111111111111111111111111",
                  hour="300000", hour_fees="300")

    (group,) = token_groups([first, second])

    assert group.base_mint == MEME
    # SOL-MEME took more LP fees in the hour (300 > 100), so it is tried first.
    assert [item.pool.address for item in group.pools] == [second.pool.address, first.pool.address]
    assert group.volume_1h == Decimal("500000")  # 200 000 + 300 000
    selection = group.selection_for(first.pool.address.root)
    assert (selection.sibling_pools, selection.sibling_volume_1h) == (1, Decimal("300000"))


def pair_history(x: str, y: str) -> list[PoolSnapshot]:
    return [item.model_copy(update={"pool": ref(x, y)}) for item in guide_history()]


@pytest.mark.parametrize(
    ("x", "y", "code"),
    [
        (WSOL, MEME, RejectionCode.TOKEN_SECOND_IN_PAIR),
        (WSOL, USDC, RejectionCode.PAIR_OF_QUOTE_TOKENS),
        (MEME, JUP, RejectionCode.PAIR_WITHOUT_QUOTE_TOKEN),
    ],
)
def test_guide_v1_skips_a_pool_it_cannot_price_from_the_coin_with_the_reason(
    x: str, y: str, code: RejectionCode
) -> None:
    token = guide_token().model_copy(update={"mint": PublicKey(x)})

    decision = screen(pair_history(x, y), token, mode=TradingMode.SHADOW)

    assert decision.status is CandidateStatus.REJECT
    assert code in decision.reasons
    assert evidence(decision)["pool_pair"][0] == pair_token(ref(x, y)).shape.value


def test_a_pool_with_the_coin_first_and_a_quote_token_second_is_screened_as_before() -> None:
    decision = screen(pair_history(MEME, WSOL), mode=TradingMode.SHADOW)

    assert decision.status is CandidateStatus.ACCEPT, decision.reasons
    assert evidence(decision)["pool_pair"][0] == "TOKEN_FIRST"
