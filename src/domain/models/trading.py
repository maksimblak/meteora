"""Trading-mode state restored conservatively after every restart."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class TradingMode(StrEnum):
    SHADOW = "SHADOW"
    APPROVAL = "APPROVAL"
    LIVE = "LIVE"


class PersistedModeAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: TradingMode
    promotion_authorized: bool = False


def restore_trading_mode(state: PersistedModeAuthorization | None) -> TradingMode:
    """Fail closed unless a persisted promotion record explicitly authorizes the mode."""

    if state is None or not state.promotion_authorized:
        return TradingMode.SHADOW
    return state.mode
