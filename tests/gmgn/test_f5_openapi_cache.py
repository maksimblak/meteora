"""F5 (T18 review): what the GMGN OpenAPI provider already holds can rank tokens for free.

``cached_signals`` answers from the cache of good readings only - no request, nothing past
``cache_ttl``, never a refused or failed reading.
"""

from datetime import timedelta

from src.core.clock import FrozenClock
from tests.gmgn.test_gmgn_openapi import BONK, NOW, WIF, Api, error, run


def test_a_good_reading_is_served_from_the_cache_without_a_request() -> None:
    clock = FrozenClock(NOW)
    api = Api(clock)
    (reading,), provider = run(api, [WIF], clock=clock, cache_ttl=timedelta(minutes=5))
    sent = len(api.requests)

    assert provider.cached_signals(WIF) == reading
    assert provider.cached_signals(BONK) is None  # never read
    clock.advance(timedelta(minutes=5))
    assert provider.cached_signals(WIF) is None  # as old as the cache allows: read again
    assert len(api.requests) == sent


def test_a_refused_reading_is_never_served_from_the_cache() -> None:
    clock = FrozenClock(NOW)
    api = Api(clock, info=(error(401, {"code": 401, "error": "AUTH_KEY_INVALID"}),))
    (reading,), provider = run(api, [WIF], clock=clock)

    assert reading.detail == "AUTH_KEY_INVALID"
    assert provider.cached_signals(WIF) is None
