"""F4 (spec 6.8, No 49): in a pool not quoted in wrapped SOL the modeled priority fee has no
price in the quote token (the snapshot carries no SOL price), so the SHADOW fill says so
with a model flag on its ``CommandConfirmed`` instead of silently booking nothing."""

from src.accounting import InMemoryAccountingRepository
from src.accounting.models import ExitSaleModel
from src.accounting.service import ShadowBroker
from src.core.clock import FrozenClock
from src.domain.models.events import EventType
from src.execution.contracts import DlmmOperation
from tests.accounting.test_t19_shadow_costs import NOW, USDC, WSOL, bid, costs, snapshot


def confirmed_flags(quote: str, priority_lamports: int) -> list[object]:
    repo = InMemoryAccountingRepository()
    broker = ShadowBroker(repo, FrozenClock(NOW), exit_sale=ExitSaleModel())
    cost = costs(priority_lamports)
    broker.apply(bid(DlmmOperation.OPEN, "open"), snapshot("2", quote=quote), cost)
    broker.apply(bid(DlmmOperation.CLOSE, "close"), snapshot("1", quote=quote), cost)
    return [
        event.payload.get("cost_model_flags")
        for event in repo.outbox_events()
        if event.event_type is EventType.COMMAND_CONFIRMED
    ]


def test_a_pool_not_quoted_in_sol_flags_its_unpriced_priority_fee_on_every_fill() -> None:
    assert confirmed_flags(USDC, 1_000_000) == [["PRIORITY_FEE_UNPRICED"]] * 2


def test_a_sol_pool_or_a_zero_fee_has_nothing_to_flag() -> None:
    assert confirmed_flags(WSOL, 1_000_000) == [None, None]
    assert confirmed_flags(USDC, 0) == [None, None]
