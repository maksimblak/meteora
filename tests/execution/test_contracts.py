from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.execution.contracts import (
    ChainReceiptV1,
    DlmmCommandV1,
    GatewayReceiptV1,
    SimulationResultV1,
    canonical_command_digest,
    canonical_simulation_digest,
)

CORPUS = Path(__file__).parents[2] / "contracts" / "execution" / "v1-corpus.json"


def test_python_contract_accepts_only_v1_corpus() -> None:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))

    commands = [DlmmCommandV1.model_validate(payload) for payload in corpus["valid_commands"]]

    assert [command.operation.value for command in commands] == [
        "OPEN",
        "ADD",
        "REMOVE",
        "CLOSE",
    ]
    assert [canonical_command_digest(command) for command in commands] == corpus[
        "canonical_command_digests"
    ]
    for invalid in corpus["invalid_commands"]:
        with pytest.raises(ValidationError):
            DlmmCommandV1.model_validate(invalid["payload"])


def test_python_contract_shares_simulation_submit_and_status_payloads() -> None:
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))

    contract_cases = (
        (SimulationResultV1, "valid_simulations", "invalid_simulations"),
        (GatewayReceiptV1, "valid_gateway_receipts", "invalid_gateway_receipts"),
        (ChainReceiptV1, "valid_chain_receipts", "invalid_chain_receipts"),
    )
    for model, valid_key, invalid_key in contract_cases:
        for payload in corpus[valid_key]:
            model.model_validate(payload)
        for invalid in corpus[invalid_key]:
            with pytest.raises(ValidationError):
                model.model_validate(invalid["payload"])

    simulations = [
        SimulationResultV1.model_validate(payload)
        for payload in corpus["valid_simulations"]
    ]
    assert [canonical_simulation_digest(value) for value in simulations] == corpus[
        "canonical_simulation_digests"
    ]
