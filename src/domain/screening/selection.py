"""The universe by token (spec 6.8, G24): main pool first, tokens ranked by hourly volume.

The author works a token, not a pool: GMGN sorted by the hour's volume, then the token's
main pool. Here the pools of one token form a ``TokenGroup``; the token of a pool is the
side of its pair that is not a quote token (``pairs.pair_token``, F5), so SOL-MEME and
MEME-SOL are pools of one token:

* ``pools`` - the order in which they are tried; the first that passes the cheap filters is
  the one pool of the token evaluated this cycle: the largest LP fees over the last hour
  (``windows["1h"].fees``, where the volume actually pays liquidity), on a tie the larger
  TVL, then the address so the order never depends on the input order;
* ``volume_1h`` - the token's hourly volume summed over its DLMM pools;
* ``token_volume_1h`` - GMGN's hourly volume of the token (as in Trending), when the cycle
  already holds a reading of it: GMGN is asked only for tokens checked in depth, so only
  those it has read lately carry one (F5);
* ``rank_volume`` - what ranks the token among the cycle's candidates: GMGN's figure where
  held, else the sum of its pools (``rank_source``);
* ``selection_for`` - what one pool needs to know about its siblings (``TokenSelection``).
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from src.domain.screening.models import TokenSelection, TokenVolumeSource
from src.domain.screening.pairs import pair_token
from src.market.models import PoolSnapshot

_HOUR = "1h"
_SHORT = "5m"

# The hourly volume of a token (by mint) an enhanced source already holds, or ``None``.
TokenVolumeLookup = Callable[[str], Decimal | None]


def _hour_volume(snapshot: PoolSnapshot) -> Decimal:
    window = snapshot.windows.get(_HOUR)
    return window.volume if window is not None else Decimal(0)


def _priority(snapshot: PoolSnapshot) -> tuple[bool, Decimal, Decimal, str]:
    """Sort key, smallest first: pools with hourly LP fees, the largest fees, the larger TVL."""

    window = snapshot.windows.get(_HOUR)
    fees = window.fees if window is not None else None
    return (
        fees is None,
        -(fees or Decimal(0)),
        -snapshot.tvl,
        snapshot.pool.address.root,
    )


@dataclass(frozen=True, slots=True)
class TokenGroup:
    """The DLMM pools of one token in the universe, main pool first.

    ``base_mint`` is the token's mint, whichever side of its pairs it stands on."""

    base_mint: str
    pools: tuple[PoolSnapshot, ...]
    volume_1h: Decimal
    token_volume_1h: Decimal | None = None

    @property
    def rank_source(self) -> TokenVolumeSource:
        if self.token_volume_1h is not None:
            return TokenVolumeSource.GMGN
        return TokenVolumeSource.DLMM_POOLS

    @property
    def rank_volume(self) -> Decimal:
        return self.token_volume_1h if self.token_volume_1h is not None else self.volume_1h

    def selection_for(
        self, pool_address: str, *, held: bool = False, enhanced_pending: bool = False
    ) -> TokenSelection:
        """The token as one of its pools sees it: the other pools' volumes and counts."""

        siblings = [item for item in self.pools if item.pool.address.root != pool_address]
        with_short = [item.windows[_SHORT] for item in siblings if _SHORT in item.windows]
        return TokenSelection(
            sibling_pools=len(siblings),
            sibling_volume_1h=sum((_hour_volume(item) for item in siblings), Decimal(0)),
            sibling_volume_5m=sum((window.volume for window in with_short), Decimal(0)),
            sibling_pools_without_5m=len(siblings) - len(with_short),
            held=held,
            enhanced_pending=enhanced_pending,
        )


def token_groups(
    snapshots: Sequence[PoolSnapshot], *, token_volume_1h: TokenVolumeLookup | None = None
) -> tuple[TokenGroup, ...]:
    """The latest snapshots grouped by token, ranked by the token's hourly volume:
    ``token_volume_1h`` (GMGN's figure already held) where it knows one, else the sum of
    the token's DLMM pools."""

    by_mint: defaultdict[str, list[PoolSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        by_mint[pair_token(snapshot.pool).mint].append(snapshot)
    groups = [
        TokenGroup(
            base_mint=mint,
            pools=tuple(sorted(pools, key=_priority)),
            volume_1h=sum((_hour_volume(item) for item in pools), Decimal(0)),
            token_volume_1h=None if token_volume_1h is None else token_volume_1h(mint),
        )
        for mint, pools in by_mint.items()
    ]
    return tuple(sorted(groups, key=lambda group: (-group.rank_volume, group.base_mint)))
