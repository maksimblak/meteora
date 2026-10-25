"""Immutable screening inputs, policy, and audit-ready decisions."""

import re
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.domain.features import VolumeSpikeFeatures
from src.domain.models.primitives import PublicKey
from src.domain.models.trading import TradingMode


def _exact_decimal(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("screening decimals must not use binary float")
    return value


class CandidateStatus(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"
    WARMING_UP = "WARMING_UP"


class IntegrityStatus(StrEnum):
    CLEAR = "CLEAR"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


class RejectionCode(StrEnum):
    INPUT_IDENTITY_MISMATCH = "INPUT_IDENTITY_MISMATCH"
    SNAPSHOT_ALIGNMENT_MISMATCH = "SNAPSHOT_ALIGNMENT_MISMATCH"
    FUTURE_MARKET_DATA = "FUTURE_MARKET_DATA"
    FUTURE_TOKEN_RISK_DATA = "FUTURE_TOKEN_RISK_DATA"  # noqa: S105
    FUTURE_FEATURE_DATA = "FUTURE_FEATURE_DATA"
    LOW_TVL = "LOW_TVL"
    LOW_DEPTH = "LOW_DEPTH"
    POOL_TOO_YOUNG = "POOL_TOO_YOUNG"
    HISTORY_WARMING_UP = "HISTORY_WARMING_UP"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    INCOMPLETE_MARKET_DATA = "INCOMPLETE_MARKET_DATA"
    MINT_AUTHORITY_PRESENT = "MINT_AUTHORITY_PRESENT"
    FREEZE_AUTHORITY_PRESENT = "FREEZE_AUTHORITY_PRESENT"
    BLACKLISTED_TOKEN = "BLACKLISTED_TOKEN"  # noqa: S105 - stable domain code
    UNVERIFIED_TOKEN = "UNVERIFIED_TOKEN"  # noqa: S105 - stable domain code
    HOLDER_CONCENTRATION_HIGH = "HOLDER_CONCENTRATION_HIGH"
    TOKEN_RISK_STALE = "TOKEN_RISK_STALE"  # noqa: S105 - stable domain code
    TOKEN_RISK_UNKNOWN = "TOKEN_RISK_UNKNOWN"  # noqa: S105 - stable domain code
    ARTIFICIAL_VOLUME_CONFIRMED = "ARTIFICIAL_VOLUME_CONFIRMED"
    VOLUME_INTEGRITY_UNKNOWN = "VOLUME_INTEGRITY_UNKNOWN"
    PRICE_IMPACT_HIGH = "PRICE_IMPACT_HIGH"
    VOLUME_SIGNAL_BELOW_THRESHOLD = "VOLUME_SIGNAL_BELOW_THRESHOLD"
    SCORE_BELOW_THRESHOLD = "SCORE_BELOW_THRESHOLD"
    # guide-v1 (spec 6.6)
    MARKET_CAP_BELOW_THRESHOLD = "MARKET_CAP_BELOW_THRESHOLD"
    TOKEN_TOO_YOUNG = "TOKEN_TOO_YOUNG"  # noqa: S105 - stable domain code
    VOLUME_PER_MINUTE_BELOW_THRESHOLD = "VOLUME_PER_MINUTE_BELOW_THRESHOLD"
    VOLUME_TO_LIQUIDITY_BELOW_THRESHOLD = "VOLUME_TO_LIQUIDITY_BELOW_THRESHOLD"
    ARTIFICIAL_VOLUME_SUSPECTED = "ARTIFICIAL_VOLUME_SUSPECTED"
    TOTAL_FEES_BELOW_THRESHOLD = "TOTAL_FEES_BELOW_THRESHOLD"
    GUIDE_SIGNAL_UNKNOWN = "GUIDE_SIGNAL_UNKNOWN"
    # live data (D07)
    BLACKLISTED_POOL = "BLACKLISTED_POOL"
    DEPTH_UNAVAILABLE = "DEPTH_UNAVAILABLE"
    VOLUME_PARTICIPANTS_UNAVAILABLE = "VOLUME_PARTICIPANTS_UNAVAILABLE"
    # GMGN holder structure (spec 6.7, G18)
    INSIDERS_SHARE_HIGH = "INSIDERS_SHARE_HIGH"
    BUNDLING_SHARE_HIGH = "BUNDLING_SHARE_HIGH"
    PHISHING_SHARE_HIGH = "PHISHING_SHARE_HIGH"
    GMGN_TOP10_HOLDER_SHARE_HIGH = "GMGN_TOP10_HOLDER_SHARE_HIGH"
    GMGN_UNAVAILABLE = "GMGN_UNAVAILABLE"
    # D09: no on-chain source configured; APPROVAL and LIVE only (SHADOW has the flag).
    TOKEN_RISK_UNAVAILABLE = "TOKEN_RISK_UNAVAILABLE"  # noqa: S105 - stable domain code
    # Selection as the author does it (G24, T18).
    # The token's average volume per minute over the last hour is below the guide minimum.
    HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD = "HOURLY_VOLUME_PER_MINUTE_BELOW_THRESHOLD"
    # guide-v1 fee opportunity (30m LP fees / TVL) until 2026-10-25 (ADR 0016): no longer
    # given, kept for the decisions recorded with it.
    FEE_TVL_BELOW_THRESHOLD = "FEE_TVL_BELOW_THRESHOLD"
    # The token's name or ticker names an excluded theme (Elon, Trump, politics, streams).
    THEME_EXCLUDED = "THEME_EXCLUDED"
    # An open position or a PENDING/EXECUTING plan already holds this base token.
    TOKEN_ALREADY_HELD = "TOKEN_ALREADY_HELD"  # noqa: S105 - stable domain code
    # LIVE only: the developer still holds tokens (no operator checklist in LIVE).
    DEVELOPER_HOLDS_TOKENS = "DEVELOPER_HOLDS_TOKENS"  # noqa: S105 - stable domain code
    # A candidate whose token ranked below the cycle's deep-check limit by hourly volume.
    CANDIDATE_RANK_BELOW_LIMIT = "CANDIDATE_RANK_BELOW_LIMIT"
    # The pair (``pairs.pair_token``, F5): prices, bins and quotes are taken from token_x,
    # so guide-v1 enters only a pool of the coin first and a quote token second.
    TOKEN_SECOND_IN_PAIR = "TOKEN_SECOND_IN_PAIR"  # noqa: S105 - stable domain code
    PAIR_OF_QUOTE_TOKENS = "PAIR_OF_QUOTE_TOKENS"  # noqa: S105 - stable domain code
    PAIR_WITHOUT_QUOTE_TOKEN = "PAIR_WITHOUT_QUOTE_TOKEN"  # noqa: S105 - stable domain code
    # The strategy found no plan for a screened candidate (spec 6.8, F5): the candidate's
    # outcome, recorded with ``refuse_candidate``; same names as ``StrategyRejection``.
    NOT_NEAR_LOCAL_HIGH = "NOT_NEAR_LOCAL_HIGH"
    RANGE_TOO_NARROW = "RANGE_TOO_NARROW"
    # T22: RugCheck rates at least one risk of the token ``danger`` (a whale holder, mint
    # authority, unlocked LP), in every mode.
    RUGCHECK_DANGER = "RUGCHECK_DANGER"
    # After an exit from the token the author waits for new volumes or a new high
    # (``ReentryMark``); neither has come yet.
    NO_NEW_VOLUME_OR_HIGH = "NO_NEW_VOLUME_OR_HIGH"


class DecisionFlag(StrEnum):
    """Facts the operator must see. A flag never blocks SHADOW; where a stage cannot accept
    it (see each flag), the screener adds the reason of the same name instead."""

    # D03: GMGN-style total fees have no Data API/RPC source; the conditional threshold is
    # skipped and the approval preview asks for a manual check instead.
    TOTAL_FEES_UNAVAILABLE = "TOTAL_FEES_UNAVAILABLE"
    # D07: no quote measured depth and price impact. SHADOW goes on with a conservative
    # slippage model; APPROVAL and LIVE are closed (reason ``DEPTH_UNAVAILABLE``).
    DEPTH_UNAVAILABLE = "DEPTH_UNAVAILABLE"
    # D07: no transaction or participant counts (none from the Data API, and no fresh
    # Jupiter reading of the token's 5m trades and traders, T22). Artificial volume is
    # judged on turnover, fee organicity and burst self-similarity; SHADOW and APPROVAL go
    # on, LIVE is closed (reason ``VOLUME_PARTICIPANTS_UNAVAILABLE``).
    VOLUME_PARTICIPANTS_UNAVAILABLE = "VOLUME_PARTICIPANTS_UNAVAILABLE"
    # G18: no fresh GMGN reading of every threshold metric. SHADOW and APPROVAL go on (the
    # approval preview asks for a manual check); LIVE is closed (reason ``GMGN_UNAVAILABLE``)
    # unless ``GmgnScreeningRules.required_for_live`` was switched off.
    GMGN_UNAVAILABLE = "GMGN_UNAVAILABLE"
    # G18: GMGN reports a developer share above zero ("желательно 0" in the guide): shown and
    # recorded for the operator's checklist in SHADOW and APPROVAL; LIVE, where no operator
    # checks it, is closed (reason ``DEVELOPER_HOLDS_TOKENS``, G24).
    DEVELOPER_HOLDS_TOKENS = "DEVELOPER_HOLDS_TOKENS"  # noqa: S105 - stable domain code
    # D09: no on-chain source (RPC) is configured, every base token signal is unknown.
    # SHADOW goes on; APPROVAL and LIVE are closed (reason ``TOKEN_RISK_UNAVAILABLE``).
    TOKEN_RISK_UNAVAILABLE = "TOKEN_RISK_UNAVAILABLE"  # noqa: S105 - stable domain code
    # G24: the 5m window spiked (ratio, robust z and 30m confirmation): a score bonus.
    VOLUME_SPIKE = "VOLUME_SPIKE"
    # G24: token volume is the sum of the token's DLMM pools, not GMGN's token volume as in
    # Trending (no fresh GMGN reading, and no fresh Jupiter reading either, T22).
    TOKEN_VOLUME_POOL_ONLY = "TOKEN_VOLUME_POOL_ONLY"  # noqa: S105 - stable domain code
    # GMGN counts more launch snipers than the guide-v1 maximum ("большие - ждать").
    SNIPERS_HIGH = "SNIPERS_HIGH"
    # The token is at least ``revived_token_age`` old: a revival whose volume tends to fade.
    TOKEN_REVIVED = "TOKEN_REVIVED"  # noqa: S105 - stable domain code
    # The source reports neither the token's name nor its ticker, so the theme filter could
    # not run. SHADOW goes on; APPROVAL and LIVE are closed (missing signal ``theme``).
    THEME_UNKNOWN = "THEME_UNKNOWN"
    # T22: RugCheck was asked for the token but gave no fresh report; an optional source, so
    # nothing is closed - the operator checks the token on RugCheck by hand.
    RUGCHECK_UNAVAILABLE = "RUGCHECK_UNAVAILABLE"


class VolumeIntegrityMode(StrEnum):
    """Which signals decide artificial volume (G07)."""

    # shadow-hypothesis-v1: transaction and participant counts are mandatory.
    PARTICIPANTS = "PARTICIPANTS"
    # Live data (D07): turnover, fee organicity and self-similar bursts are mandatory;
    # participant counts are used when present, otherwise flagged.
    AVAILABLE_SIGNALS = "AVAILABLE_SIGNALS"


class DepthSource(StrEnum):
    SNAPSHOT = "SNAPSHOT"
    QUOTE = "QUOTE"
    UNAVAILABLE = "UNAVAILABLE"


class DepthEvidence(BaseModel):
    """Depth and price impact at the reference size, as measured for the decision (D07).

    ``QUOTE``: the executor's read-only quote; no ``depth`` means the pool could not fill
    the reference size. ``UNAVAILABLE``: nothing was measured, ``detail`` says why.

    ``depth`` and ``reference_notional`` are USD, the unit of ``minimum_depth`` and the TVL;
    ``quote_price_usd`` is the quote token's USD price that turned the reference size into
    the quote-token amount the executor quoted (A3).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    source: DepthSource
    depth: Decimal | None = None
    price_impact: Decimal | None = None
    reference_notional: Decimal | None = None
    detail: str | None = None
    measured_at: datetime | None = None
    quote_price_usd: Decimal | None = None

    _reject_float = field_validator(
        "depth", "price_impact", "reference_notional", "quote_price_usd", mode="before"
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_depth(self) -> "DepthEvidence":
        if any(
            value is not None and value < 0
            for value in (
                self.depth,
                self.price_impact,
                self.reference_notional,
                self.quote_price_usd,
            )
        ):
            raise ValueError("depth evidence cannot be negative")
        if self.price_impact is not None and self.price_impact > 1:
            raise ValueError("price impact is a fraction in [0, 1]")
        if self.source is DepthSource.UNAVAILABLE and (
            self.depth is not None or self.price_impact is not None
        ):
            raise ValueError("unavailable depth carries no measured value")
        if self.measured_at is not None and (
            self.measured_at.tzinfo is None or self.measured_at.utcoffset() is None
        ):
            raise ValueError("measured_at must be timezone-aware")
        return self


class VolumeRegime(StrEnum):
    BELOW = "BELOW"
    WORKING = "WORKING"
    OVERVOLUME = "OVERVOLUME"
    UNKNOWN = "UNKNOWN"


class DecisionContext(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    mode: TradingMode
    ruleset_version: str
    config_version: str
    correlation_id: str

    @model_validator(mode="after")
    def validate_context(self) -> "DecisionContext":
        if not all((self.ruleset_version, self.config_version, self.correlation_id)):
            raise ValueError("decision versions and correlation_id are required")
        return self


class EvidenceItem(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    name: str
    value: str
    threshold: str | None = None


class ScoreComponent(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    name: str
    normalized_value: Decimal
    weight: Decimal
    contribution: Decimal

    _reject_float = field_validator("normalized_value", "weight", "contribution", mode="before")(
        _exact_decimal
    )


class ScoreWeights(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    volume_acceleration: Decimal = Decimal("0.25")
    robust_volume: Decimal = Decimal("0.20")
    long_confirmation: Decimal = Decimal("0.15")
    fee_opportunity: Decimal = Decimal("0.15")
    liquidity_stability: Decimal = Decimal("0.10")
    volatility_fit: Decimal = Decimal("0.10")
    token_quality: Decimal = Decimal("0.05")

    _reject_float = field_validator("*", mode="before")(_exact_decimal)

    @model_validator(mode="after")
    def validate_weights(self) -> "ScoreWeights":
        values = tuple(self.model_dump().values())
        if any(value < 0 for value in values):
            raise ValueError("score weights cannot be negative")
        if sum(values, Decimal(0)) != Decimal(1):
            raise ValueError("score weights must sum exactly to one")
        return self


class GmgnScreeningRules(BaseModel):
    """GMGN thresholds of ``guide-v1`` (spec 6.7, G18), from the article's checklist.

    The guide asks for insiders < 10%, bundling < 70%, phishing < 30% and top-10 < 30%, so
    a share equal to its maximum already rejects. The developer share is a flag. Only a
    fresh reading is applied; without one the decision carries ``GMGN_UNAVAILABLE`` and
    LIVE is closed while ``required_for_live`` holds (``GMGN_REQUIRED_FOR_LIVE``).
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    maximum_insiders_share: Decimal = Decimal("0.10")
    maximum_bundling_share: Decimal = Decimal("0.70")
    maximum_phishing_share: Decimal = Decimal("0.30")
    maximum_top10_holder_share: Decimal = Decimal("0.30")
    required_for_live: bool = True
    # More launch snipers than this is the ``SNIPERS_HIGH`` flag (never a reject): the
    # gmgn-token SKILL.md scores over 20 snipers as a danger signal; video 2, 17:35 - wait.
    maximum_sniper_count: int = 20

    _reject_float = field_validator(
        "maximum_insiders_share",
        "maximum_bundling_share",
        "maximum_phishing_share",
        "maximum_top10_holder_share",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_gmgn(self) -> "GmgnScreeningRules":
        if self.maximum_sniper_count < 0:
            raise ValueError("maximum_sniper_count must not be negative")
        if not all(
            Decimal(0) < value <= Decimal(1)
            for value in (
                self.maximum_insiders_share,
                self.maximum_bundling_share,
                self.maximum_phishing_share,
                self.maximum_top10_holder_share,
            )
        ):
            raise ValueError("GMGN share thresholds must be fractions in (0, 1]")
        return self


# The author skips coins about Elon and Trump "in 99% of cases" (guide, video 2 19:05) and
# tries to skip political and stream coins (guide, "щитки"). Matched against the words of the
# token's name and ticker, case-insensitively, in Latin and Cyrillic (F5): a word is the
# theme word itself, or the theme word glued to meme affixes and other theme words
# (ELONDOGE, BABYTRUMP, ELONMUSK); a word ending with ``*`` is a stem ("politic*" -
# Political, Politics). A word merely containing one (WATERMELON, Trumpet, MAGAZINE,
# Маска, Mainstream) is an ordinary coin. The list is a guide-v1 setting.
DEFAULT_THEME_WORDS: tuple[str, ...] = (
    "elon",
    "musk",
    "илон",
    "маск",
    "trump",
    "трамп",
    "maga",
    "melania",
    "меланья",
    "мелания",
    "biden",
    "байден",
    "kamala",
    "камала",
    "obama",
    "обама",
    "putin",
    "путин",
    "zelensk*",
    "зеленск*",
    "politic*",
    "политик*",
    "president*",
    "президент*",
    "election*",
    "выборы",
    "stream*",
    "стрим*",
    "twitch",
    "твич",
)
# Meme words glued to a theme word in a ticker (BABYTRUMP, ELONDOGE, MAGAHAT). None is a
# piece of an ordinary word around a theme word (no "s", "et", "zine", "а", "up", "main").
DEFAULT_THEME_AFFIXES: tuple[str, ...] = (
    "baby",
    "mini",
    "lil",
    "little",
    "big",
    "fat",
    "based",
    "super",
    "mega",
    "ultra",
    "king",
    "queen",
    "lord",
    "sir",
    "mister",
    "mr",
    "dr",
    "dark",
    "real",
    "official",
    "pepe",
    "doge",
    "shib",
    "inu",
    "wif",
    "hat",
    "cat",
    "dog",
    "frog",
    "bonk",
    "coin",
    "token",
    "meme",
    "ai",
    "gpt",
    "moon",
    "sol",
    "fan",
    "fans",
    "army",
    "classic",
    "бэби",
    "беби",
    "мини",
    "супер",
    "мега",
    "кинг",
    "кот",
    "пес",
    "доге",
    "коин",
    "токен",
    "мем",
)
_LETTERS_ONLY = re.compile(r"[^\W\d_]+")


class GuideScoreWeights(BaseModel):
    """guide-v1 score (G24): sustained volume carries it, a spike only adds a bonus."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    sustained_volume: Decimal = Decimal("0.40")
    volume_spike: Decimal = Decimal("0.15")
    fee_opportunity: Decimal = Decimal("0.15")
    liquidity_stability: Decimal = Decimal("0.10")
    volatility_fit: Decimal = Decimal("0.10")
    token_quality: Decimal = Decimal("0.10")

    _reject_float = field_validator("*", mode="before")(_exact_decimal)

    @model_validator(mode="after")
    def validate_weights(self) -> "GuideScoreWeights":
        values = tuple(self.model_dump().values())
        if any(value < 0 for value in values):
            raise ValueError("score weights cannot be negative")
        if sum(values, Decimal(0)) != Decimal(1):
            raise ValueError("score weights must sum exactly to one")
        return self


class GuideScreeningRules(BaseModel):
    """Starting thresholds of the LexDollar guide (spec 6.6, G16) — a shadow hypothesis.

    Top-10 holder share uses ``ScreeningPolicy.maximum_holder_concentration``.

    Selection as the author does it (spec 6.8, G24): the token's volume must be sustained -
    the hour's average per minute (``minimum_hourly_volume_per_minute``) and the current 5m
    rate (``minimum_volume_per_minute``) both clear the bar; a spike is a score bonus. The
    score normalizes the hourly rate by ``target_volume_per_minute`` (the author's "20 000 a
    minute is a good figure"). Artificial volume (Savash) is total fees below
    ``minimum_total_fees_sol x market cap / total_fees_reference_market_cap`` while the pool
    turned over at least ``maximum_hourly_turnover`` times its TVL in the hour.
    ``minimum_fee_efficiency`` is kept only so stored records still load: fees over
    ``volume x base fee`` is at least one by construction in DLMM and no longer screened.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    minimum_market_cap: Decimal = Decimal("500000")
    preferred_market_cap: Decimal = Decimal("1000000")
    minimum_token_age: timedelta = timedelta(hours=2)
    minimum_volume_per_minute: Decimal = Decimal("14000")
    overvolume_volume_per_minute: Decimal = Decimal("100000")
    minimum_volume_to_liquidity: Decimal = Decimal("0.5")
    minimum_fee_efficiency: Decimal = Decimal("0.5")
    minimum_total_fees_sol: Decimal = Decimal("30")
    # GMGN holder structure (G18); ``None`` = the bundle has no GMGN rules at all.
    gmgn: GmgnScreeningRules | None = None
    minimum_hourly_volume_per_minute: Decimal = Decimal("14000")
    target_volume_per_minute: Decimal = Decimal("20000")
    total_fees_reference_market_cap: Decimal = Decimal("500000")
    maximum_hourly_turnover: Decimal = Decimal("10")
    excluded_theme_words: tuple[str, ...] = DEFAULT_THEME_WORDS
    revived_token_age: timedelta = timedelta(days=60)
    weights: GuideScoreWeights = GuideScoreWeights()
    # Meme words a theme word may be glued to in one word (see ``DEFAULT_THEME_WORDS``).
    theme_affixes: tuple[str, ...] = DEFAULT_THEME_AFFIXES

    _reject_float = field_validator(
        "minimum_market_cap",
        "preferred_market_cap",
        "minimum_volume_per_minute",
        "overvolume_volume_per_minute",
        "minimum_volume_to_liquidity",
        "minimum_fee_efficiency",
        "minimum_total_fees_sol",
        "minimum_hourly_volume_per_minute",
        "target_volume_per_minute",
        "total_fees_reference_market_cap",
        "maximum_hourly_turnover",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_guide(self) -> "GuideScreeningRules":
        if any(
            value < 0
            for value in (
                self.minimum_market_cap,
                self.minimum_volume_per_minute,
                self.minimum_volume_to_liquidity,
                self.minimum_fee_efficiency,
                self.minimum_total_fees_sol,
                self.minimum_hourly_volume_per_minute,
                self.maximum_hourly_turnover,
            )
        ):
            raise ValueError("guide thresholds cannot be negative")
        if self.target_volume_per_minute <= 0 or self.total_fees_reference_market_cap <= 0:
            raise ValueError("score target and total-fees reference cap must be positive")
        if self.minimum_token_age < timedelta(0) or self.revived_token_age <= timedelta(0):
            raise ValueError("token age thresholds must be valid durations")
        if self.preferred_market_cap < self.minimum_market_cap:
            raise ValueError("preferred_market_cap is below minimum_market_cap")
        if self.overvolume_volume_per_minute < self.minimum_volume_per_minute:
            raise ValueError("overvolume threshold is below the working volume threshold")
        if not all(
            _LETTERS_ONLY.fullmatch(word.strip().removesuffix("*"))
            for word in self.excluded_theme_words
        ):
            raise ValueError(
                "an excluded theme word is one word of letters, optionally ending with *"
            )
        if not all(_LETTERS_ONLY.fullmatch(affix.strip()) for affix in self.theme_affixes):
            raise ValueError("a theme affix is one word of letters")
        return self


class TokenVolumeSource(StrEnum):
    """Where a token-level volume of a decision came from (G24)."""

    # A fresh GMGN reading: the token's volume as GMGN reports it, as in Trending (F5).
    GMGN = "GMGN"
    # T22: no fresh GMGN figure, a fresh Jupiter reading: the token's volume over every venue
    # Jupiter routes.
    JUPITER = "JUPITER"
    DLMM_POOLS = "DLMM_POOLS"  # the sum of the token's DLMM pools (``TOKEN_VOLUME_POOL_ONLY``)
    PENDING = "PENDING"  # the enhanced source is asked only after the cheap filters


class ReentryMark(BaseModel):
    """The token's last closed position, against which a new entry is judged (guide-v1).

    The author leaves when the volumes fall and enters the coin again only on new volumes or
    a new high ("Если АТХ пробьёт, то перезайдём"; "подожду ... на более уверенных
    объёмах"). ``since``: the observation of that entry's market snapshot;
    ``volume_per_minute``/``volume_source``: the token's 5m volume a minute the entry was
    decided with and whose figure it was; ``price_high``: the evaluated pool's highest price
    observed since, read from its history by the pipeline - ``None`` while none is known.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    exited_at: datetime
    since: datetime
    volume_per_minute: Decimal | None = None
    volume_source: TokenVolumeSource | None = None
    price_high: Decimal | None = None

    _reject_float = field_validator("volume_per_minute", "price_high", mode="before")(
        _exact_decimal
    )


class TokenSelection(BaseModel):
    """What the cycle knows about the pool's base token beyond this pool (G24).

    ``sibling_*``: the token's other DLMM pools in the universe - how many, their summed
    1h volume and summed current 5m volume (``sibling_pools_without_5m`` had no 5m window
    and add nothing, so the sum is a lower bound). ``held``: an open position or an entry
    in flight already holds the token. ``enhanced_pending``: an enhanced source (GMGN) will
    report the token's own volume after the cheap filters, so pool sums must not decide it
    yet. ``rank``/``rank_limit``: the token's place among the cycle's candidates by hourly
    volume and how many of them are checked in depth; ``rank_source``: whose hourly volume
    ranked it - GMGN's reading already held for the token, else Jupiter's (T22), else its
    DLMM pools' sum (F5). ``reentry``: the token's last exit the cycle still sees.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    sibling_pools: int = 0
    sibling_volume_1h: Decimal = Decimal(0)
    sibling_volume_5m: Decimal = Decimal(0)
    sibling_pools_without_5m: int = 0
    held: bool = False
    enhanced_pending: bool = False
    rank: int | None = None
    rank_limit: int | None = None
    rank_source: TokenVolumeSource | None = None
    reentry: ReentryMark | None = None

    _reject_float = field_validator("sibling_volume_1h", "sibling_volume_5m", mode="before")(
        _exact_decimal
    )

    @model_validator(mode="after")
    def validate_selection(self) -> "TokenSelection":
        if min(self.sibling_pools, self.sibling_pools_without_5m) < 0:
            raise ValueError("pool counts must not be negative")
        if self.sibling_pools_without_5m > self.sibling_pools:
            raise ValueError("more siblings without a 5m window than siblings")
        if self.sibling_volume_1h < 0 or self.sibling_volume_5m < 0:
            raise ValueError("sibling volumes must not be negative")
        if (self.rank is not None and self.rank < 1) or (
            self.rank_limit is not None and self.rank_limit < 1
        ):
            raise ValueError("rank and rank_limit start at one")
        return self


class ScreeningPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    minimum_tvl: Decimal
    minimum_depth: Decimal
    minimum_pool_age: timedelta
    maximum_holder_concentration: Decimal
    maximum_price_impact: Decimal
    minimum_short_ratio: Decimal
    minimum_long_ratio: Decimal
    minimum_robust_z_score: Decimal
    minimum_score: Decimal
    # The legacy spike's fee floor (30m LP fees / TVL). guide-v1 has none since 2026-10-25:
    # the author selects by volume (ADR 0016); its older records still carry 0.002.
    minimum_fee_tvl: Decimal | None = None
    target_short_ratio: Decimal = Decimal("4")
    target_long_ratio: Decimal = Decimal("2")
    target_robust_z_score: Decimal = Decimal("6")
    target_fee_tvl: Decimal = Decimal("0.01")
    target_volatility: Decimal = Decimal("0.02")
    assumed_cost_rate: Decimal = Decimal("0.002")
    maximum_turnover: Decimal = Decimal("20")
    minimum_unique_participants: int = 5
    maximum_transactions_per_participant: Decimal = Decimal("20")
    weights: ScoreWeights = ScoreWeights()
    guide: GuideScreeningRules | None = None
    volume_integrity: VolumeIntegrityMode = VolumeIntegrityMode.PARTICIPANTS
    # AVAILABLE_SIGNALS: bursts closer than this spread are one repeated trade (G07).
    # ``minimum_fee_support`` is no longer applied (G24: fees over ``volume x base fee`` is
    # at least one by construction in DLMM); it stays so stored records still load.
    maximum_burst_spread: Decimal = Decimal("0.02")
    minimum_fee_support: Decimal = Decimal("0.5")

    _reject_float = field_validator(
        "minimum_tvl",
        "minimum_depth",
        "maximum_holder_concentration",
        "maximum_price_impact",
        "minimum_short_ratio",
        "minimum_long_ratio",
        "minimum_robust_z_score",
        "minimum_fee_tvl",
        "minimum_score",
        "target_short_ratio",
        "target_long_ratio",
        "target_robust_z_score",
        "target_fee_tvl",
        "target_volatility",
        "assumed_cost_rate",
        "maximum_turnover",
        "maximum_transactions_per_participant",
        "maximum_burst_spread",
        "minimum_fee_support",
        mode="before",
    )(_exact_decimal)

    @model_validator(mode="after")
    def validate_policy(self) -> "ScreeningPolicy":
        if not Decimal(0) <= self.maximum_burst_spread < Decimal(1) or self.minimum_fee_support < 0:
            raise ValueError("artificial-volume thresholds are out of range")
        if self.guide is None and self.minimum_fee_tvl is None:
            raise ValueError("the legacy ruleset needs its minimum_fee_tvl")
        non_negative = (
            self.minimum_tvl,
            self.minimum_depth,
            self.maximum_price_impact,
            self.minimum_short_ratio,
            self.minimum_long_ratio,
            self.minimum_robust_z_score,
            self.minimum_score,
            self.assumed_cost_rate,
            self.maximum_turnover,
        )
        if any(value < 0 for value in non_negative) or (
            self.minimum_fee_tvl is not None and self.minimum_fee_tvl < 0
        ):
            raise ValueError("screening thresholds cannot be negative")
        if not Decimal(0) <= self.maximum_holder_concentration <= Decimal(1):
            raise ValueError("maximum_holder_concentration must be in [0, 1]")
        if not Decimal(0) <= self.maximum_price_impact <= Decimal(1):
            raise ValueError("maximum_price_impact must be in [0, 1]")
        if not Decimal(0) <= self.minimum_score <= Decimal(1):
            raise ValueError("minimum_score must be in [0, 1]")
        if self.minimum_pool_age < timedelta(0):
            raise ValueError("minimum_pool_age cannot be negative")
        if self.minimum_unique_participants < 1:
            raise ValueError("minimum_unique_participants must be positive")
        if any(
            value <= 0
            for value in (
                self.target_short_ratio,
                self.target_long_ratio,
                self.target_robust_z_score,
                self.target_fee_tvl,
                self.target_volatility,
                self.maximum_transactions_per_participant,
            )
        ):
            raise ValueError("normalization and integrity targets must be positive")
        return self


class GuideAssessment(BaseModel):
    """What the guide-v1 thresholds found: terminal reasons, unknown signals and evidence."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    reasons: tuple[RejectionCode, ...]
    missing_signals: tuple[str, ...]
    evidence: tuple[EvidenceItem, ...]
    volume_regime: VolumeRegime
    flags: tuple[DecisionFlag, ...] = ()
    # The token's hourly volume per minute the sustained rule used (G24) and whether the 5m
    # window spiked (``None``: not enough history to tell); both feed the guide-v1 score.
    hourly_volume_per_minute: Decimal | None = None
    volume_spike: bool | None = None


class VolumeIntegrityAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: IntegrityStatus
    reasons: tuple[str, ...]
    missing_signals: tuple[str, ...]
    flags: tuple[DecisionFlag, ...] = ()


class CandidateDecision(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    status: CandidateStatus
    pool_address: PublicKey
    token_mint: PublicKey
    decision_at: datetime
    mode: TradingMode
    estimated_price_impact: Decimal
    score: Decimal | None
    reasons: tuple[RejectionCode, ...]
    missing_signals: tuple[str, ...]
    evidence: tuple[EvidenceItem, ...]
    score_components: tuple[ScoreComponent, ...]
    features: VolumeSpikeFeatures
    ruleset_version: str
    config_version: str
    correlation_id: str
    flags: tuple[DecisionFlag, ...] = ()
    # How depth and price impact were measured (D07); ``None``: the snapshot's own depth.
    depth: DepthEvidence | None = None

    _reject_float = field_validator("score", "estimated_price_impact", mode="before")(
        _exact_decimal
    )

    @model_validator(mode="after")
    def validate_record(self) -> "CandidateDecision":
        if not all((self.ruleset_version, self.config_version, self.correlation_id)):
            raise ValueError("decision versions and correlation_id are required")
        if self.decision_at.tzinfo is None or self.decision_at.utcoffset() is None:
            raise ValueError("decision_at must be timezone-aware")
        if self.decision_at != self.features.decision_at:
            raise ValueError("record and feature decision times differ")
        if self.estimated_price_impact < 0:
            raise ValueError("estimated price impact cannot be negative")
        if self.ruleset_version != self.features.ruleset_version:
            raise ValueError("decision and feature ruleset versions differ")
        if self.config_version != self.features.config_version:
            raise ValueError("decision and feature config versions differ")
        if self.status is CandidateStatus.ACCEPT and self.score is None:
            raise ValueError("accepted decision requires a score")
        if self.score is not None and not Decimal(0) <= self.score <= Decimal(1):
            raise ValueError("decision score must be in [0, 1]")
        return self
