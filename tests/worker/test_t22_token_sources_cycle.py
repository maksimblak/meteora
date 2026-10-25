"""T22 in the worker's candidate cycle: Jupiter read for every token before screening,
RugCheck only for the candidates checked in depth.

The worker runs its real cycle (``WorkerRuntime.candidate_job``) with the real providers on
mock APIs. Three tokens:

* A - 1 400 000 an hour over its DLMM pool (23 333 a minute), but Jupiter counts 600 000
  over every venue (10 000 a minute): rejected by the cheap filters, never read in depth;
* B - 300 000 an hour over its pool (5 000 a minute), Jupiter 5 000 000: ranked first by
  Jupiter's figure, read in depth, RugCheck clean - accepted;
* C - Jupiter 2 000 000 an hour, but RugCheck rates a risk ``danger``: rejected in depth.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx
from prometheus_client import CollectorRegistry

from src.core.clock import FrozenClock
from src.core.config import AppConfig
from src.domain.screening import CandidateStatus, RejectionCode, TokenVolumeSource
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import DecisionRecord, JobLease
from src.persistence.memory import InMemoryEventSink
from src.providers.jupiter import JUPITER_URL, JupiterTokensProvider
from src.providers.rugcheck import RUGCHECK_URL, RugCheckProvider
from src.worker.policy import GuidePolicySettings, guide_policy
from src.worker.runtime import WorkerRuntime, WorkerSettings, build_token_sources
from src.worker.scheduler import JobContext
from tests.domain.guide_fixtures import NOW, guide_token
from tests.gmgn.test_gmgn_openapi import Sleeper, mint_number
from tests.worker.harness import memory_repositories
from tests.worker.test_t18_cycle import CountingOnChain, pool_history

MINT_A, MINT_B, MINT_C = mint_number(61), mint_number(62), mint_number(63)
POOL_A, POOL_B, POOL_C = mint_number(71), mint_number(72), mint_number(73)
# Jupiter's hourly and 5-minute volumes of each token (USD).
JUPITER = {
    MINT_A.root: (600_000, 50_000),
    MINT_B.root: (5_000_000, 500_000),
    MINT_C.root: (2_000_000, 200_000),
}
DANGER = {MINT_C.root: "Single holder ownership"}


def jupiter_token(mint: str) -> dict[str, Any]:
    hour, short = JUPITER[mint]
    return {
        "id": mint,
        "symbol": "COIN",
        "stats5m": {
            "buyVolume": short // 2,
            "sellVolume": short // 2,
            "numBuys": 200,
            "numSells": 200,
            "numTraders": 150,
        },
        "stats1h": {"buyVolume": hour // 2, "sellVolume": hour // 2},
        "organicScore": 85.25,
        "organicScoreLabel": "high",
        "audit": {
            "mintAuthorityDisabled": True,
            "freezeAuthorityDisabled": True,
            "topHoldersPercentage": 18.5,
        },
    }


@dataclass
class Cycle:
    records: dict[str, DecisionRecord]
    searches: list[list[str]]
    reports: list[str]
    rpc_reads: dict[str, int]


def run_cycle() -> Cycle:
    searches: list[list[str]] = []
    reports: list[str] = []

    def search(request: httpx.Request) -> httpx.Response:
        asked = request.url.params["query"].split(",")
        searches.append(asked)
        return httpx.Response(200, json=[jupiter_token(mint) for mint in asked if mint in JUPITER])

    def report(request: httpx.Request) -> httpx.Response:
        mint = request.url.path.split("/")[3]
        reports.append(mint)
        risks = [{"name": DANGER[mint], "level": "danger"}] if mint in DANGER else []
        return httpx.Response(
            200, json={"risks": risks, "score_normalised": 1, "lpLockedPct": 50}
        )

    async def scenario() -> Cycle:
        clock = FrozenClock(NOW)
        repos = memory_repositories(clock)
        async with (
            httpx.AsyncClient(
                base_url=JUPITER_URL, transport=httpx.MockTransport(search)
            ) as jupiter_client,
            httpx.AsyncClient(
                base_url=RUGCHECK_URL, transport=httpx.MockTransport(report)
            ) as rugcheck_client,
        ):
            onchain = CountingOnChain(
                {
                    mint.root: guide_token().model_copy(update={"mint": mint})
                    for mint in (MINT_A, MINT_B, MINT_C)
                },
                clock,
            )
            runtime = WorkerRuntime(
                settings=WorkerSettings(metrics_port=None, owner="worker-1"),
                policy=guide_policy(
                    GuidePolicySettings(
                        baseline_points=6, minimum_baseline_points=6, maximum_history_points=20
                    )
                ),
                repositories=repos,
                market=None,
                onchain=onchain,
                clock=clock,
                metrics=WorkerMetrics(CollectorRegistry()),
                sink=InMemoryEventSink(),
                jupiter=JupiterTokensProvider(jupiter_client, clock=clock, sleep=Sleeper(clock)),
                rugcheck=RugCheckProvider(rugcheck_client, clock=clock, sleep=Sleeper(clock)),
            )
            snapshots = [
                *pool_history(POOL_A, MINT_A, hour="1400000"),
                *pool_history(POOL_B, MINT_B, hour="300000"),
                *pool_history(POOL_C, MINT_C, hour="900000"),
            ]
            for at in sorted({item.observed_at for item in snapshots}):
                stage = await repos.universe.begin(uuid4(), at)
                await stage.stage(tuple(item for item in snapshots if item.observed_at == at))
                await stage.commit()
            lease = JobLease(
                name="candidate_pipeline",
                owner="worker-1",
                token=1,
                acquired_at=NOW,
                expires_at=NOW + timedelta(minutes=2),
            )
            await runtime.candidate_job(
                JobContext(
                    name="candidate_pipeline", correlation_id=uuid4(), started_at=NOW, lease=lease
                )
            )
            return Cycle(
                records={
                    record.decision.pool_address.root: record
                    for record in repos.decisions.decisions.values()
                },
                searches=searches,
                reports=reports,
                rpc_reads=dict(onchain.reads),
            )

    return asyncio.run(scenario())


def evidence(record: DecisionRecord) -> dict[str, str]:
    return {item.name: item.value for item in record.decision.evidence}


def test_one_search_reads_every_token_before_any_decision() -> None:
    cycle = run_cycle()

    # Ranked by DLMM sums before the search: A 1 400 000, C 900 000, B 300 000.
    assert cycle.searches == [[MINT_A.root, MINT_C.root, MINT_B.root]]
    for record in cycle.records.values():
        assert record.token.jupiter is not None
        assert record.token.jupiter.source_at <= record.decision.decision_at


def test_jupiters_volume_rejects_a_token_before_any_request_for_it() -> None:
    cycle = run_cycle()
    record = cycle.records[POOL_A.root]

    assert RejectionCode.HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD in record.decision.reasons
    assert evidence(record)["token_volume_source"] == TokenVolumeSource.JUPITER
    assert MINT_A.root not in cycle.rpc_reads
    assert MINT_A.root not in cycle.reports
    assert evidence(record)["rugcheck_status"] == "NOT_READ"


def test_a_token_thin_on_meteora_but_busy_elsewhere_ranks_first_and_is_accepted() -> None:
    cycle = run_cycle()
    record = cycle.records[POOL_B.root]
    found = evidence(record)

    assert record.decision.status is CandidateStatus.ACCEPT, record.decision.reasons
    assert (found["token_volume_rank"], found["token_volume_rank_source"]) == ("1", "JUPITER")
    # 5 000 000 an hour by Jupiter: 83 333.33... a minute.
    assert found["hourly_volume_per_minute"].startswith("83333.33")
    assert (found["rugcheck_status"], found["rugcheck_danger"]) == ("FRESH", "none")


def test_rugcheck_is_asked_for_the_deep_candidates_only_and_its_danger_rejects() -> None:
    cycle = run_cycle()
    record = cycle.records[POOL_C.root]

    assert sorted(cycle.reports) == sorted([MINT_B.root, MINT_C.root])
    assert cycle.rpc_reads == {MINT_B.root: 1, MINT_C.root: 1}
    assert record.decision.status is CandidateStatus.REJECT
    assert RejectionCode.RUGCHECK_DANGER in record.decision.reasons
    assert evidence(record)["rugcheck_danger"] == "Single holder ownership"


def test_the_sources_are_on_by_their_urls_and_off_when_empty() -> None:
    clock = FrozenClock(NOW)
    config = AppConfig.load(environ={})

    async def build(environ: dict[str, str]) -> tuple[Any, Any]:
        jupiter, rugcheck, clients = build_token_sources(config, environ, clock=clock)
        for client in clients:
            await client.aclose()
        return jupiter, rugcheck

    on = asyncio.run(build({"JUPITER_API_URL": JUPITER_URL, "RUGCHECK_API_URL": RUGCHECK_URL}))
    off = asyncio.run(build({"JUPITER_API_URL": "", "RUGCHECK_API_URL": " "}))

    assert isinstance(on[0], JupiterTokensProvider) and isinstance(on[1], RugCheckProvider)
    assert off == (None, None)
