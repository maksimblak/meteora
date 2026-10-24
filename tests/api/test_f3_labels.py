"""F3 (C4, spec 9): every code the operator can meet in the panel has a Russian line."""

import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from src.accounting.models import LedgerKind
from src.api import labels
from src.api.presenters import alert_view, evidence_views, flag_notices
from src.domain.risk.models import RiskReason
from src.domain.screening.models import (
    DecisionFlag,
    EvidenceItem,
    RejectionCode,
    TokenVolumeSource,
)
from src.domain.screening.pairs import PairShape
from src.domain.strategy.models import PositionReason, StrategyFlag
from src.market.models import DeveloperStatus
from src.modes.models import CriticalAlert
from src.worker.execution import SaleStatus

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)

# Worker readiness (src/observability/health.py, WorkerHealthProbe) as /health/ready lists it.
WORKER_READINESS = (
    "NO_HEARTBEAT",
    "HEARTBEAT_STALE",
    "PERSISTENCE_UNAVAILABLE",
    "PROVIDERS_NOT_CONFIGURED",
    "PROVIDER_DEGRADED:meteora",
    "UNIVERSE_STALE",
    "CRITICAL_ALERTS_OPEN",
    "EXECUTION_GATEWAY_NOT_CONFIGURED",
    "EXECUTION_GATEWAY_NOT_READY",
    "WORKER_UNREACHABLE",
    "WORKER_HEALTH_NOT_CONFIGURED",
)
# Refusals of the executor (executor/src: engine.ts, sdk.ts; spec 8.5 D08) and of the
# worker's own entry guards (src/worker/execution.py), as they reach the operator.
EXECUTOR_CODES = (
    "ACTIVE_BIN_DRIFT",
    "ACTIVE_BIN_UNAVAILABLE",
    "POOL_STATE_MISMATCH",
    "SLIPPAGE_BREACH",
    "PRICE_IMPACT_UNAVAILABLE",
    "WALLET_INSUFFICIENT_BASE_TOKEN",
    "WALLET_INSUFFICIENT_QUOTE_TOKEN",
    "INSUFFICIENT_FUNDS",
    "INSUFFICIENT_LIQUIDITY",
    "SWAP_QUOTE_INSUFFICIENT_LIQUIDITY",
    "TOKEN_DECIMALS_MISMATCH",
    "MARKET_STATE_UNAVAILABLE",
    "POSITION_STATE_UNAVAILABLE",
    "TRANSACTION_SIMULATION_FAILED",
    "SIMULATION_INCOMPLETE",
    "STALE_SIMULATION",
    "FRESH_SIMULATION_REQUIRED",
    "COMMAND_EXPIRED",
    "MODE_NOT_AUTHORIZED",
    "MODE_TOKEN_REQUIRED",
    "LIVE_PROMOTION_REQUIRED",
    "VALID_APPROVAL_REQUIRED",
    "APPROVAL_ALREADY_USED",
    "IDEMPOTENCY_CONFLICT",
    "IDEMPOTENCY_MISMATCH",
    "SUBMISSION_DISABLED",
    "SHADOW_NO_SUBMIT",
    "PARTIAL_BROADCAST",
    "SUBMIT_OUTCOME_UNKNOWN",
    "QUOTE_FAILED",
    "ENTRY_ANCHOR_UNAVAILABLE",
    "EXECUTION_GATEWAY_NOT_CONFIGURED",
    # The sale after an exit (G22, T17/F4): the executor's and ``swap_rejection``'s codes.
    "PRICE_IMPACT_BREACH",
    "SWAP_QUOTE_INCOMPLETE",
    "SWAP_EXCEEDS_EXIT_RETURNS",
)
# Critical alerts the worker, recovery and mode service raise (src/worker, src/recovery,
# src/modes), with the sale's own (T17, F4: the sale of what an exit returned).
ALERT_CODES = (
    "DAILY_STOP_TRIGGERED",
    "KILL_SWITCH_ACTIVE",
    "EXECUTION_RECOVERY_REQUIRED",
    "DECISION_PERSISTENCE_FAILED",
    "AUDIT_PERSISTENCE_FAILED",
    "PLAN_PERSISTENCE_FAILED",
    "ENTRY_FILL_UNTRACKED",
    "ENTRY_POSITION_UNKNOWN",
    "EXIT_REQUIRES_EXECUTION_GATEWAY",
    "EXIT_GATEWAY_UNAVAILABLE",
    "POSITION_ACTION_FAILED",
    "POSITION_ACTION_ATTEMPTS_EXHAUSTED",
    "POSITION_PARTIALLY_EXECUTED",
    "GATEWAY_FILL_UNBOOKED",
    "POSITION_CONTEXT_MISSING",
    "EXIT_SALE_IMPACT_EXCEEDED",
    "EXIT_SALE_FAILED",
    "EXIT_SALE_AMOUNT_UNKNOWN",
    "EXIT_SALE_GATEWAY_UNAVAILABLE",
    "EXIT_SALE_OUTCOME_UNKNOWN",
    "EXIT_SALE_BLOCKED",
)
# ``SaleProgress.detail`` of a sale (src/worker/execution.py, T17/F4) as logs and the
# ``PositionClosed.exit_sale`` event carry it.
SALE_DETAILS = (
    "SETTLEMENT_PENDING",
    "SALE_SETTLEMENT_PENDING",
    "REDUCTION_SALE_PENDING",
    "NEXT_PART",
    "AMOUNT_UNKNOWN",
    "PRICE_IMPACT_EXCEEDED",
    "ATTEMPTS_EXHAUSTED",
    "GATEWAY_UNAVAILABLE",
    "SALE_OUTCOME_UNKNOWN",
    "SALE_BLOCKED",
    "CONTEXT_MISSING",
    "AWAITING_APPROVAL",
)
# Entry admission (EntryGate / ModeRepository.admit_entry) and the sizer's caps (RiskReason).
ADMISSION = ("RISK_DAY_NOT_OPENED", "KILL_SWITCH_ACTIVE", "DAILY_STOP_REACHED",
             "POSITION_LIMIT_REACHED", *(item.value for item in RiskReason))
# ``depth_source`` details (D07, F1) and ``gmgn_status`` values (G18, T16).
DEPTH_DETAILS = (
    "QUOTE_PRICE_UNKNOWN",
    "QUOTE_DECIMALS_UNKNOWN",
    "QUOTES_NOT_CONFIGURED",
    "NOT_A_CANDIDATE",
    "NOT_MEASURED",
    "REFERENCE_SIZE_ZERO",
    "INSUFFICIENT_LIQUIDITY",
    "QUOTE_FAILED",
)
GMGN_STATUSES = (
    "FRESH",
    "STALE",
    "NOT_CONFIGURED",
    "UNAVAILABLE:AUTH_KEY_INVALID",
    "UNAVAILABLE:AUTH_TIMESTAMP_EXPIRED",
    "UNAVAILABLE:AUTH_IP_BLOCKED",
    "UNAVAILABLE:HTTP_401",
    "UNAVAILABLE:HTTP_403",
    "UNAVAILABLE:PLAN_UPGRADE_REQUIRED",
    "UNAVAILABLE:RATE_LIMIT_EXCEEDED",
    "UNAVAILABLE:RATE_LIMIT_BANNED",
    "UNAVAILABLE:RATE_LIMITED",
    "UNAVAILABLE:RATE_LIMIT_LOCAL_QUEUE",
    "UNAVAILABLE:API_TIMEOUT",
    "UNAVAILABLE:API_UNAVAILABLE",
    "UNAVAILABLE:API_PAYLOAD_INVALID",
    "UNAVAILABLE:API_MINT_MISMATCH",
    "UNAVAILABLE:API_ERROR",
    "UNAVAILABLE:CIRCUIT_OPEN",
    # T21 (G25): the browser bridge, the headless page service and the worker's client.
    "UNAVAILABLE:LOGIN_REQUIRED",
    "UNAVAILABLE:CHALLENGE",
    "UNAVAILABLE:CHALLENGE_TIMEOUT",
    "UNAVAILABLE:CAPTCHA",
    "UNAVAILABLE:ACCESS_BLOCKED",
    "UNAVAILABLE:API_BLOCKED",
    "UNAVAILABLE:NETWORK_ERROR",
    "UNAVAILABLE:PAGE_TIMEOUT",
    "UNAVAILABLE:NO_SIGNALS",
    "UNAVAILABLE:FETCH_PENDING",
    "UNAVAILABLE:QUEUE_FULL",
    "UNAVAILABLE:BROWSER_CLOSED",
    "UNAVAILABLE:BROWSER_ERROR",
    "UNAVAILABLE:BROWSER_UNAVAILABLE",
    "UNAVAILABLE:SERVICE_UNAUTHORIZED",
    "UNAVAILABLE:SERVICE_UNAVAILABLE",
    "UNAVAILABLE:SERVICE_TIMEOUT",
)


def russian(text: str, code: str) -> None:
    assert text != code and CYRILLIC.search(text), f"{code}: {text!r}"


@pytest.mark.parametrize("code", [item.value for item in RejectionCode])
def test_every_rejection_code_has_a_russian_line(code: str) -> None:
    russian(labels.label(labels.REJECTION, code), code)


@pytest.mark.parametrize("code", [item.value for item in DecisionFlag])
def test_every_decision_flag_has_a_russian_line(code: str) -> None:
    (notice,) = flag_notices([code])
    russian(notice.message, code)


@pytest.mark.parametrize("code", WORKER_READINESS)
def test_every_worker_readiness_code_has_a_russian_line(code: str) -> None:
    russian(labels.readiness_label(code), code)


@pytest.mark.parametrize("code", EXECUTOR_CODES)
def test_every_executor_refusal_has_a_russian_line(code: str) -> None:
    russian(labels.executor_label(code), code)


@pytest.mark.parametrize(
    "code",
    [
        "SLIPPAGE_BREACH",
        "PRICE_IMPACT_UNAVAILABLE",
        # ``swap_rejection`` (T17): the blockers of a sale card.
        "PRICE_IMPACT_BREACH",
        "SWAP_QUOTE_INCOMPLETE",
    ],
)
def test_the_impact_blockers_of_the_approval_desk_are_labelled(code: str) -> None:
    russian(labels.readiness_label(code), code)


@pytest.mark.parametrize("code", ALERT_CODES)
def test_every_critical_alert_has_a_russian_line_on_the_panel(code: str) -> None:
    alert = CriticalAlert(
        alert_id=uuid4(), code=code, message="english text of the worker", created_at=NOW
    )

    russian(alert_view(alert).label, code)


@pytest.mark.parametrize("status", [item.value for item in SaleStatus])
def test_every_sale_status_has_a_russian_line(status: str) -> None:
    russian(labels.label(labels.SALE_STATUS, status), status)


@pytest.mark.parametrize("detail", SALE_DETAILS)
def test_every_sale_detail_has_a_russian_line(detail: str) -> None:
    russian(labels.label(labels.SALE_DETAIL, detail), detail)


@pytest.mark.parametrize("kind", [item.value for item in LedgerKind])
def test_every_lifecycle_step_of_a_position_has_a_russian_line(kind: str) -> None:
    # SWAP (T17): the sale of the coin an exit returned, booked with the closing exit.
    russian(labels.label(labels.LEDGER_KIND, kind), kind)


@pytest.mark.parametrize("reason", [item.value for item in PositionReason])
def test_every_position_exit_reason_has_a_russian_line(reason: str) -> None:
    # TAKE_PROFIT and DEV_SOLD (T19) among them.
    russian(labels.label(labels.POSITION_REASON, reason), reason)


@pytest.mark.parametrize(
    ("table", "code"),
    [
        ("COST_MODEL_FLAGS", "PRIORITY_FEE_UNPRICED"),  # F4: a pool not quoted in wSOL
        ("TAKE_PROFIT_STATE", "UNMEASURED"),  # F4: no take profit outside SHADOW
        *(("STRATEGY_FLAG", item.value) for item in StrategyFlag),  # ATH_POOL_ONLY (T19)
    ],
)
def test_the_event_and_plan_codes_of_t19_and_f4_have_russian_lines(table: str, code: str) -> None:
    russian(labels.label(getattr(labels, table), code), code)


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("ath_source", ["GMGN_TOKEN", *(item.value for item in StrategyFlag)]),
        ("pool_pair", [item.value for item in PairShape]),
        ("token_volume_source", [item.value for item in TokenVolumeSource]),
        ("token_volume_rank_source", [item.value for item in TokenVolumeSource]),
        ("market_cap_source", ["GMGN", "DATA_API", "UNKNOWN"]),
        ("developer_status", [*(item.value for item in DeveloperStatus), "UNAVAILABLE"]),
    ],
)
def test_every_code_valued_evidence_row_is_explained(name: str, values: list[str]) -> None:
    for value in values:
        (row,) = evidence_views([EvidenceItem(name=name, value=value)], ())
        assert row.note is not None, (name, value)
        russian(row.note, value)


def test_the_pool_only_token_volume_names_its_real_source() -> None:
    (notice,) = flag_notices(["TOKEN_VOLUME_POOL_ONLY"])

    # GMGN's token volume is what the author reads in Trending; nothing was checked about
    # "every venue", so the line must not claim it.
    assert "по всем площадкам" not in notice.message
    assert "объём токена GMGN, как в Trending" in notice.message


def test_the_unused_impact_blocker_codes_are_gone() -> None:
    assert "PRICE_IMPACT_UNMEASURED" not in labels.APPROVAL_BLOCKERS
    assert "PRICE_IMPACT_ABOVE_LIMIT" not in labels.APPROVAL_BLOCKERS


@pytest.mark.parametrize("code", ADMISSION)
def test_every_admission_reason_and_sizing_cap_has_a_russian_line(code: str) -> None:
    russian(labels.admission_label(code), code)


@pytest.mark.parametrize("detail", DEPTH_DETAILS)
def test_every_depth_detail_is_explained_next_to_its_evidence_row(detail: str) -> None:
    (row,) = evidence_views([EvidenceItem(name="depth_source", value="UNAVAILABLE",
                                          threshold=detail)], ())
    assert row.note is not None
    russian(row.note, detail)


@pytest.mark.parametrize(
    "status", GMGN_STATUSES[GMGN_STATUSES.index("UNAVAILABLE:LOGIN_REQUIRED") :]
)
def test_every_gmgn_bridge_reason_has_its_own_line_not_the_generic_one(status: str) -> None:
    reason = status.partition(":")[2]

    note = labels.gmgn_status_label(status)

    russian(note, status)
    assert f"({reason})" not in note, note


@pytest.mark.parametrize("status", GMGN_STATUSES)
def test_every_gmgn_status_is_explained_next_to_its_evidence_row(status: str) -> None:
    (row,) = evidence_views([EvidenceItem(name="gmgn_status", value=status)], ())
    assert row.note is not None
    russian(row.note, status)
    reason = status.partition(":")[2]
    if reason:
        # A T16 reason gets its own line, not only the generic "unavailable".
        assert row.note != labels.gmgn_status_label("UNAVAILABLE:SOMETHING_NEW")


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("insiders_share", "INSIDERS_SHARE_HIGH"),
        ("bundling_share", "BUNDLING_SHARE_HIGH"),
        ("phishing_share", "PHISHING_SHARE_HIGH"),
        ("gmgn_top10_holder_share", "GMGN_TOP10_HOLDER_SHARE_HIGH"),
    ],
)
def test_a_gmgn_row_is_marked_when_its_own_code_fired(name: str, code: str) -> None:
    rows = evidence_views(
        [
            EvidenceItem(name="insiders_share", value="0.2", threshold="0.10"),
            EvidenceItem(name="bundling_share", value="0.8", threshold="0.70"),
            EvidenceItem(name="phishing_share", value="0.4", threshold="0.30"),
            EvidenceItem(name="gmgn_top10_holder_share", value="0.5", threshold="0.30"),
        ],
        (code,),
    )

    assert {row.name for row in rows if row.triggered} == {name}
