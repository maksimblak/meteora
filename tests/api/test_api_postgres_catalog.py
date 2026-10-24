"""Decision read model on PostgreSQL: keyset pages, plan lookup, latest per pool.

Runs only with ``TEST_DATABASE_URL`` (the Compose ``check`` service); skipped otherwise.
"""

import asyncio
from collections.abc import Iterator

import pytest

from src.core.clock import FrozenClock
from src.domain.screening import CandidateStatus
from src.persistence.contracts import DecisionJournal, DecisionQuery
from src.persistence.decisions import PostgresDecisionStore
from src.persistence.postgres import PostgresDatabase
from src.persistence.testing import integration_database_url, migrated_schema
from tests.api.support import POOL_B, POOL_C, Scenario, populated
from tests.worker.harness import NOW, POOL, Harness


@pytest.fixture
def database() -> Iterator[PostgresDatabase]:
    url = integration_database_url()
    if url is None:
        pytest.skip("PostgreSQL integration URL is not configured")
    with migrated_schema(url) as schema:
        connection = schema.connect()
        try:
            yield connection
        finally:
            connection.close()


def copy_records(scenario: Scenario, database: PostgresDatabase) -> None:
    store = PostgresDecisionStore(database)

    async def run() -> None:
        for record in scenario.harness.decisions.decisions.values():
            await store.record_decision(record)
        for plan in scenario.harness.decisions.plans.values():
            await store.record_plan(plan)

    asyncio.run(run())


def test_postgres_catalog_pages_newest_first_and_finds_plans(database: PostgresDatabase) -> None:
    scenario = populated(Harness(clock=FrozenClock(NOW)))
    copy_records(scenario, database)
    catalog: DecisionJournal = PostgresDecisionStore(database)
    accepted, warming, rejected = (
        scenario.accepted.decision_id,
        scenario.others[0].decision_id,
        scenario.others[1].decision_id,
    )

    first = asyncio.run(catalog.page(DecisionQuery(limit=2)))
    rest = asyncio.run(catalog.page(DecisionQuery(limit=2, cursor=first.next_cursor)))
    only_accepted = asyncio.run(catalog.page(DecisionQuery(limit=5, status=CandidateStatus.ACCEPT)))
    only_b = asyncio.run(catalog.page(DecisionQuery(limit=5, pool_address=POOL_B.address.root)))
    plan = asyncio.run(catalog.plan_for(accepted))
    latest = asyncio.run(catalog.latest_by_pool([POOL.address.root, POOL_C.address.root]))
    outcomes = asyncio.run(catalog.latest_outcomes())

    assert [item.decision_id for item in first.items] == [rejected, warming]
    assert first.next_cursor is not None
    assert [item.decision_id for item in rest.items] == [accepted] and rest.next_cursor is None
    assert [item.decision_id for item in only_accepted.items] == [accepted]
    assert [item.decision_id for item in only_b.items] == [warming]
    assert plan is not None and plan.plan_id == scenario.accepted.plan_id
    assert {address: record.decision_id for address, record in latest.items()} == {
        POOL.address.root: accepted,
        POOL_C.address.root: rejected,
    }
    assert {item.pool_address: item.status for item in outcomes} == {
        POOL.address.root: CandidateStatus.ACCEPT,
        POOL_B.address.root: CandidateStatus.WARMING_UP,
        POOL_C.address.root: CandidateStatus.REJECT,
    }
    assert "LOW_TVL" in next(
        item.reasons for item in outcomes if item.pool_address == POOL_C.address.root
    )
