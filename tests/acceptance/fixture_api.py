"""Stand-in for the Meteora DLMM Data API inside the acceptance stack (ticket 08).

Serves the recorded live sweeps of ``tests/providers/fixtures/meteora_live/series``
(17 pools, recorded 2026-10-23, every value as the API sent it) so the deployed worker
ingests a real-format universe without any external network:

* ``GET /pools`` - one recorded sweep per request, in order; from the request number
  ``FIXTURE_FREEZE_AFTER`` on the last replayed sweep is served again and again, so the
  cumulative counters stop and the short windows derived from them fall to zero (a
  volume decay that closes the shadow positions);
* ``GET /pools/{address}`` - that pool from the sweep served last;
* ``GET /pools/{address}/volume/history`` - the recorded 5m history of the two recorded
  pools, moved forward in whole 5-minute steps so it ends now; other pools get an
  empty history;
* ``GET /health`` and ``GET /state`` - liveness and replay progress for the smoke test.

Standard library only; it binds inside the compose network (``expose``, no ``ports``).
"""

import json
import os
import re
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

SERIES = Path(__file__).resolve().parents[1] / "providers" / "fixtures" / "meteora_live" / "series"
BUCKET_SECONDS = 300
HISTORY_PATH = re.compile(r"^/pools/([1-9A-HJ-NP-Za-km-z]{32,44})/volume/history$")
POOL_PATH = re.compile(r"^/pools/([1-9A-HJ-NP-Za-km-z]{32,44})$")


class Replay:
    """The recorded sweeps in order, frozen on the last one after ``freeze_after``."""

    def __init__(self, series: Path, freeze_after: int) -> None:
        self._sweeps = sorted(path for path in series.glob("sweep_*.json"))
        if not self._sweeps:
            raise RuntimeError(f"no recorded sweeps in {series}")
        self._last_index = min(max(freeze_after, 1), len(self._sweeps)) - 1
        self._requests = 0
        self._served: Path | None = None
        self._lock = threading.Lock()
        recorded = (series / "history_at.txt").read_text(encoding="utf-8").strip()
        self._history_recorded_at = datetime.fromisoformat(recorded)
        self._series = series

    def next_sweep(self) -> bytes:
        with self._lock:
            index = min(self._requests, self._last_index)
            self._requests += 1
            self._served = self._sweeps[index]
            return self._served.read_bytes()

    def pool(self, address: str) -> dict[str, Any] | None:
        with self._lock:
            served = self._served or self._sweeps[0]
        document = json.loads(served.read_text(encoding="utf-8"))
        for item in document.get("data", []):
            if item.get("address") == address:
                return dict(item)
        return None

    def history(self, address: str, start: int | None, end: int | None) -> dict[str, Any]:
        recorded = self._series / f"history_{address}.json"
        now = int(datetime.now(UTC).timestamp())
        payload: dict[str, Any] = {
            "start_time": start if start is not None else now - 3 * 3600,
            "end_time": end if end is not None else now,
            "timeframe": "5m",
            "data": [],
        }
        if not recorded.is_file():
            return payload
        document = json.loads(recorded.read_text(encoding="utf-8"))
        then = int(self._history_recorded_at.timestamp())
        shift = (now // BUCKET_SECONDS - then // BUCKET_SECONDS) * BUCKET_SECONDS
        buckets = []
        for item in document.get("data", []):
            moved = dict(item)
            moved["timestamp"] = int(item["timestamp"]) + shift
            moved["timestamp_str"] = datetime.fromtimestamp(moved["timestamp"], UTC).isoformat()
            if start is not None and moved["timestamp"] < start:
                continue
            if end is not None and moved["timestamp"] + BUCKET_SECONDS > end:
                continue
            buckets.append(moved)
        payload["data"] = buckets
        return payload

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "requests": self._requests,
                "served": None if self._served is None else self._served.stem,
                "frozen": self._requests > self._last_index,
                "freeze_after": self._last_index + 1,
            }


def _integer(values: dict[str, list[str]], name: str) -> int | None:
    raw = values.get(name)
    if not raw:
        return None
    try:
        return int(raw[0])
    except ValueError:
        return None


def handler_for(replay: Replay) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MeteoraFixtureApi/1"

        def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == "/health":
                self._send(200, {"status": "ok"})
            elif url.path == "/state":
                self._send(200, replay.state())
            elif url.path == "/pools":
                self._send_bytes(200, replay.next_sweep())
            elif match := HISTORY_PATH.match(url.path):
                history = replay.history(
                    match.group(1), _integer(query, "start_time"), _integer(query, "end_time")
                )
                self._send(200, history)
            elif match := POOL_PATH.match(url.path):
                pool = replay.pool(match.group(1))
                if pool is None:
                    self._send(404, {"message": "pool not recorded"})
                else:
                    self._send(200, pool)
            else:
                self._send(404, {"message": "not found"})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # One short line per request on stdout: ``docker compose logs fixture-api``.
            print(f"fixture-api {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            self._send_bytes(status, json.dumps(payload).encode("utf-8"))

        def _send_bytes(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main() -> None:
    freeze_after = int(os.environ.get("FIXTURE_FREEZE_AFTER") or "6")
    port = int(os.environ.get("FIXTURE_PORT") or "8080")
    replay = Replay(SERIES, freeze_after)
    # Compose-internal only: the service has ``expose`` and no published port.
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_for(replay))  # noqa: S104
    print(f"fixture-api serving {SERIES} on :{port}, frozen after {freeze_after} sweeps")
    server.serve_forever()


if __name__ == "__main__":
    main()
