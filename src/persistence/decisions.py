"""PostgreSQL decision records, their per-pool heads and the decision journal (R29i.2, C5).

* Records are immutable, with deterministic identifiers, so a retried cycle never
  duplicates them.
* ``candidate_decision_heads`` holds, per pool, the decision that stands and the last
  cycle that confirmed it. ``record_evaluation`` writes a new record only when the pool's
  outcome changed; otherwise it only moves the head's ``evaluated_at``. Storage grows with
  changes, not with pools x cycles.
* The journal reads (pages, plan of a decision, standing decision per pool, current
  outcomes) live here, on the indexes of migration 0004; the API sees only the protocol.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, union_all, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.screening import CandidateStatus
from src.persistence.contracts import (
    DecisionOutcome,
    DecisionPage,
    DecisionQuery,
    DecisionRecord,
    PlanRecord,
    RecordedDecision,
    outcome_key,
)
from src.persistence.market_store import as_uuid, ensure_snapshot, pool_id_for
from src.persistence.models import (
    CandidateDecisionHeadRecord,
    CandidateDecisionRecord,
    FeatureVectorRecord,
    PoolRecord,
    StrategyPlanRecord,
)
from src.persistence.postgres import PostgresDatabase
from src.persistence.serialization import from_payload, to_payload

Head = CandidateDecisionHeadRecord
Decision = CandidateDecisionRecord


class PostgresDecisionStore:
    """``DecisionStore`` and ``DecisionJournal`` over one PostgreSQL database."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._db = database

    # --- writes ---------------------------------------------------------------------------

    async def record_decision(
        self, record: DecisionRecord, *, evaluated_at: datetime | None = None
    ) -> UUID:
        cycle = evaluated_at or record.decision.decision_at
        stored = await self._record(record, cycle, force=True)
        return stored.decision_id

    async def record_evaluation(
        self, record: DecisionRecord, *, evaluated_at: datetime
    ) -> RecordedDecision:
        return await self._record(record, evaluated_at, force=False)

    async def _record(
        self, record: DecisionRecord, evaluated_at: datetime, *, force: bool
    ) -> RecordedDecision:
        decision = record.decision
        pool_id = pool_id_for(decision.pool_address.root)
        key = outcome_key(decision)

        async def work(session: AsyncSession) -> RecordedDecision:
            head = (
                await session.execute(
                    select(
                        Head.decision_id,
                        Head.outcome_key,
                        Head.evaluated_at,
                        Head.previous_evaluated_at,
                    )
                    .where(Head.pool_id == pool_id)
                    .with_for_update()
                )
            ).one_or_none()
            if not force and head is not None and head.outcome_key == key:
                if evaluated_at > head.evaluated_at:
                    await session.execute(
                        update(Head)
                        .where(Head.pool_id == pool_id)
                        .values(
                            evaluated_at=evaluated_at,
                            previous_evaluated_at=head.evaluated_at,
                        )
                    )
                return RecordedDecision(head.decision_id, recorded=False)
            snapshot_id, _ = await ensure_snapshot(session, record.market)
            feature_id = await _ensure_features(session, snapshot_id, record)
            await session.execute(
                pg_insert(Decision)
                .values(
                    id=record.decision_id,
                    pool_id=pool_id,
                    feature_vector_id=feature_id,
                    status=decision.status.value,
                    score=decision.score,
                    reasons=[reason.value for reason in decision.reasons],
                    missing_signals=list(decision.missing_signals),
                    evidence=to_payload(record),
                    ruleset_version=decision.ruleset_version,
                    correlation_id=as_uuid(decision.correlation_id),
                    decided_at=decision.decision_at,
                    mode=decision.mode.value,
                    config_version=decision.config_version,
                )
                .on_conflict_do_nothing(index_elements=[Decision.id])
            )
            previous = None
            if head is not None:
                previous = (
                    head.evaluated_at
                    if head.evaluated_at < evaluated_at
                    else head.previous_evaluated_at
                )
            head_values = {
                "decision_id": record.decision_id,
                "outcome_key": key,
                "status": decision.status.value,
                "reasons": [reason.value for reason in decision.reasons],
                "decided_at": decision.decision_at,
                "evaluated_at": evaluated_at,
                "previous_evaluated_at": previous,
            }
            await session.execute(
                pg_insert(Head)
                .values(pool_id=pool_id, **head_values)
                .on_conflict_do_update(index_elements=[Head.pool_id], set_=head_values)
            )
            return RecordedDecision(record.decision_id, recorded=True)

        result: RecordedDecision = await self._db.run_async(work)
        return result

    async def record_plan(self, record: PlanRecord) -> UUID:
        plan = record.sized_plan.plan

        async def work(session: AsyncSession) -> UUID:
            await session.execute(
                pg_insert(StrategyPlanRecord)
                .values(
                    id=record.plan_id,
                    decision_id=record.decision_id,
                    strategy=plan.strategy.value,
                    parameters=to_payload(record),
                    ruleset_version=plan.ruleset_version,
                    correlation_id=as_uuid(plan.correlation_id),
                    created_at=record.recorded_at,
                )
                .on_conflict_do_nothing(index_elements=[StrategyPlanRecord.id])
            )
            return record.plan_id

        result: UUID = await self._db.run_async(work)
        return result

    # --- reads ----------------------------------------------------------------------------

    async def decision(self, decision_id: UUID) -> DecisionRecord | None:
        async def work(session: AsyncSession) -> DecisionRecord | None:
            payload = await session.scalar(
                select(Decision.evidence).where(Decision.id == decision_id)
            )
            return None if payload is None else from_payload(DecisionRecord, payload)

        result: DecisionRecord | None = await self._db.run_async(work)
        return result

    async def plan(self, plan_id: UUID) -> PlanRecord | None:
        async def work(session: AsyncSession) -> PlanRecord | None:
            payload = await session.scalar(
                select(StrategyPlanRecord.parameters).where(StrategyPlanRecord.id == plan_id)
            )
            return None if payload is None else from_payload(PlanRecord, payload)

        result: PlanRecord | None = await self._db.run_async(work)
        return result

    # --- DecisionJournal ------------------------------------------------------------------

    async def page(self, query: DecisionQuery) -> DecisionPage:
        """Keyset pagination on ``seq`` (index ``(seq)`` / ``(pool_id, seq DESC)``)."""

        async def work(session: AsyncSession) -> DecisionPage:
            statement = (
                select(Decision.seq, Decision.evidence)
                .order_by(Decision.seq.desc())
                .limit(query.limit + 1)
            )
            if query.cursor is not None:
                statement = statement.where(Decision.seq < query.cursor)
            if query.status is not None:
                statement = statement.where(Decision.status == query.status.value)
            if query.pool_address is not None:
                statement = statement.where(Decision.pool_id == pool_id_for(query.pool_address))
            rows = (await session.execute(statement)).all()
            visible = rows[: query.limit]
            items = tuple(from_payload(DecisionRecord, row.evidence) for row in visible)
            more = len(rows) > query.limit and bool(visible)
            return DecisionPage(items, int(visible[-1].seq) if more else None)

        result: DecisionPage = await self._db.run_async(work)
        return result

    async def plan_for(self, decision_id: UUID) -> PlanRecord | None:
        async def work(session: AsyncSession) -> PlanRecord | None:
            payload = await session.scalar(
                select(StrategyPlanRecord.parameters)
                .where(StrategyPlanRecord.decision_id == decision_id)
                .order_by(StrategyPlanRecord.created_at.desc())
                .limit(1)
            )
            return None if payload is None else from_payload(PlanRecord, payload)

        result: PlanRecord | None = await self._db.run_async(work)
        return result

    async def latest_by_pool(self, addresses: Sequence[str]) -> dict[str, DecisionRecord]:
        if not addresses:
            return {}

        async def work(session: AsyncSession) -> dict[str, DecisionRecord]:
            rows = await session.execute(
                select(PoolRecord.address, Decision.evidence)
                .join(Head, Head.pool_id == PoolRecord.id)
                .join(Decision, Decision.id == Head.decision_id)
                .where(PoolRecord.address.in_(list(addresses)))
            )
            return {row.address: from_payload(DecisionRecord, row.evidence) for row in rows}

        result: dict[str, DecisionRecord] = await self._db.run_async(work)
        return result

    async def latest_outcomes(self) -> tuple[DecisionOutcome, ...]:
        """Heads evaluated since the last complete cycle: the newest cycle instant any
        pool saw before the latest one (the running cycle has only part of the pools)."""

        async def work(session: AsyncSession) -> tuple[DecisionOutcome, ...]:
            latest = select(func.max(Head.evaluated_at)).scalar_subquery()
            moments = union_all(
                select(Head.evaluated_at.label("moment")),
                select(Head.previous_evaluated_at.label("moment")),
            ).subquery()
            earlier = (
                select(func.max(moments.c.moment))
                .where(moments.c.moment < latest)
                .scalar_subquery()
            )
            rows = await session.execute(
                select(PoolRecord.address, Head.status, Head.reasons, Head.decided_at)
                .join(PoolRecord, PoolRecord.id == Head.pool_id)
                .where(Head.evaluated_at >= func.coalesce(earlier, latest))
            )
            return tuple(_outcome(row) for row in rows)

        result: tuple[DecisionOutcome, ...] = await self._db.run_async(work)
        return result


async def _ensure_features(
    session: AsyncSession, snapshot_id: UUID, record: DecisionRecord
) -> UUID:
    decision = record.decision
    await session.execute(
        pg_insert(FeatureVectorRecord)
        .values(
            id=uuid5(NAMESPACE_URL, f"feature-vector:{snapshot_id}:{decision.ruleset_version}"),
            pool_snapshot_id=snapshot_id,
            ruleset_version=decision.ruleset_version,
            values=to_payload(decision.features),
        )
        .on_conflict_do_nothing()
    )
    feature_id = await session.scalar(
        select(FeatureVectorRecord.id).where(
            FeatureVectorRecord.pool_snapshot_id == snapshot_id,
            FeatureVectorRecord.ruleset_version == decision.ruleset_version,
        )
    )
    if feature_id is None:  # pragma: no cover - guarded by the upsert above
        raise RuntimeError("feature vector row disappeared")
    return feature_id


def _outcome(row: Any) -> DecisionOutcome:
    return DecisionOutcome(
        pool_address=row.address,
        status=CandidateStatus(row.status),
        reasons=tuple(str(reason) for reason in row.reasons),
        decided_at=row.decided_at,
    )
