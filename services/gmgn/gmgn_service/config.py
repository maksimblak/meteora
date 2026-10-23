"""Settings of the ``gmgn`` service, read once from the environment (names only in .env)."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    """Every bound of the service; the defaults are its compose defaults.

    ``page_timeout``: one page load, challenge included; ``settle``: how long a page that
    already showed some metrics may take for the rest; ``response_wait``: how long one
    HTTP request waits for a load before it answers ``FETCH_PENDING`` (the load goes on
    and fills the cache). Times are seconds.
    """

    base_url: str = "https://gmgn.ai"
    host: str = "0.0.0.0"  # noqa: S104 - the compose-internal network only (expose, no ports)
    port: int = 8080
    page_timeout: float = 45.0
    settle: float = 8.0
    cache_ttl: float = 600.0
    max_pages: int = 2
    max_pending: int = 16
    max_cached: int = 2048
    response_wait: float = 10.0
    backoff_base: float = 60.0
    backoff_max: float = 1800.0
    breaker_threshold: int = 3
    breaker_cooldown: float = 900.0

    def __post_init__(self) -> None:
        if not self.base_url.startswith("https://"):
            raise ValueError("GMGN_BASE_URL must be an https URL")
        positive = (
            self.page_timeout,
            self.settle,
            self.cache_ttl,
            self.response_wait,
            self.backoff_base,
            self.breaker_cooldown,
        )
        if any(value <= 0 for value in positive) or self.backoff_max < self.backoff_base:
            raise ValueError("gmgn service times must be positive and the backoff bounded")
        if min(self.max_pages, self.max_pending, self.max_cached, self.breaker_threshold) < 1:
            raise ValueError("gmgn service limits must be at least one")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "ServiceSettings":
        def number(name: str, default: float) -> float:
            raw = (environ.get(name) or "").strip()
            return float(raw) if raw else default

        def integer(name: str, default: int) -> int:
            raw = (environ.get(name) or "").strip()
            return int(raw) if raw else default

        defaults = cls()
        return cls(
            base_url=(environ.get("GMGN_BASE_URL") or "").strip() or defaults.base_url,
            host=(environ.get("GMGN_HOST") or "").strip() or defaults.host,
            port=integer("GMGN_PORT", defaults.port),
            page_timeout=number("GMGN_PAGE_TIMEOUT_SECONDS", defaults.page_timeout),
            settle=number("GMGN_SETTLE_SECONDS", defaults.settle),
            cache_ttl=number("GMGN_CACHE_TTL_SECONDS", defaults.cache_ttl),
            max_pages=integer("GMGN_MAX_PAGES", defaults.max_pages),
            max_pending=integer("GMGN_MAX_PENDING", defaults.max_pending),
            response_wait=number("GMGN_RESPONSE_WAIT_SECONDS", defaults.response_wait),
            backoff_base=number("GMGN_BACKOFF_SECONDS", defaults.backoff_base),
            backoff_max=number("GMGN_BACKOFF_MAX_SECONDS", defaults.backoff_max),
            breaker_threshold=integer("GMGN_BREAKER_THRESHOLD", defaults.breaker_threshold),
            breaker_cooldown=number("GMGN_BREAKER_COOLDOWN_SECONDS", defaults.breaker_cooldown),
        )
