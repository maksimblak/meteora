from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.execution.contracts import (
    ChainReceiptV1,
    DlmmCommandV1,
    DlmmCommandV2,
    GatewayReceiptV1,
    GatewayReceiptV2,
    QuoteDirection,
    QuoteRequestV1,
    QuoteResultV1,
    SimulationResultV1,
    SimulationResultV2,
    canonical_command_digest,
    canonical_simulation_digest,
    command_from_payload,
    receipt_from_payload,
    simulation_from_payload,
)

CONTRACTS = Path(__file__).parents[2] / "contracts" / "execution"


def corpus(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    return loaded


def test_python_contract_accepts_the_v2_corpus_with_shared_digests() -> None:
    v2 = corpus("v2-corpus.json")

    commands = [DlmmCommandV2.model_validate(payload) for payload in v2["valid_commands"]]
    simulations = [
        SimulationResultV2.model_validate(payload) for payload in v2["valid_simulations"]
    ]

    assert [command.operation.value for command in commands] == ["OPEN", "ADD", "REMOVE", "CLOSE"]
    assert commands[0].position_address is None
    assert [canonical_command_digest(command) for command in commands] == v2[
        "canonical_command_digests"
    ]
    assert [canonical_simulation_digest(value) for value in simulations] == v2[
        "canonical_simulation_digests"
    ]
    for payload in v2["valid_gateway_receipts"]:
        assert GatewayReceiptV2.model_validate(payload).position_address == (
            "SysvarRent111111111111111111111111111111111"
        )


@pytest.mark.parametrize(
    ("model", "key"),
    [
        (DlmmCommandV2, "invalid_commands"),
        (SimulationResultV2, "invalid_simulations"),
        (GatewayReceiptV2, "invalid_gateway_receipts"),
    ],
)
def test_python_contract_rejects_every_invalid_v2_payload(model: Any, key: str) -> None:
    for invalid in corpus("v2-corpus.json")[key]:
        with pytest.raises(ValidationError):
            model.model_validate(invalid["payload"])


def test_retired_v1_payloads_are_never_read_as_v2() -> None:
    v1 = corpus("v1-corpus.json")
    cases = (
        (DlmmCommandV2, "valid_commands"),
        (SimulationResultV2, "valid_simulations"),
        (GatewayReceiptV2, "valid_gateway_receipts"),
    )

    for model, key in cases:
        for payload in v1[key]:
            with pytest.raises(ValidationError):
                model.model_validate(payload)


def test_v1_remains_the_lifecycle_shape_of_stored_and_shadow_records() -> None:
    v1 = corpus("v1-corpus.json")

    commands = [DlmmCommandV1.model_validate(payload) for payload in v1["valid_commands"]]

    assert [canonical_command_digest(command) for command in commands] == v1[
        "canonical_command_digests"
    ]
    contract_cases = (
        (SimulationResultV1, "valid_simulations", "invalid_simulations"),
        (GatewayReceiptV1, "valid_gateway_receipts", "invalid_gateway_receipts"),
        (ChainReceiptV1, "valid_chain_receipts", "invalid_chain_receipts"),
    )
    for model, valid_key, invalid_key in contract_cases:
        for payload in v1[valid_key]:
            model.model_validate(payload)
        for invalid in v1[invalid_key]:
            with pytest.raises(ValidationError):
                model.model_validate(invalid["payload"])


def test_stored_payloads_are_read_by_their_own_version_and_unknown_ones_refused() -> None:
    v1, v2 = corpus("v1-corpus.json"), corpus("v2-corpus.json")

    assert isinstance(command_from_payload(v1["valid_commands"][0]), DlmmCommandV1)
    assert isinstance(command_from_payload(v2["valid_commands"][0]), DlmmCommandV2)
    assert isinstance(simulation_from_payload(v1["valid_simulations"][0]), SimulationResultV1)
    assert isinstance(simulation_from_payload(v2["valid_simulations"][0]), SimulationResultV2)
    assert isinstance(receipt_from_payload(v1["valid_gateway_receipts"][0]), GatewayReceiptV1)
    assert isinstance(receipt_from_payload(v2["valid_gateway_receipts"][0]), GatewayReceiptV2)
    for parse, payload in (
        (command_from_payload, v2["invalid_commands"][0]["payload"]),
        (simulation_from_payload, v2["invalid_simulations"][0]["payload"]),
        (receipt_from_payload, v2["invalid_gateway_receipts"][0]["payload"]),
    ):
        with pytest.raises(ValidationError):
            parse(payload)


def test_v2_extension_swap_commands_share_their_digests_and_old_payloads_stay_valid() -> None:
    v2 = corpus("v2-corpus.json")

    commands = [DlmmCommandV2.model_validate(payload) for payload in v2["swap_commands"]]

    assert [command.operation.value for command in commands] == ["SWAP", "SWAP"]
    assert [command.in_amount for command in commands] == ["125000000", "1"]
    assert all(command.min_bin_id is None and command.max_bin_id is None for command in commands)
    assert [canonical_command_digest(command) for command in commands] == v2[
        "swap_command_digests"
    ]
    # The extension is compatible: every payload valid before keeps its digest.
    assert [
        canonical_command_digest(DlmmCommandV2.model_validate(payload))
        for payload in v2["valid_commands"]
    ] == v2["canonical_command_digests"]
    for invalid in v2["invalid_swap_commands"]:
        with pytest.raises(ValidationError):
            DlmmCommandV2.model_validate(invalid["payload"])


def test_v2_extension_swap_simulations_share_their_digests() -> None:
    v2 = corpus("v2-corpus.json")

    simulations = [SimulationResultV2.model_validate(item) for item in v2["swap_simulations"]]

    assert simulations[0].swap_quote is not None
    assert simulations[0].swap_quote.out_amount == "2500000000"
    assert simulations[0].swap_quote.min_out_amount == "2475000000"
    assert simulations[1].errors[0].code == "PRICE_IMPACT_BREACH"
    assert simulations[2].swap_quote is None
    assert [canonical_simulation_digest(item) for item in simulations] == v2[
        "swap_simulation_digests"
    ]
    assert [
        canonical_simulation_digest(SimulationResultV2.model_validate(item))
        for item in v2["valid_simulations"]
    ] == v2["canonical_simulation_digests"]
    for invalid in v2["invalid_swap_simulations"]:
        with pytest.raises(ValidationError):
            SimulationResultV2.model_validate(invalid["payload"])


def test_v2_extension_receipts_carry_the_chain_settlement_of_a_confirmed_command() -> None:
    v2 = corpus("v2-corpus.json")

    swap, close, pending = (
        GatewayReceiptV2.model_validate(item) for item in v2["settled_receipts"]
    )

    assert swap.settlement is not None
    assert (swap.settlement.delta_x, swap.settlement.delta_y) == (
        "-125000000",
        "2491000000",
    )
    assert (swap.settlement.fee_lamports, swap.settlement.priority_fee_lamports) == (
        "6200",
        "1200",
    )
    assert close.settlement is not None and close.settlement.delta_x == "125000000"
    assert pending.settlement is None
    assert all(
        GatewayReceiptV2.model_validate(item).settlement is None
        for item in v2["valid_gateway_receipts"]
    )
    assert "settlement" not in swap.lifecycle().model_dump()
    for invalid in v2["invalid_settled_receipts"]:
        with pytest.raises(ValidationError):
            GatewayReceiptV2.model_validate(invalid["payload"])


def test_the_retired_v1_lifecycle_shape_has_no_swap() -> None:
    swap = corpus("v2-corpus.json")["swap_commands"][0]
    lifecycle = {
        key: value
        for key, value in swap.items()
        if key not in {"swap_direction", "in_amount", "max_price_impact_bps"}
    }

    with pytest.raises(ValidationError):
        DlmmCommandV1.model_validate(
            {**lifecycle, "schema_version": 1, "min_bin_id": 0, "max_bin_id": 0, "remove_bps": 1}
        )


def test_python_quote_contract_shares_the_quote_corpus() -> None:
    quotes = corpus("quote-v1-corpus.json")

    requests = [QuoteRequestV1.model_validate(payload) for payload in quotes["valid_requests"]]
    results = [QuoteResultV1.model_validate(payload) for payload in quotes["valid_results"]]

    assert [request.direction for request in requests] == [
        QuoteDirection.Y_TO_X,
        QuoteDirection.X_TO_Y,
    ]
    assert [result.success for result in results] == [True, False]
    assert results[0].price_impact_bps == 37
    for model, key in ((QuoteRequestV1, "invalid_requests"), (QuoteResultV1, "invalid_results")):
        for invalid in quotes[key]:
            with pytest.raises(ValidationError):
                model.model_validate(invalid["payload"])
