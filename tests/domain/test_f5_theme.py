"""F5 (T18 review): the theme filter matches words, not substrings.

Ordinary coins whose names merely contain a theme word pass; glued meme names built from a
theme word and meme affixes (ELONDOGE, BABYTRUMP) are excluded like the plain word (MAGA).
"""

import pytest

from src.domain.models.trading import TradingMode
from src.domain.screening import CandidateStatus, GuideScreeningRules, RejectionCode
from tests.domain.guide_fixtures import guide_history
from tests.domain.test_t18_selection import evidence, policy, screen

ORDINARY = [
    ("WATERMELON", "WMLN"),
    ("Belong", "BLNG"),
    ("Felony", "FLNY"),
    ("Natural Selection", "NSEL"),
    ("MAGAZINE", "MGZN"),
    ("Computing", "CMPT"),
    ("Rasputin", "RSPT"),
    ("Маска", "MSKA"),
    ("Маскот", "MSKT"),
    ("Trumpet", "TRPT"),
    ("Mainstream", "MSTR"),
    ("Upstream", "UPST"),
    ("Musket", "MSKT"),
]
THEMED = [
    ("Elon Doge", "ELONDOGE", "elon"),
    ("Baby Trump", "BABYTRUMP", "trump"),
    ("MAGA Hat", "MAGA", "maga"),
]


@pytest.mark.parametrize(("name", "symbol"), ORDINARY)
def test_a_name_that_only_contains_a_theme_word_is_not_excluded(name: str, symbol: str) -> None:
    decision = screen(guide_history(name=name, symbol=symbol), mode=TradingMode.SHADOW)

    assert RejectionCode.THEME_EXCLUDED not in decision.reasons
    assert evidence(decision)["theme"][0] == "none"
    assert decision.status is CandidateStatus.ACCEPT, decision.reasons


@pytest.mark.parametrize(("name", "symbol", "word"), THEMED)
def test_a_theme_word_alone_or_glued_to_meme_affixes_is_excluded(
    name: str, symbol: str, word: str
) -> None:
    # Only the ticker carries the theme: the name is neutral.
    decision = screen(guide_history(name="Coin", symbol=symbol), mode=TradingMode.SHADOW)
    named = screen(guide_history(name=name, symbol="COIN"), mode=TradingMode.SHADOW)

    for result in (decision, named):
        assert result.status is CandidateStatus.REJECT
        assert RejectionCode.THEME_EXCLUDED in result.reasons
        assert evidence(result)["theme"][0] == word


def test_a_word_ending_with_a_star_is_a_stem() -> None:
    rules = policy(GuideScreeningRules(excluded_theme_words=("politic*", "doge")))

    political = screen(guide_history(name="Political Cat", symbol="PCAT"), rules=rules)
    dogecoin = screen(guide_history(name="Dogecoin", symbol="DGC"), rules=rules)
    hotdoge = screen(guide_history(name="Hotdoge", symbol="HTD"), rules=rules)

    assert evidence(political)["theme"][0] == "politic*"
    assert evidence(dogecoin)["theme"][0] == "doge"  # "doge" + the affix "coin"
    assert RejectionCode.THEME_EXCLUDED not in hotdoge.reasons  # "hot" is no meme affix


@pytest.mark.parametrize("word", ["joe biden", "*trump", "tr*mp", "", "2024"])
def test_a_theme_word_is_one_word_of_letters_with_an_optional_final_star(word: str) -> None:
    with pytest.raises(ValueError):
        GuideScreeningRules(excluded_theme_words=(word,))
