"""F5 (T18 review): the source of the GMGN token volume is named as checked.

``price.volume_*`` of token info matched the trending rank's hourly volume of a one-pool
pump.fun token, while WIF's hourly figure (1 732 USD against 274 040 over 24 h) is far below
its known turnover - so it is "GMGN's token volume, as in Trending", never a volume "over
every pool". The texts that describe it say so.
"""

import inspect
import re
from pathlib import Path

from src.domain import screening
from src.gmgn import openapi

UNVERIFIED = re.compile(
    r"(every|all)\s+pools?\s+of\s+the\s+token|over\s+every\s+pool|по\s+всем\s+пулам", re.I
)


def test_the_openapi_reader_names_gmgn_token_volume_as_in_trending() -> None:
    text = inspect.getdoc(openapi._market) or ""

    assert "as in Trending" in text
    assert not UNVERIFIED.search(text)


def test_no_screening_text_claims_gmgn_counts_every_pool() -> None:
    sources = [Path(openapi.__file__), *Path(screening.__file__).parent.glob("*.py")]

    for path in sources:
        assert not UNVERIFIED.search(path.read_text(encoding="utf-8")), path.name
