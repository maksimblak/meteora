"""F3 (C4, spec 9): every code the operator can meet in the panel has a Russian line."""

import re

import pytest

from src.api import labels
from src.api.presenters import evidence_views, flag_notices
from src.domain.risk.models import RiskReason
from src.domain.screening.models import DecisionFlag, EvidenceItem, RejectionCode

CYRILLIC = re.compile(r"[А-Яа-яЁё]")

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


@pytest.mark.parametrize("code", ["SLIPPAGE_BREACH", "PRICE_IMPACT_UNAVAILABLE"])
def test_the_entry_impact_blockers_of_the_approval_desk_are_labelled(code: str) -> None:
    russian(labels.readiness_label(code), code)


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
