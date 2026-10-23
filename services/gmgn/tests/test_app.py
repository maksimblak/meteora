"""The internal HTTP surface: signals, liveness, readiness and input validation."""

from decimal import Decimal

from fastapi.testclient import TestClient

from gmgn_service.app import create_app
from gmgn_service.config import ServiceSettings
from gmgn_service.extract import Extraction
from gmgn_service.service import PageUnavailable, SignalService

MINT = "CSocnzcRpgSCr1naKcNTieWZUxb6rABe45j844m7pump"


class StaticFetcher:
    def __init__(self, outcome: Extraction | PageUnavailable, *, ready: bool = True) -> None:
        self.outcome = outcome
        self._ready = ready
        self.calls: list[tuple[str, str]] = []

    @property
    def ready(self) -> bool:
        return self._ready

    async def fetch(self, chain: str, mint: str) -> Extraction:
        self.calls.append((chain, mint))
        if isinstance(self.outcome, PageUnavailable):
            raise self.outcome
        return self.outcome


def client(fetcher: StaticFetcher) -> TestClient:
    settings = ServiceSettings()
    return TestClient(
        create_app(settings=settings, service=SignalService(fetcher, settings=settings))
    )


def test_signals_are_served_as_exact_decimal_strings() -> None:
    fetcher = StaticFetcher(
        Extraction(
            signals={
                "top10_holder_share": Decimal("0.7904"),
                "insiders_share": Decimal("0.7533"),
                "bundling_share": Decimal("0.1139"),
                "phishing_share": Decimal("0.8081"),
                "developer_share": Decimal("0"),
                "total_fees_sol": Decimal("1922.2996902657378"),
            },
            sources=("mutil_window_token_info", "token_stat"),
        )
    )

    with client(fetcher) as http:
        response = http.get(f"/v1/tokens/sol/{MINT}/signals")

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "gmgn-signals.v1"
    assert body["status"] == "AVAILABLE" and body["freshness"] == "FRESH"
    assert body["signals"]["insiders_share"] == "0.7533"
    assert body["signals"]["total_fees_sol"] == "1922.2996902657378"
    assert fetcher.calls == [("sol", MINT)]


def test_an_unreadable_page_is_a_200_answer_saying_unavailable_and_why() -> None:
    with client(StaticFetcher(PageUnavailable("ACCESS_BLOCKED"))) as http:
        body = http.get(f"/v1/tokens/sol/{MINT}/signals").json()

    assert (body["status"], body["reason"], body["source_at"]) == (
        "UNAVAILABLE",
        "ACCESS_BLOCKED",
        None,
    )
    assert all(value is None for value in body["signals"].values())


def test_unknown_chains_and_malformed_mints_never_open_a_page() -> None:
    fetcher = StaticFetcher(PageUnavailable("NO_SIGNALS"))

    with client(fetcher) as http:
        chain = http.get(f"/v1/tokens/eth/{MINT}/signals")
        mint = http.get("/v1/tokens/sol/not-a-mint!/signals")
        long_mint = http.get(f"/v1/tokens/sol/{'1' * 45}/signals")

    assert chain.status_code == 404
    assert chain.json()["error"]["code"] == "CHAIN_UNSUPPORTED"
    assert mint.status_code == 422 and long_mint.status_code == 422
    assert fetcher.calls == []


def test_liveness_is_the_process_and_readiness_the_browser_and_the_circuit() -> None:
    with client(StaticFetcher(PageUnavailable("NO_SIGNALS"), ready=False)) as http:
        live = http.get("/health/live")
        ready = http.get("/health/ready")

    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready", "reasons": ["BROWSER_UNAVAILABLE"]}
