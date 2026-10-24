"""Environment of the Python side of the execution gateway (names only in ``.env.example``).

Secrets come from ``NAME`` or ``NAME_FILE``, never both, and never appear in errors.
The LIVE promotion evidence is read from the same file as the TS executor's
``LIVE_PROMOTION_FILE``, so both sides gate LIVE on one operator-produced record.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from pydantic import SecretStr, ValidationError

from src.execution.contracts import LivePromotionEvidence

EXECUTOR_URL = "EXECUTOR_URL"
EXECUTOR_TIMEOUT_SECONDS = "EXECUTOR_TIMEOUT_SECONDS"
LIVE_PROMOTION_FILE = "LIVE_PROMOTION_FILE"

_PROMOTION_FIELDS = {
    "promotionId": "promotion_id",
    "rulesetVersion": "ruleset_version",
    "approvalCycles": "approval_cycles",
    "receiptsReconciled": "receipts_reconciled",
    "riskInvariantsPreserved": "risk_invariants_preserved",
    "dedicatedRpcReady": "dedicated_rpc_ready",
    "walletReady": "wallet_ready",
    "checklistCompleted": "checklist_completed",
    "operatorConfirmationId": "operator_confirmation_id",
    "promotedAt": "promoted_at",
    "expiresAt": "expires_at",
}


@dataclass(frozen=True, slots=True)
class ExecutorSettings:
    url: str | None
    mode_token: SecretStr | None
    approval_secret: SecretStr | None
    timeout: timedelta
    live_promotion_file: Path | None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "ExecutorSettings":
        raw_timeout = environ.get(EXECUTOR_TIMEOUT_SECONDS)
        seconds = int(raw_timeout) if raw_timeout else 30
        if seconds <= 0:
            raise ValueError(f"{EXECUTOR_TIMEOUT_SECONDS} must be a positive number of seconds")
        promotion = environ.get(LIVE_PROMOTION_FILE)
        return cls(
            url=environ.get(EXECUTOR_URL) or None,
            mode_token=_secret(environ, "EXECUTOR_MODE_TOKEN"),
            approval_secret=_secret(environ, "EXECUTOR_APPROVAL_SECRET"),
            timeout=timedelta(seconds=seconds),
            live_promotion_file=Path(promotion) if promotion else None,
        )

    def live_promotion(self) -> LivePromotionEvidence | None:
        """The operator's LIVE promotion record; anything unreadable gates LIVE shut."""

        if self.live_promotion_file is None:
            return None
        try:
            raw = json.loads(self.live_promotion_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return LivePromotionEvidence.model_validate(
                {field: raw.get(key) for key, field in _PROMOTION_FIELDS.items()}
            )
        except ValidationError:
            return None


SWAP_AFTER_EXIT = "SWAP_AFTER_EXIT"
SWAP_MAX_PRICE_IMPACT_BPS = "SWAP_MAX_PRICE_IMPACT_BPS"
SWAP_SLIPPAGE_BPS = "SWAP_SLIPPAGE_BPS"
SWAP_AFTER_EXIT_MAX_SPLITS = "SWAP_AFTER_EXIT_MAX_SPLITS"
SWAP_AFTER_EXIT_ATTEMPTS = "SWAP_AFTER_EXIT_ATTEMPTS"
SWAP_SETTLEMENT_TIMEOUT_SECONDS = "SWAP_SETTLEMENT_TIMEOUT_SECONDS"
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class SwapSettings:
    """The sale of the base token an exit returned (G22), one swap command per part.

    ``enabled`` (``SWAP_AFTER_EXIT``, on by default): sell after every confirmed
    REMOVE/CLOSE. ``max_price_impact_bps``: the impact limit of one sale command; above it
    the amount is halved, at most ``max_splits`` times (the smallest part is
    ``1 / 2**max_splits`` of what the exit returned) - if even that part is over the limit
    the token stays and a critical alert asks the operator. ``slippage_bps``: the on-chain
    minimum output below the quote. ``attempts``: failed parts before the sale gives up
    (alert). ``settlement_timeout``: how long to wait for the chain to tell how much the
    exit returned before the token is reported unsold. The 3% impact, 1% slippage and
    the counts are the build's starting values, not the guide's.
    """

    enabled: bool = True
    max_price_impact_bps: int = 300
    slippage_bps: int = 100
    max_splits: int = 4
    attempts: int = 5
    settlement_timeout: timedelta = timedelta(minutes=10)

    def __post_init__(self) -> None:
        if not 0 <= self.max_price_impact_bps <= 10_000:
            raise ValueError(f"{SWAP_MAX_PRICE_IMPACT_BPS} must be in [0, 10000]")
        if not 0 <= self.slippage_bps < 10_000:
            raise ValueError(f"{SWAP_SLIPPAGE_BPS} must be in [0, 10000)")
        if not 0 <= self.max_splits <= 10:
            raise ValueError(f"{SWAP_AFTER_EXIT_MAX_SPLITS} must be in [0, 10]")
        if not 1 <= self.attempts <= 20:
            raise ValueError(f"{SWAP_AFTER_EXIT_ATTEMPTS} must be in [1, 20]")
        if self.settlement_timeout <= timedelta(0):
            raise ValueError(f"{SWAP_SETTLEMENT_TIMEOUT_SECONDS} must be positive")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "SwapSettings":
        """Empty or absent means the default; anything unreadable raises ``ValueError``."""

        default = cls()
        raw_flag = (environ.get(SWAP_AFTER_EXIT) or "").strip().lower()
        if raw_flag and raw_flag not in _TRUE | _FALSE:
            raise ValueError(f"{SWAP_AFTER_EXIT} must be true or false")
        return cls(
            enabled=default.enabled if not raw_flag else raw_flag in _TRUE,
            max_price_impact_bps=_whole(
                environ, SWAP_MAX_PRICE_IMPACT_BPS, default.max_price_impact_bps
            ),
            slippage_bps=_whole(environ, SWAP_SLIPPAGE_BPS, default.slippage_bps),
            max_splits=_whole(environ, SWAP_AFTER_EXIT_MAX_SPLITS, default.max_splits),
            attempts=_whole(environ, SWAP_AFTER_EXIT_ATTEMPTS, default.attempts),
            settlement_timeout=timedelta(
                seconds=_whole(
                    environ,
                    SWAP_SETTLEMENT_TIMEOUT_SECONDS,
                    int(default.settlement_timeout.total_seconds()),
                )
            ),
        )


def _whole(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise ValueError(f"{name} must be a whole number")
    return int(raw)


def _secret(environ: Mapping[str, str], name: str) -> SecretStr | None:
    direct = environ.get(name)
    file_name = environ.get(f"{name}_FILE")
    if direct and file_name:
        raise ValueError(f"set either {name} or {name}_FILE, not both")
    if direct:
        return SecretStr(direct)
    if file_name:
        value = Path(file_name).read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"{name}_FILE contains an empty secret")
        return SecretStr(value)
    return None
