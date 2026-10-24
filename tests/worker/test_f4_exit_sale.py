"""F4: the sale after a gateway exit never hangs silently (G22, spec 8.5, 7.1).

Built on the T17 world (``tests/worker/test_t17_exit_sale.py``): an in-process executor of
contract V2 with SWAP whose quote impact is ``impact_per_unit`` bps per base unit sold. The
variant below adds the failures the point reviews of T17 named: an executor that never got
a part (lookup -> None), one that is unreachable, one whose simulation contradicts the
command, and a pool too thin for the part (the SDK's ``SWAP_QUOTE_INSUFFICIENT_LIQUIDITY``).
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from src.domain.strategy import PositionActionKind
from src.execution.contracts import (
    DlmmCommand,
    DlmmCommandV2,
    DlmmOperation,
    GatewayReceiptV2,
    OperatorApproval,
    SimulationErrorV1,
    SimulationResultV2,
    bind_simulation_result_v2,
    canonical_command_digest,
)
from src.execution.service import GatewayRejected
from src.persistence.contracts import PositionContext
from src.worker.execution import PositionActionStatus
from tests.worker.test_t17_exit_sale import POSITION, SaleExecutor, World, exit_at_half_price


class F4Executor(SaleExecutor):
    def __init__(self, clock: Any) -> None:
        super().__init__(clock)
        # The SWAP request is lost before the executor recorded anything: lookup -> None.
        self.forget_swaps = False
        self.swap_unreachable = False
        # The simulation answers for another payload (the service refuses the binding).
        self.corrupt_swaps = False
        # The SDK finds no liquidity for a sale larger than this.
        self.liquidity_limit: int | None = None

    async def simulate(self, command: DlmmCommand) -> SimulationResultV2:
        if command.operation is DlmmOperation.SWAP:
            assert isinstance(command, DlmmCommandV2) and command.in_amount is not None
            if self.swap_unreachable:
                raise GatewayRejected("EXECUTOR_UNREACHABLE")
            if self.liquidity_limit is not None and int(command.in_amount) > self.liquidity_limit:
                refused = bind_simulation_result_v2(
                    command_id=command.command_id,
                    command_digest=canonical_command_digest(command),
                    success=False,
                    simulated_at=self.clock.now(),
                    blockhash=None,
                    last_valid_block_height=None,
                    compute_units=0,
                    network_fee_lamports="0",
                    priority_fee_lamports="0",
                    price_impact_bps=None,
                    min_bin_id=None,
                    max_bin_id=None,
                    position_address=None,
                    active_bin_id=None,
                    token_x_decimals=None,
                    token_y_decimals=None,
                    errors=(
                        SimulationErrorV1(
                            code="SWAP_QUOTE_INSUFFICIENT_LIQUIDITY",
                            message="Insufficient liquidity in binArrays",
                        ),
                    ),
                )
                self.simulated.append((command, refused))
                return refused
        simulation = await super().simulate(command)
        if command.operation is DlmmOperation.SWAP and self.corrupt_swaps:
            return simulation.model_copy(update={"command_digest": "0" * 64})
        return simulation

    async def submit(
        self, command: DlmmCommand, idempotency_key: str, approval: OperatorApproval | None
    ) -> GatewayReceiptV2:
        if command.operation is DlmmOperation.SWAP and self.forget_swaps:
            self.submitted.append(command)
            raise TimeoutError("the request was lost; the executor holds nothing for it")
        return await super().submit(command, idempotency_key, approval)


def f4_world(**settings: Any) -> World:
    world = World(**settings)
    world.executor = F4Executor(world.clock)
    world.manager = world.new_manager()
    return world


def executor(world: World) -> F4Executor:
    assert isinstance(world.executor, F4Executor)
    return world.executor


# --- (T17) EXIT_SALE_GATEWAY_UNAVAILABLE is closed by the first sold part ------------------


def test_the_first_confirmed_part_closes_the_sale_gateway_alert() -> None:
    # 1000 base units at 0.4 bps each: 1000 breaches the 300 bps limit, 500 is sold first.
    world = f4_world()
    world.executor.exit_returns = [1_000]
    world.open()
    executor(world).swap_unreachable = True

    waiting = exit_at_half_price(world)
    raised = world.open_alerts()
    executor(world).swap_unreachable = False
    first_part = world.apply(PositionActionKind.EXIT)
    after_part = world.open_alerts()
    sold_first = world.executor.sales()
    closed = world.apply(PositionActionKind.EXIT)

    assert waiting is not None and waiting.status is PositionActionStatus.PENDING
    assert "EXIT_SALE_GATEWAY_UNAVAILABLE" in raised
    assert first_part is not None and first_part.status is PositionActionStatus.SELLING
    assert sold_first == [500]
    assert "EXIT_SALE_GATEWAY_UNAVAILABLE" not in after_part
    assert closed is not None and closed.status is PositionActionStatus.EXITED
    assert world.open_alerts() == set()


def test_a_part_the_executor_never_got_alerts_once_the_command_expired_and_is_never_resent(
) -> None:
    # The part's command lives POSITION_ACTION_TTL = 2 minutes; past it the executor cannot
    # start it any more, yet it holds no record: the outcome stays unknown under an alert.
    world = f4_world()
    world.open()
    executor(world).forget_swaps = True

    sent = exit_at_half_price(world)
    world.clock.advance(timedelta(minutes=1))
    young = world.apply(PositionActionKind.EXIT)
    young_alerts = world.open_alerts()
    world.clock.advance(timedelta(minutes=1, seconds=1))
    expired = world.apply(PositionActionKind.EXIT)
    world.clock.advance(timedelta(minutes=5))
    later = world.apply(PositionActionKind.EXIT)

    assert [item.status for item in (sent, young, expired, later) if item is not None] == [
        PositionActionStatus.PENDING
    ] * 4
    assert "EXIT_SALE_OUTCOME_UNKNOWN" not in young_alerts
    assert "EXIT_SALE_OUTCOME_UNKNOWN" in world.open_alerts()
    # One request ever left; the position is still open: nothing is booked or resent blind.
    assert world.executor.sales() == [500]
    assert world.executor.quoted_sales() == [500]
    assert world.modes.open_positions() == (POSITION,)
    alerts = [alert for alert in world.modes.alerts() if alert.code == "EXIT_SALE_OUTCOME_UNKNOWN"]
    assert len(alerts) == 1


class ForgetfulContexts:
    """The durable context store after the position's context was lost."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.lost: set[str] = set()

    async def get(self, position_id: str) -> PositionContext | None:
        return None if position_id in self.lost else await self.inner.get(position_id)

    async def put(self, context: PositionContext) -> None:
        await self.inner.put(context)

    async def all(self) -> Any:
        return await self.inner.all()


def test_a_sale_without_the_position_context_raises_a_critical_alert_like_the_exit() -> None:
    # The CLOSE is confirmed before the chain tells what it returned; meanwhile the context
    # of the position is lost, so no sale command can be built.
    world = f4_world()
    world.executor.settle_exits = False
    world.open()
    waiting = exit_at_half_price(world)
    contexts = ForgetfulContexts(world.contexts)
    contexts.lost.add(POSITION)
    world.contexts = contexts  # type: ignore[assignment]
    world.manager = world.new_manager()
    world.executor.settle_exits = True

    held = [world.apply(PositionActionKind.EXIT) for _ in range(3)]

    assert waiting is not None and waiting.status is PositionActionStatus.PENDING
    assert all(item is not None and item.detail == "CONTEXT_MISSING" for item in held)
    assert "POSITION_CONTEXT_MISSING" in world.open_alerts()
    assert world.executor.sales() == []
    assert [alert.code for alert in world.modes.alerts()].count("POSITION_CONTEXT_MISSING") == 1


def test_a_sale_blocked_before_any_record_raises_a_critical_alert_once() -> None:
    # The simulation answers for another payload: the service refuses it before it stores
    # anything, so no part exists to count - every cycle would try again in silence.
    world = f4_world()
    world.open()
    executor(world).corrupt_swaps = True

    results = [exit_at_half_price(world), world.apply(PositionActionKind.EXIT)]

    assert all(item is not None and item.status is PositionActionStatus.SELLING for item in results)
    assert "EXIT_SALE_BLOCKED" in world.open_alerts()
    assert [alert.code for alert in world.modes.alerts()].count("EXIT_SALE_BLOCKED") == 1
    assert world.executor.sales() == []
    assert world.modes.open_positions() == (POSITION,)


def test_a_quote_refused_for_liquidity_is_split_like_an_impact_breach() -> None:
    # The pool has liquidity for at most 600 base units (impact itself stays within the
    # limit: 0.1 bps a unit): 1000 is refused, 500 goes out, then the remaining 500.
    world = f4_world()
    world.executor.exit_returns = [1_000]
    world.executor.impact_per_unit = Decimal("0.1")
    executor(world).liquidity_limit = 600
    world.open()

    first = exit_at_half_price(world)
    second = world.apply(PositionActionKind.EXIT)

    assert [item.status for item in (first, second) if item is not None] == [
        PositionActionStatus.SELLING,
        PositionActionStatus.EXITED,
    ]
    assert world.executor.quoted_sales() == [1_000, 500, 500]
    assert world.executor.sales() == [500, 500]
    assert world.closed_event()["exit_sale"]["status"] == "SOLD"


def test_when_no_part_finds_liquidity_the_token_stays_under_the_impact_alert() -> None:
    # Liquidity for 50 units only: 1000, 500, 250, 125 are refused; the smallest allowed part
    # is ceil(1000 / 2**4) = 63, so the sale ends unsold instead of repeating one size.
    world = f4_world()
    world.executor.exit_returns = [1_000]
    executor(world).liquidity_limit = 50
    world.open()

    result = exit_at_half_price(world)

    assert result is not None and result.status is PositionActionStatus.EXITED
    assert world.executor.quoted_sales() == [1_000, 500, 250, 125]
    assert world.executor.sales() == []
    assert "EXIT_SALE_IMPACT_EXCEEDED" in world.open_alerts()
    event = world.closed_event()["exit_sale"]
    assert (event["status"], event["detail"]) == ("UNSOLD", "PRICE_IMPACT_EXCEEDED")


def test_a_new_outage_after_a_sold_part_raises_a_new_open_alert() -> None:
    # Review of F4: the first outage is closed by the sold part (500 of 1000); the gateway
    # then fails again before the last 500 - that outage must not hide behind the first.
    world = f4_world()
    world.executor.exit_returns = [1_000]
    world.open()
    executor(world).swap_unreachable = True
    exit_at_half_price(world)
    executor(world).swap_unreachable = False
    world.apply(PositionActionKind.EXIT)  # 1000 breaches, 500 is sold: the alert closes
    sold_first = world.executor.sales()
    executor(world).swap_unreachable = True

    again = [world.apply(PositionActionKind.EXIT) for _ in range(2)]
    open_during_outage = world.open_alerts()
    executor(world).swap_unreachable = False
    closed = world.apply(PositionActionKind.EXIT)

    assert sold_first == [500]
    assert all(
        item is not None and item.detail == "GATEWAY_UNAVAILABLE" for item in again
    )
    assert "EXIT_SALE_GATEWAY_UNAVAILABLE" in open_during_outage
    codes = [alert.code for alert in world.modes.alerts()]
    assert codes.count("EXIT_SALE_GATEWAY_UNAVAILABLE") == 2  # one per outage, not per cycle
    assert closed is not None and closed.status is PositionActionStatus.EXITED
    assert world.executor.sales() == [500, 500]
    assert world.open_alerts() == set()


def test_the_close_booking_closes_a_sale_gateway_alert_raised_before_it() -> None:
    world = f4_world()
    world.open()
    executor(world).swap_unreachable = True
    exit_at_half_price(world)
    executor(world).swap_unreachable = False

    result = world.apply(PositionActionKind.EXIT)

    assert result is not None and result.status is PositionActionStatus.EXITED
    assert world.executor.sales() == [500]
    assert "EXIT_SALE_GATEWAY_UNAVAILABLE" not in world.open_alerts()
