"""The free token sources of a decision (T22): Jupiter Tokens API v2 and RugCheck.

Both are optional, like GMGN: a source that is off, failed or does not know the token
changes no status by itself - the decision shows what was read and the operator sees a
flag where a check is left to them. What a fresh reading decides:

* Jupiter - the token's volume, market cap and token age where GMGN has no fresh figure
  (``screener._token_volume`` and the guide thresholds); the 5-minute trades and traders
  for the artificial-volume check (``assess_volume_integrity``); Jupiter's ``low`` organic
  grade as suspected artificial volume; mint and freeze authority the other sources left
  unknown (``merge_token_sources``); the developer's share where GMGN reports none;
* RugCheck - a ``danger`` risk rejects (``RUGCHECK_DANGER``) in every mode.
"""

from src.domain.models.trading import TradingMode
from src.domain.screening.models import DecisionFlag, EvidenceItem, RejectionCode
from src.market.models import (
    AuthorityStatus,
    DataQuality,
    Freshness,
    JupiterTokenReading,
    RugCheckReading,
    TokenRiskSnapshot,
)

UNAVAILABLE = "UNAVAILABLE"
# Neither source was asked for the token: off, beyond the cycle's limit, or not a deep
# candidate (RugCheck is asked only for those).
NOT_READ = "NOT_READ"


def fresh_jupiter(token: TokenRiskSnapshot) -> JupiterTokenReading | None:
    reading = token.jupiter
    return reading if reading is not None and reading.freshness is Freshness.FRESH else None


def merge_token_sources(
    token: TokenRiskSnapshot,
    *,
    jupiter: JupiterTokenReading | None = None,
    rugcheck: RugCheckReading | None = None,
) -> TokenRiskSnapshot:
    """The token's risk with the free sources' readings attached as received.

    A fresh Jupiter reading fills the mint and freeze authority the other sources left
    unknown; where both report, the more dangerous reading wins - as the Data API's own
    flags do (``merge_pool_token_signals``). A reading of another mint is not attached.
    ``ingested_at`` moves up to the readings' source time, so no input is newer than it.
    """

    if jupiter is not None and jupiter.mint != token.mint:
        jupiter = None
    if rugcheck is not None and rugcheck.mint != token.mint:
        rugcheck = None
    attached = [reading for reading in (jupiter, rugcheck) if reading is not None]
    if not attached:
        return token
    data = token.model_dump()
    data.update(
        provider="+".join((token.provider, *(reading.provider for reading in attached))),
        ingested_at=max(token.ingested_at, *(reading.source_at for reading in attached)),
    )
    if rugcheck is not None:
        data["rugcheck"] = rugcheck
    if jupiter is not None:
        data["jupiter"] = jupiter
        if jupiter.freshness is Freshness.FRESH:
            mint = _worse(token.mint_authority, jupiter.mint_authority)
            freeze = _worse(token.freeze_authority, jupiter.freeze_authority)
            resolved = {
                name
                for name, value in (("mint_authority", mint), ("freeze_authority", freeze))
                if value is not AuthorityStatus.UNKNOWN
            }
            missing = tuple(name for name in token.missing_signals if name not in resolved)
            quality = token.data_quality
            if quality is DataQuality.PARTIAL and not missing:
                quality = DataQuality.COMPLETE
            data.update(
                mint_authority=mint,
                freeze_authority=freeze,
                missing_signals=missing,
                data_quality=quality,
            )
    return TokenRiskSnapshot.model_validate(data)


def _worse(current: AuthorityStatus, reported: AuthorityStatus) -> AuthorityStatus:
    if AuthorityStatus.PRESENT in (current, reported):
        return AuthorityStatus.PRESENT
    if AuthorityStatus.REVOKED in (current, reported):
        return AuthorityStatus.REVOKED
    return AuthorityStatus.UNKNOWN


def assess_token_sources(
    token: TokenRiskSnapshot, mode: TradingMode, *, gmgn_developer_known: bool
) -> tuple[list[RejectionCode], list[EvidenceItem], list[DecisionFlag]]:
    """Reasons, evidence and flags of the free sources (T22).

    ``gmgn_developer_known``: a fresh GMGN reading reported the developer's share, which
    then decides ``DEVELOPER_HOLDS_TOKENS`` alone; otherwise Jupiter's share does.
    """

    reasons: list[RejectionCode] = []
    flags: list[DecisionFlag] = []
    evidence = _jupiter_evidence(token.jupiter)
    jupiter = fresh_jupiter(token)
    developer = jupiter.developer_share if jupiter is not None else None
    if not gmgn_developer_known and developer is not None and developer > 0:
        flags.append(DecisionFlag.DEVELOPER_HOLDS_TOKENS)
        if mode is TradingMode.LIVE:
            # As with GMGN's share (G24): LIVE has no operator to check the supply.
            reasons.append(RejectionCode.DEVELOPER_HOLDS_TOKENS)

    report = token.rugcheck
    if report is None:
        evidence.append(EvidenceItem(name="rugcheck_status", value=NOT_READ))
        return reasons, evidence, flags
    fresh = report.freshness is Freshness.FRESH
    if not fresh:
        flags.append(DecisionFlag.RUGCHECK_UNAVAILABLE)
    elif report.danger:
        reasons.append(RejectionCode.RUGCHECK_DANGER)
    evidence.extend(
        (
            EvidenceItem(name="rugcheck_status", value=_status(report.freshness, report.detail)),
            EvidenceItem(
                name="rugcheck_danger",
                value=("; ".join(report.danger) or "none") if fresh else UNAVAILABLE,
                threshold="none",
            ),
            EvidenceItem(
                name="rugcheck_warnings",
                value=("; ".join(report.warnings) or "none") if fresh else UNAVAILABLE,
            ),
            EvidenceItem(
                name="rugcheck_score",
                value=UNAVAILABLE if not fresh or report.score is None else str(report.score),
            ),
            EvidenceItem(
                name="rugcheck_lp_locked_share",
                value=UNAVAILABLE
                if not fresh or report.lp_locked_share is None
                else str(report.lp_locked_share),
            ),
        )
    )
    return reasons, evidence, flags


def _jupiter_evidence(reading: JupiterTokenReading | None) -> list[EvidenceItem]:
    if reading is None:
        return [EvidenceItem(name="jupiter_status", value=NOT_READ)]
    fresh = reading.freshness is Freshness.FRESH

    def value(item: object) -> str:
        return UNAVAILABLE if not fresh or item is None else str(item)

    first_pool = reading.first_pool_at
    return [
        EvidenceItem(name="jupiter_status", value=_status(reading.freshness, reading.detail)),
        EvidenceItem(
            name="jupiter_source_at",
            value=reading.source_at.isoformat() if fresh else UNAVAILABLE,
        ),
        EvidenceItem(name="jupiter_trades_5m", value=value(reading.trades_5m)),
        EvidenceItem(name="jupiter_traders_5m", value=value(reading.traders_5m)),
        EvidenceItem(name="jupiter_top_holders_share", value=value(reading.top_holders_share)),
        EvidenceItem(
            name="jupiter_developer_share", value=value(reading.developer_share), threshold="0"
        ),
        EvidenceItem(name="jupiter_holder_count", value=value(reading.holder_count)),
        EvidenceItem(
            name="jupiter_first_pool_at",
            value=value(None if first_pool is None else first_pool.isoformat()),
        ),
    ]


def _status(freshness: Freshness, detail: str | None) -> str:
    if freshness is Freshness.FRESH:
        return "FRESH"
    return f"{UNAVAILABLE}:{detail}" if detail else Freshness.STALE.value
