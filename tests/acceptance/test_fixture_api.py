"""The acceptance Data API stand-in serves what the live API serves (F1 A3, F3).

Runs in the canonical check (no Docker): the replay of recorded sweeps carries each
token's recorded USD price, as the live ``/pools`` does, and the acceptance cadence lets
the recorded counters build a full 5m window under the F1 rules before they freeze.
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from src.worker.policy import guide_policy
from tests.acceptance.fixture_api import SERIES, Replay

LIVE = SERIES.parent
SOL_USDC = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6"
HYPE = "98sMhvDwXj1RQi5c5Mndm3vPe9cBqPrbLaufMXFNMh5g"
OVERLAY = Path("tests/acceptance/compose.acceptance.yml")


def recorded(name: str) -> Any:
    return json.loads((LIVE / name).read_text(encoding="utf-8"))


def test_every_served_token_carries_its_recorded_usd_price() -> None:
    single = recorded("pool_single.json")
    replay = Replay(SERIES, 6)

    sweep = json.loads(replay.next_sweep())
    pool = replay.pool(SOL_USDC)

    by_address = {item["address"]: item for item in sweep["data"]}
    sol_usdc = by_address[SOL_USDC]
    # As the Data API sent them the same day (pool_single.json), value for value.
    assert sol_usdc["token_y"]["price"] == single["token_y"]["price"]
    assert sol_usdc["token_x"]["price"] == single["token_x"]["price"]
    assert pool is not None and pool["token_y"]["price"] == single["token_y"]["price"]
    # A token nothing recorded a price for stays unpriced (fails closed, never invented).
    hype_pools = [item for item in sweep["data"] if item["token_y"]["address"] == HYPE]
    assert hype_pools and all("price" not in item["token_y"] for item in hype_pools)
    priced = [item for item in sweep["data"] if "price" in item["token_y"]]
    assert len(priced) == len(sweep["data"]) - len(hype_pools)


class OverlayLoader(yaml.SafeLoader):
    """Compose's ``!override`` tag read as the plain value it carries."""


OverlayLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))


def test_the_acceptance_cadence_builds_a_full_f1_window_before_the_freeze() -> None:
    overlay = yaml.load(OVERLAY.read_text(encoding="utf-8"), Loader=OverlayLoader)["services"]  # noqa: S506
    interval = timedelta(
        seconds=int(overlay["worker"]["environment"]["WORKER_UNIVERSE_INTERVAL_SECONDS"])
    )
    freeze_after = int(
        overlay["fixture-api"]["environment"]["FIXTURE_FREEZE_AFTER"].split(":-")[1].rstrip("}")
    )
    rules = guide_policy(None, universe_interval=interval).short_window

    recorded_span = interval * (freeze_after - 1)
    steps = next(
        count
        for count in range(1, freeze_after)
        if interval * count >= rules.minimum_interval
    )

    assert recorded_span >= rules.minimum_interval
    assert interval * steps <= rules.maximum_interval
