import asyncio

import httpx

from src.core.app import create_app


def test_health_endpoints_distinguish_liveness_from_readiness() -> None:
    ready = False

    async def readiness_probe() -> bool:
        return ready

    async def get(path: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app(readiness_probe=readiness_probe))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    live_response = asyncio.run(get("/health/live"))
    not_ready_response = asyncio.run(get("/health/ready"))
    ready = True
    ready_response = asyncio.run(get("/health/ready"))

    assert live_response.status_code == 200
    assert live_response.json() == {"status": "ok"}
    assert not_ready_response.status_code == 503
    assert not_ready_response.json() == {"status": "not_ready"}
    assert ready_response.status_code == 200
    assert ready_response.json() == {"status": "ready", "mode": "SHADOW"}


def test_metrics_endpoint_uses_prometheus_content_type() -> None:
    async def readiness_probe() -> bool:
        return True

    async def get_metrics() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app(readiness_probe=readiness_probe))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/metrics")

    response = asyncio.run(get_metrics())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "python_info" in response.text
