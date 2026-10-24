"""F5 (T18 review): with the GMGN key the cycle ranks tokens by GMGN's hourly volume of the
token wherever the provider already holds a reading, else by the sum of its DLMM pools.

The worker runs its real candidate cycle with the real ``GmgnOpenApiProvider`` on a mock
OpenAPI; only the top-ranked token (limit 1) is checked in depth.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from prometheus_client import CollectorRegistry
from pydantic import SecretStr

from src.core.clock import FrozenClock
from src.domain.models.primitives import PublicKey
from src.domain.screening import RejectionCode
from src.gmgn.openapi import OPENAPI_URL, GmgnOpenApiProvider
from src.observability.metrics import WorkerMetrics
from src.persistence.contracts import DecisionRecord, JobLease
from src.persistence.memory import InMemoryEventSink
from src.worker.policy import GuidePolicySettings, guide_policy
from src.worker.runtime import WorkerRuntime, WorkerSettings
from src.worker.scheduler import JobContext
from tests.domain.guide_fixtures import NOW, guide_token
from tests.gmgn.test_gmgn_openapi import KEY, Api, Sleeper, asked, info_body, mint_number
from tests.worker.harness import memory_repositories
from tests.worker.test_t18_cycle import CountingOnChain, pool_history

MINT_A, MINT_B = mint_number(41), mint_number(42)
POOL_A, POOL_B = mint_number(51), mint_number(52)
# GMGN's hourly volume of B; A is never read. DLMM sums: A 1 400 000, B 900 000.
GMGN_HOUR = {MINT_B.root: "5000000"}


def info(request: httpx.Request) -> httpx.Response:
    mint = asked(request)
    body = info_body(mint)
    body["data"]["price"].update(volume_5m="100000", volume_1h=GMGN_HOUR.get(mint.root, "1"))
    return httpx.Response(200, json=body)


@dataclass
class Cycle:
    records: dict[str, DecisionRecord]
    reads: dict[str, int]
    gmgn_asked: list[str]


def run_cycle(*, cached: tuple[PublicKey, ...]) -> Cycle:
    async def scenario() -> Cycle:
        clock = FrozenClock(NOW)
        repos = memory_repositories(clock)
        api = Api(clock, info=(info,))
        async with httpx.AsyncClient(
            base_url=OPENAPI_URL, transport=httpx.MockTransport(api)
        ) as client:
            gmgn = GmgnOpenApiProvider(
                client, api_key=SecretStr(KEY), clock=clock, sleep=Sleeper(clock)
            )
            onchain = CountingOnChain(
                {
                    mint.root: guide_token().model_copy(update={"mint": mint})
                    for mint in (MINT_A, MINT_B)
                },
                clock,
            )
            runtime = WorkerRuntime(
                settings=WorkerSettings(metrics_port=None, owner="worker-1"),
                policy=guide_policy(
                    GuidePolicySettings(
                        baseline_points=6,
                        minimum_baseline_points=6,
                        maximum_history_points=20,
                        maximum_deep_candidates=1,
                    )
                ),
                repositories=repos,
                market=None,
                onchain=onchain,
                clock=clock,
                metrics=WorkerMetrics(CollectorRegistry()),
                sink=InMemoryEventSink(),
                gmgn=gmgn,
            )
            snapshots = [
                *pool_history(POOL_A, MINT_A, hour="1400000"),
                *pool_history(POOL_B, MINT_B, hour="900000"),
            ]
            for at in sorted({item.observed_at for item in snapshots}):
                stage = await repos.universe.begin(uuid4(), at)
                await stage.stage(tuple(item for item in snapshots if item.observed_at == at))
                await stage.commit()
            for mint in cached:  # an earlier cycle's deep check left these readings
                await gmgn.get_token_signals(mint)
            api.requests.clear()
            lease = JobLease(
                name="candidate_pipeline",
                owner="worker-1",
                token=1,
                acquired_at=NOW,
                expires_at=NOW + timedelta(minutes=2),
            )
            await runtime.candidate_job(
                JobContext(
                    name="candidate_pipeline",
                    correlation_id=uuid4(),
                    started_at=NOW,
                    lease=lease,
                )
            )
            return Cycle(
                records={
                    record.decision.pool_address.root: record
                    for record in repos.decisions.decisions.values()
                },
                reads=dict(onchain.reads),
                gmgn_asked=[asked(request).root for request in api.requests],
            )

    return asyncio.run(scenario())


def rank(record: DecisionRecord) -> dict[str, Any]:
    found = {item.name: (item.value, item.threshold) for item in record.decision.evidence}
    return {
        "rank": found["token_volume_rank"],
        "source": found.get("token_volume_rank_source", ("MISSING", None))[0],
        "below": RejectionCode.CANDIDATE_RANK_BELOW_LIMIT in record.decision.reasons,
    }


def test_a_token_gmgn_already_read_ranks_by_its_gmgn_hourly_volume() -> None:
    cycle = run_cycle(cached=(MINT_B,))

    # B: 5 000 000 an hour by GMGN beats A's 1 400 000 over its DLMM pools.
    assert rank(cycle.records[POOL_B.root]) == {
        "rank": ("1", "1"),
        "source": "GMGN",
        "below": False,
    }
    assert rank(cycle.records[POOL_A.root]) == {
        "rank": ("2", "1"),
        "source": "DLMM_POOLS",
        "below": True,
    }
    assert cycle.reads == {MINT_B.root: 1}
    assert cycle.gmgn_asked == []  # the deep check of B was served from the cache too


@pytest.mark.parametrize("cached", [(), (MINT_A,)], ids=["nothing_cached", "cache_of_a"])
def test_without_a_gmgn_reading_of_the_other_token_the_pool_sums_rank(
    cached: tuple[PublicKey, ...]
) -> None:
    cycle = run_cycle(cached=cached)

    # Nothing cached: 1 400 000 (A) over 900 000 (B) by their DLMM pools. A cached: GMGN
    # reports 1 an hour for A, so B's 900 000 over its pools ranks first.
    first, second = (POOL_A, POOL_B) if not cached else (POOL_B, POOL_A)
    assert rank(cycle.records[first.root])["rank"] == ("1", "1")
    assert rank(cycle.records[second.root])["below"] is True
