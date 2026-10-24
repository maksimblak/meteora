"""The universe by base token (spec 6.8, G24): main pool first, token volume summed.

The author works a token, not a pool: GMGN sorted by the hour's volume, then the token's
main pool. Here the pools of one base mint form a ``TokenGroup``:

* ``pools`` - the order in which they are tried; the first that passes the cheap filters is
  the one pool of the token evaluated this cycle: the largest LP fees over the last hour
  (``windows["1h"].fees``, where the volume actually pays liquidity), on a tie the larger
  TVL, then the address so the order never depends on the input order;
* ``volume_1h`` - the token's hourly volume summed over its DLMM pools, the rank of the
  token among the cycle's candidates (GMGN's own figure is asked only for those checked in
  depth, so it cannot rank the whole universe);
* ``selection_for`` - what one pool needs to know about its siblings (``TokenSelection``).
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from src.domain.screening.models import TokenSelection
from src.market.models import PoolSnapshot

_HOUR = "1h"
_SHORT = "5m"


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
    """The DLMM pools of one base mint in the universe, main pool first."""

    base_mint: str
    pools: tuple[PoolSnapshot, ...]
    volume_1h: Decimal

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


def token_groups(snapshots: Sequence[PoolSnapshot]) -> tuple[TokenGroup, ...]:
    """The latest snapshots grouped by base mint, ranked by the token's hourly volume."""

    by_mint: defaultdict[str, list[PoolSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        by_mint[snapshot.pool.base_mint.root].append(snapshot)
    groups = [
        TokenGroup(
            base_mint=mint,
            pools=tuple(sorted(pools, key=_priority)),
            volume_1h=sum((_hour_volume(item) for item in pools), Decimal(0)),
        )
        for mint, pools in by_mint.items()
    ]
    return tuple(sorted(groups, key=lambda group: (-group.volume_1h, group.base_mint)))
