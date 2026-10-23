"""Execution records of both contract versions on the PostgreSQL store (canonical check).

Runs only with ``TEST_DATABASE_URL`` (the compose ``check`` service sets it); each test
works in its own migrated schema. A "restart" is a second connection pool.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.domain.models.trading import TradingMode
from src.execution.contracts import (
    DlmmCommandV1,
    DlmmCommandV2,
    DlmmOperation,
    ExecutionPhase,
    GatewayReceiptV2,
    GatewayState,
    LiquidityStrategy,
    SimulationResultV1,
    SimulationResultV2,
    bind_simulation_result,
    bind_simulation_result_v2,
    canonical_command_digest,
)
from src.execution.service import ExecutionRecord
from src.persistence.execution_store import PostgresExecutionStore
from src.persistence.testing import MigratedSchema, integration_database_url, migrated_schema

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
KEY = "11111111111111111111111111111111"
EXECUTOR_POSITION = "SysvarRent111111111111111111111111111111111"


@pytest.fixture
def postgres_schema() -> Iterator[MigratedSchema]:
    url = integration_database_url()
    if url is None:
        pytest.skip("PostgreSQL integration URL is not configured")
    with migrated_schema(url) as schema:
        yield schema


def v2_record(phase: ExecutionPhase, key: str, *, confirmed: bool) -> ExecutionRecord:
    command = DlmmCommandV2(
        command_id=uuid4(),
        idempotency_key=key,
        correlation_id=uuid4(),
        ruleset_version="guide-v1",
        mode=TradingMode.LIVE,
        operation=DlmmOperation.OPEN,
        pool_address=KEY,
        wallet_address=KEY,
        expires_at=NOW + timedelta(minutes=5),
        max_slippage_bps=100,
        strategy=LiquidityStrategy.SPOT,
        min_bin_id=7_998,
        max_bin_id=8_002,
        amount_x="10000",
        amount_y="10",
        active_bin_id=8_000,
        active_bin_tolerance=3,
        token_x_decimals=6,
        token_y_decimals=9,
    )
    simulation = bind_simulation_result_v2(
        command_id=command.command_id,
        command_digest=canonical_command_digest(command),
        success=True,
        simulated_at=NOW,
        blockhash="hash",
        last_valid_block_height=1,
        compute_units=1,
        network_fee_lamports="5000",
        priority_fee_lamports="0",
        price_impact_bps=10,
        min_bin_id=command.min_bin_id,
        max_bin_id=command.max_bin_id,
        position_address=EXECUTOR_POSITION,
        active_bin_id=8_001,
        token_x_decimals=6,
        token_y_decimals=9,
        errors=(),
    )
    receipt = (
        GatewayReceiptV2(
            command_id=command.command_id,
            command_digest=simulation.command_digest,
            simulation_digest=simulation.simulation_digest,
            idempotency_key=key,
            state=GatewayState.CONFIRMED,
            position_address=EXECUTOR_POSITION,
            signature="sig-1",
            signatures=("sig-1",),
            transaction_count=1,
            submitted_at=NOW,
            attempt_count=1,
        )
        if confirmed
        else None
    )
    return ExecutionRecord(command=command, simulation=simulation, phase=phase, receipt=receipt)


def v1_record(key: str) -> ExecutionRecord:
    command = DlmmCommandV1(
        command_id=uuid4(),
        idempotency_key=key,
        correlation_id=uuid4(),
        ruleset_version="rules-v1",
        mode=TradingMode.LIVE,
        operation=DlmmOperation.OPEN,
        pool_address=KEY,
        wallet_address=KEY,
        position_address=KEY,
        expires_at=NOW + timedelta(minutes=5),
        max_slippage_bps=100,
        strategy=LiquidityStrategy.SPOT,
        min_bin_id=-2,
        max_bin_id=2,
        amount_x="10",
        amount_y="10",
    )
    simulation = bind_simulation_result(
        command_id=command.command_id,
        command_digest=canonical_command_digest(command),
        success=True,
        simulated_at=NOW,
        blockhash="hash",
        last_valid_block_height=1,
        compute_units=1,
        network_fee_lamports="5000",
        priority_fee_lamports="0",
        price_impact_bps=10,
        min_bin_id=-2,
        max_bin_id=2,
        errors=(),
    )
    return ExecutionRecord(command=command, simulation=simulation, phase=ExecutionPhase.SIMULATED)


def test_postgres_reads_v2_records_and_still_reads_v1_ones(
    postgres_schema: MigratedSchema,
) -> None:
    confirmed = v2_record(ExecutionPhase.CONFIRMED, "entry:v2-confirmed", confirmed=True)
    unknown = v2_record(ExecutionPhase.UNKNOWN, "entry:v2-unknown", confirmed=False)
    legacy = v1_record("entry:v1")

    async def scenario() -> tuple[ExecutionRecord | None, ExecutionRecord | None, list[object]]:
        database = postgres_schema.connect()
        restarted_db = postgres_schema.connect()
        try:
            store = PostgresExecutionStore(database)
            for record in (confirmed, unknown, legacy):
                await store.put(record)
            restarted = PostgresExecutionStore(restarted_db)
            loaded = await restarted.get(confirmed.command.command_id)
            old = await restarted.get_by_idempotency_key("entry:v1")
            pending = await restarted.in_phases((ExecutionPhase.UNKNOWN,))
            return loaded, old, [record.command for record in pending]
        finally:
            database.close()
            restarted_db.close()

    loaded, old, pending = asyncio.run(scenario())

    assert loaded is not None
    assert loaded.command == confirmed.command and isinstance(loaded.command, DlmmCommandV2)
    assert isinstance(loaded.simulation, SimulationResultV2)
    assert loaded.simulation == confirmed.simulation
    assert loaded.receipt == confirmed.receipt
    assert loaded.position_address == EXECUTOR_POSITION
    assert old is not None and old.command == legacy.command
    assert isinstance(old.simulation, SimulationResultV1)
    assert pending == [unknown.command]
