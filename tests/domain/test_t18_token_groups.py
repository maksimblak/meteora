"""T18 (G24): the universe by base token - the main pool first, token volume summed."""

from decimal import Decimal
from typing import Any

from src.domain.models.primitives import PublicKey
from src.domain.screening import TokenSelection
from src.domain.screening.selection import token_groups
from src.market.models import DataQuality, PoolRef, PoolSnapshot
from tests.domain.guide_fixtures import NOW, guide_snapshot

MINT_A = "So11111111111111111111111111111111111111112"
MINT_B = "11111111111111111111111111111111"
QUOTE = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def pool(address: str, base: str, *, fees_1h: str, tvl: str, hour: str, short: str | None) -> Any:
    snapshot = guide_snapshot(NOW, hour=hour, hour_fees=fees_1h, tvl=tvl, short=short or "1")
    data = snapshot.model_dump()
    data["pool"] = PoolRef(
        address=PublicKey(address), base_mint=PublicKey(base), quote_mint=PublicKey(QUOTE)
    )
    if short is None:
        del data["windows"]["5m"]
    return PoolSnapshot.model_validate(data)


P1 = pool("SysvarRent111111111111111111111111111111111", MINT_A,
          fees_1h="100", tvl="50000", hour="200000", short="10000")
P2 = pool("SysvarC1ock11111111111111111111111111111111", MINT_A,
          fees_1h="300", tvl="10000", hour="300000", short=None)
P3 = pool("Stake11111111111111111111111111111111111111", MINT_A,
          fees_1h="300", tvl="80000", hour="400000", short="30000")
B1 = pool("Vote111111111111111111111111111111111111111", MINT_B,
          fees_1h="900", tvl="90000", hour="1000000", short="80000")


def test_of_three_pools_the_largest_hourly_lp_fees_lead_and_tvl_breaks_a_tie() -> None:
    groups = {group.base_mint: group for group in token_groups([P1, P2, P3, B1])}

    # P2 and P3 both took 300 in LP fees in the hour; P3 has the larger TVL (80k > 10k).
    assert [item.pool.address.root for item in groups[MINT_A].pools] == [
        P3.pool.address.root,
        P2.pool.address.root,
        P1.pool.address.root,
    ]
    assert groups[MINT_A].volume_1h == Decimal("900000")  # 200k + 300k + 400k
    assert groups[MINT_B].volume_1h == Decimal("1000000")


def test_tokens_are_ranked_by_their_hourly_volume() -> None:
    ranked = token_groups([P1, P2, P3, B1])

    assert [group.base_mint for group in ranked] == [MINT_B, MINT_A]


def test_a_pool_sees_its_siblings_volume_and_which_of_them_lack_a_5m_window() -> None:
    (_, group) = token_groups([P1, P2, P3, B1])

    selection = group.selection_for(P3.pool.address.root, held=True, enhanced_pending=True)

    assert selection == TokenSelection(
        sibling_pools=2,
        sibling_volume_1h=Decimal("500000"),  # P1 200k + P2 300k
        sibling_volume_5m=Decimal("10000"),  # P1 only; P2 has no 5m window
        sibling_pools_without_5m=1,
        held=True,
        enhanced_pending=True,
    )


def test_a_pool_without_an_hour_window_is_ordered_last_and_adds_no_hourly_volume() -> None:
    data = P1.model_dump()
    del data["windows"]["1h"]
    data["data_quality"] = DataQuality.PARTIAL
    no_hour = PoolSnapshot.model_validate(data)

    (group,) = token_groups([no_hour, P3])

    assert [item.pool.address.root for item in group.pools] == [
        P3.pool.address.root,
        no_hour.pool.address.root,
    ]
    assert group.volume_1h == Decimal("400000")
