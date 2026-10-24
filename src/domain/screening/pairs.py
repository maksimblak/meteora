"""Which side of a DLMM pair is the coin (G24, one position per token; F5).

A Meteora pool is a ``token_x``/``token_y`` pair, kept as ``PoolRef.base_mint``/``quote_mint``
in that order: the price is token_y per token_x, the active bin and both decimals are read
by side, and an entry spends token_y below the active bin. The coin the author trades is
the side that is not a quote token, whichever side it is - SOL-MEME is a pool of MEME as
much as MEME-SOL is. ``pair_token`` names it; the cycle groups and holds pools by it.

Prices, bins and quotes of this build are taken from ``token_x``, so only a pool of the coin
first and a quote token second (``TOKEN_FIRST``) can be entered; the other shapes are skipped
by guide-v1 with their own reason (``screener.assess_guide_thresholds``).
"""

from dataclasses import dataclass
from enum import StrEnum

from src.market.models import PoolRef

# Quote tokens on Solana mainnet, by mint:
# - wrapped SOL: the SPL Token program's native mint (``NATIVE_MINT`` of @solana/spl-token;
#   solana.com docs, "Wrapping SOL");
# - USDC: Circle, "USDC Contract Addresses" (developers.circle.com), Solana mainnet;
# - USDT: Tether, "Supported Protocols" (tether.to), Solana.
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = frozenset({WRAPPED_SOL_MINT, USDC_MINT, USDT_MINT})


class PairShape(StrEnum):
    """Where the coin stands in the pair.

    ``TOKEN_FIRST``: the coin is token_x and a quote token token_y - the tradable shape;
    ``TOKEN_SECOND``: a quote token is token_x and the coin token_y (SOL-MEME);
    ``QUOTE_ONLY``: both sides are quote tokens (SOL-USDC), no coin;
    ``NO_QUOTE``: neither side is a quote token (MEME-JUP).
    """

    TOKEN_FIRST = "TOKEN_FIRST"  # noqa: S105 - stable domain code
    TOKEN_SECOND = "TOKEN_SECOND"  # noqa: S105 - stable domain code
    QUOTE_ONLY = "QUOTE_ONLY"
    NO_QUOTE = "NO_QUOTE"


@dataclass(frozen=True, slots=True)
class PairToken:
    """The coin of a pair; ``token_x`` when the pair has no single quote token."""

    mint: str
    shape: PairShape

    @property
    def tradable(self) -> bool:
        return self.shape is PairShape.TOKEN_FIRST


def pair_token(pool: PoolRef) -> PairToken:
    x, y = pool.base_mint.root, pool.quote_mint.root
    x_quote, y_quote = x in QUOTE_MINTS, y in QUOTE_MINTS
    if x_quote and y_quote:
        return PairToken(x, PairShape.QUOTE_ONLY)
    if y_quote:
        return PairToken(x, PairShape.TOKEN_FIRST)
    if x_quote:
        return PairToken(y, PairShape.TOKEN_SECOND)
    return PairToken(x, PairShape.NO_QUOTE)
