"""Read models of ``/api/v1``: status, pools, decisions, positions, P&L, config, stream."""

import json
from pathlib import Path

import httpx

from src.api.probes import WorkerHealthProbe
from tests.api.support import (
    OPERATOR_TOKEN,
    POOL_B,
    POOL_C,
    Panel,
    make_panel,
    populated,
)
from tests.worker.harness import POOL


def test_first_run_reports_warm_up_instead_of_an_empty_success(panel: Panel) -> None:
    status = panel.get("/api/v1/system/status").json()
    pools = panel.get("/api/v1/pools").json()

    assert status["mode"]["mode"] == "SHADOW" and status["mode"]["kill_switch_active"] is False
    assert status["universe"]["state"] == "NOT_SYNCED"
    assert "прогрев" in status["universe"]["message"]
    assert status["universe"]["pool_count"] == 0
    assert status["risk"]["risk_day"] is None
    assert status["approvals_pending"] == 0
    services = {item["name"]: item["status"] for item in status["services"]}
    assert services == {"api": "OK", "database": "OK"}
    assert pools == {"items": [], "next_cursor": None, "limit": 50}


def test_dashboard_status_shows_positions_risk_and_reject_categories(panel: Panel) -> None:
    populated(panel.harness)

    status = panel.get("/api/v1/system/status").json()

    universe = status["universe"]
    assert universe["state"] == "FRESH" and universe["pool_count"] == 3
    assert universe["decision_counts"] == {"ACCEPT": 1, "REJECT": 1, "WARMING_UP": 1}
    reasons = {item["code"]: item for item in universe["reject_reasons"]}
    assert reasons["LOW_TVL"]["count"] == 1 and reasons["LOW_TVL"]["label"] == "TVL ниже минимума"
    assert "HISTORY_WARMING_UP" in reasons
    risk = status["risk"]
    assert risk["open_positions"] == 1 and risk["maximum_positions"] == 3
    assert risk["risk_day"] == "2026-10-18" and risk["daily_stop_used"] == "0.0000"
    assert status["ruleset_version"] == "rules-1"


def test_provider_degradation_reported_by_the_worker_reaches_the_dashboard(
    tmp_path: Path,
) -> None:
    def worker(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health/ready"
        return httpx.Response(
            503,
            json={
                "status": "DEGRADED",
                "mode": "SHADOW",
                "reasons": ["PROVIDER_DEGRADED:meteora", "UNIVERSE_STALE"],
            },
        )

    probe = WorkerHealthProbe(
        "http://worker:9100", timeout_seconds=1, transport=httpx.MockTransport(worker)
    )
    panel = make_panel(tmp_path, probes=(probe,))
    try:
        status = panel.get("/api/v1/system/status").json()
    finally:
        panel.repositories.close()

    worker_health = next(item for item in status["services"] if item["name"] == "worker")
    assert worker_health["status"] == "DEGRADED"
    messages = [item["message"] for item in status["readiness"]]
    assert "провайдер деградировал: meteora" in messages
    assert "universe устарел или ещё не синхронизирован" in messages


def test_an_unreachable_worker_is_down_not_healthy(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    probe = WorkerHealthProbe(
        "http://worker:9100", timeout_seconds=1, transport=httpx.MockTransport(refuse)
    )
    panel = make_panel(tmp_path, probes=(probe,))
    try:
        status = panel.get("/api/v1/system/status").json()
    finally:
        panel.repositories.close()

    worker_health = next(item for item in status["services"] if item["name"] == "worker")
    assert worker_health == {
        "name": "worker",
        "status": "DOWN",
        "reasons": [{"code": "WORKER_UNREACHABLE", "message": "worker не отвечает"}],
    }


def test_pools_are_paginated_and_carry_the_latest_decision(panel: Panel) -> None:
    populated(panel.harness)

    first = panel.get("/api/v1/pools?limit=2").json()
    second = panel.get(f"/api/v1/pools?limit=2&cursor={first['next_cursor']}").json()
    search = panel.get(f"/api/v1/pools?q={POOL_B.address.root[:6]}").json()

    addresses = [item["address"] for item in first["items"] + second["items"]]
    assert first["next_cursor"] == "2" and second["next_cursor"] is None
    assert sorted(addresses) == sorted(
        [POOL.address.root, POOL_B.address.root, POOL_C.address.root]
    )
    by_address = {item["address"]: item for item in first["items"] + second["items"]}
    assert by_address[POOL.address.root]["decision_status"] == "ACCEPT"
    assert by_address[POOL_B.address.root]["decision_label"] == "прогрев"
    assert [item["address"] for item in search["items"]] == [POOL_B.address.root]


def test_pool_card_shows_warm_up_progress_and_readiness_condition(panel: Panel) -> None:
    populated(panel.harness)

    warming = panel.get(f"/api/v1/pools/{POOL_B.address.root}").json()
    ready = panel.get(f"/api/v1/pools/{POOL.address.root}").json()
    unknown = panel.get("/api/v1/pools/SysvarS1otHashes111111111111111111111111111")

    assert warming["warmup"]["state"] == "WARMING_UP"
    assert warming["warmup"]["required"] == 6
    assert "из 6" in warming["warmup"]["message"]
    assert warming["latest_decision"]["status"] == "WARMING_UP"
    assert ready["warmup"]["state"] == "READY" and ready["history_points"] == 7
    assert unknown.status_code == 404


def test_decisions_are_paginated_newest_first_and_expose_their_evidence(panel: Panel) -> None:
    scenario = populated(panel.harness)

    first = panel.get("/api/v1/decisions?limit=2").json()
    second = panel.get(f"/api/v1/decisions?limit=2&cursor={first['next_cursor']}").json()
    accepted = panel.get("/api/v1/decisions?status=ACCEPT").json()
    by_pool = panel.get(f"/api/v1/decisions?pool={POOL_B.address.root}").json()

    ids = [item["decision_id"] for item in first["items"] + second["items"]]
    expected = [str(item.decision_id) for item in reversed([scenario.accepted, *scenario.others])]
    assert ids == expected and second["next_cursor"] is None
    assert [item["status"] for item in accepted["items"]] == ["ACCEPT"]
    assert [item["pool_address"] for item in by_pool["items"]] == [POOL_B.address.root]
    warming = by_pool["items"][0]
    assert {
        "code": "HISTORY_WARMING_UP",
        "message": "история ещё накапливается",
    } in warming["reasons"]
    assert warming["ruleset_version"] == "rules-1" and warming["freshness"] == "FRESH"


def test_decision_detail_shows_strategy_reason_thresholds_and_versions(panel: Panel) -> None:
    scenario = populated(panel.harness)
    rejected = scenario.others[0]

    accepted = panel.get(f"/api/v1/decisions/{scenario.accepted.decision_id}").json()
    warming = panel.get(f"/api/v1/decisions/{rejected.decision_id}").json()

    strategy = accepted["strategy"]
    assert strategy["plan_id"] == str(scenario.accepted.plan_id)
    assert strategy["selection_reason"] is not None and strategy["selection_label"]
    assert strategy["admitted"] is True
    names = {item["name"]: item for item in accepted["evidence"]}
    assert names["tvl"]["threshold"] == "1000" and names["tvl"]["triggered"] is False
    assert accepted["decision"]["ruleset_version"] == "rules-1"
    assert accepted["decision"]["config_version"] == "config-1"
    assert accepted["facts"]["volume_short"] == "400"
    assert accepted["token"]["mint_authority"] == "REVOKED"
    assert warming["strategy"] is None
    assert warming["decision"]["status"] == "WARMING_UP"


def test_positions_show_lifecycle_range_valuation_costs_and_hodl(panel: Panel) -> None:
    scenario = populated(panel.harness)
    position_id = scenario.accepted.entry.position_id

    listing = panel.get("/api/v1/positions").json()
    detail = panel.get(f"/api/v1/positions/{position_id}").json()
    performance = panel.get("/api/v1/performance?days=7").json()

    assert [item["position_id"] for item in listing["items"]] == [position_id]
    summary = listing["items"][0]
    assert summary["status"] == "OPEN" and summary["valuation_source"] == "MARKET"
    assert summary["net_pnl_base_units"] is not None
    assert detail["range"]["in_range"] is True
    assert detail["range"]["lower_price"] < detail["range"]["entry_price"]
    valuation = detail["valuation"]
    assert set(valuation) >= {
        "fee_income_base_units",
        "divergence_loss_base_units",
        "total_costs_base_units",
        "net_pnl_base_units",
        "hodl_pnl_base_units",
        "excess_vs_hodl_base_units",
    }
    assert [step["kind"] for step in detail["lifecycle"]] == ["OPEN"]
    assert detail["decision_id"] == str(scenario.accepted.decision_id)
    assert performance["open_positions"] == 1 and performance["groups"] == []
    assert panel.get("/api/v1/positions/unknown-position").status_code == 404


def test_effective_config_masks_secrets_and_names_what_is_missing(tmp_path: Path) -> None:
    helius = "helius-test-value-123"
    rpc_key = "rpc-key-test-value-456"
    panel = make_panel(
        tmp_path,
        environ={
            "HELIUS_API_KEY": helius,
            "SOLANA_RPC_URL": f"https://rpc.example.org/v1/{rpc_key}?api-key={rpc_key}",
            "EXECUTOR_APPROVAL_SECRET": "approval-test-value-789",
        },
    )
    try:
        response = panel.get("/api/v1/config/effective")
    finally:
        panel.repositories.close()

    text = response.text
    for value in (helius, rpc_key, OPERATOR_TOKEN, "approval-test-value-789"):
        assert value not in text
    config = response.json()
    assert config["settings"]["solana_rpc_url"] == "https://rpc.example.org/[скрыто]"
    assert config["settings"]["helius_api_key"] == "[REDACTED]"
    secrets = {item["name"]: item["configured"] for item in config["secrets"]}
    assert secrets["HELIUS_API_KEY"] is True and secrets["EXECUTOR_APPROVAL_SECRET"] is True
    assert secrets["DATABASE_URL"] is False
    readiness = {item["mode"]: item for item in config["readiness"]}
    assert readiness["SHADOW"]["ready"] is True
    assert readiness["APPROVAL"]["ready"] is False
    assert {"EXECUTOR_URL", "EXECUTOR_INTERNAL_TOKEN", "WALLET_PUBLIC_KEY"} <= set(
        readiness["APPROVAL"]["missing"]
    )
    assert "SOLANA_RPC_URL" not in readiness["APPROVAL"]["missing"]
    assert {"EXECUTOR_MODE_TOKEN", "LIVE_PROMOTION_FILE"} <= set(readiness["LIVE"]["missing"])
    assert config["thresholds"]["risk"]["maximum_open_positions"] == 3


def test_status_stream_pushes_a_status_event(panel: Panel) -> None:
    response = panel.get("/api/v1/stream/status")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [block for block in response.text.split("\n\n") if block.startswith("event:")]
    assert len(events) == 1
    name, data = events[0].split("\n", 1)
    assert name == "event: status"
    pulse = json.loads(data.removeprefix("data: "))
    assert pulse["mode"]["mode"] == "SHADOW" and pulse["universe_state"] == "NOT_SYNCED"


def test_metrics_expose_latency_errors_freshness_and_queue_by_route(panel: Panel) -> None:
    panel.get("/api/v1/system/status")
    panel.get("/api/v1/decisions/not-a-uuid")
    panel.get("/")

    text = panel.get("/metrics").text

    assert 'meteora_api_request_seconds_count{method="GET",route="/api/v1/system/status"}' in text
    assert (
        'meteora_api_errors_total{method="GET",route="/api/v1/decisions/{decision_id}",'
        'status="422"}' in text
    )
    assert "meteora_api_universe_age_seconds -1.0" in text
    assert "meteora_api_approvals_pending 0.0" in text


def test_openapi_documents_the_versioned_contract_and_error_envelope(panel: Panel) -> None:
    schema = panel.get("/openapi.json").json()

    paths = set(schema["paths"])
    assert {
        "/api/v1/system/status",
        "/api/v1/pools",
        "/api/v1/pools/{address}",
        "/api/v1/decisions",
        "/api/v1/decisions/{decision_id}",
        "/api/v1/positions",
        "/api/v1/positions/{position_id}",
        "/api/v1/performance",
        "/api/v1/config/effective",
        "/api/v1/approvals/{command_id}",
        "/api/v1/mode/transitions",
        "/api/v1/kill-switch/activate",
        "/api/v1/kill-switch/release",
    } <= paths
    assert not any(path.startswith(("/pools", "/static")) for path in paths)
    assert "ErrorEnvelope" in schema["components"]["schemas"]
    approve = schema["paths"]["/api/v1/approvals/{command_id}"]["post"]
    assert approve["responses"]["409"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorEnvelope"
    }
