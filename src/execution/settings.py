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
