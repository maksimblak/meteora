"""Settings of the sale after an exit (``SWAP_AFTER_EXIT`` and its limits)."""

from datetime import timedelta

import pytest

from src.execution.settings import SwapSettings


def test_the_sale_after_an_exit_is_on_by_default_with_conservative_limits() -> None:
    settings = SwapSettings.from_environment({})

    assert settings == SwapSettings()
    assert settings.enabled is True
    assert settings.max_price_impact_bps == 300
    assert settings.slippage_bps == 100
    assert settings.max_splits == 4
    assert settings.attempts == 5
    assert settings.settlement_timeout == timedelta(minutes=10)


def test_every_limit_is_read_from_the_environment_and_empty_means_default() -> None:
    settings = SwapSettings.from_environment(
        {
            "SWAP_AFTER_EXIT": "false",
            "SWAP_MAX_PRICE_IMPACT_BPS": "500",
            "SWAP_SLIPPAGE_BPS": "",
            "SWAP_AFTER_EXIT_MAX_SPLITS": "2",
            "SWAP_AFTER_EXIT_ATTEMPTS": "3",
            "SWAP_SETTLEMENT_TIMEOUT_SECONDS": "60",
        }
    )

    assert settings == SwapSettings(
        enabled=False,
        max_price_impact_bps=500,
        slippage_bps=100,
        max_splits=2,
        attempts=3,
        settlement_timeout=timedelta(seconds=60),
    )
    assert SwapSettings.from_environment({"SWAP_AFTER_EXIT": "ON"}).enabled is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SWAP_AFTER_EXIT", "maybe"),
        ("SWAP_MAX_PRICE_IMPACT_BPS", "10001"),
        ("SWAP_MAX_PRICE_IMPACT_BPS", "1.5"),
        ("SWAP_SLIPPAGE_BPS", "-1"),
        ("SWAP_AFTER_EXIT_MAX_SPLITS", "11"),
        ("SWAP_AFTER_EXIT_ATTEMPTS", "0"),
        ("SWAP_SETTLEMENT_TIMEOUT_SECONDS", "0"),
    ],
)
def test_a_misread_limit_stops_the_start(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        SwapSettings.from_environment({name: value})
