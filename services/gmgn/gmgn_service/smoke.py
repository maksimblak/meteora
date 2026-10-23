"""Manual live smoke: one visit of a real token page, printed as JSON (never run by tests).

    python -m gmgn_service.smoke <mint> [--chain sol] [--page-timeout 45] [--record DIR]

``--record`` writes the page's captured answers of the metric endpoints (URL path without
the query, status, JSON body) to ``DIR/<mint>.json`` as raw material for test fixtures;
trim and check them before committing anything.
"""

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path

from gmgn_service.config import ServiceSettings
from gmgn_service.page import BrowserPageFetcher, Visit


async def visit_once(mint: str, chain: str, page_timeout: float) -> Visit:
    fetcher = BrowserPageFetcher(replace(ServiceSettings(), page_timeout=page_timeout))
    try:
        return await fetcher.visit(chain, mint)
    finally:
        await fetcher.close()


def summary(visit: Visit) -> dict[str, object]:
    extraction = visit.extraction
    return {
        "url": visit.url,
        "state": visit.state.value,
        "title": visit.title,
        "reason": visit.reason,
        "elapsed_seconds": round(visit.elapsed, 1),
        "captured": [f"{item.status} {item.method} {item.path}" for item in visit.captured],
        "signals": None
        if extraction is None
        else {
            name: None if value is None else str(value)
            for name, value in extraction.signals.items()
        },
        "missing": None if extraction is None else list(extraction.missing),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="One live visit of a gmgn.ai token page.")
    parser.add_argument("mint")
    parser.add_argument("--chain", default="sol")
    parser.add_argument("--page-timeout", type=float, default=45.0)
    parser.add_argument("--record", type=Path)
    arguments = parser.parse_args()
    visit = asyncio.run(visit_once(arguments.mint, arguments.chain, arguments.page_timeout))
    if arguments.record is not None:
        arguments.record.mkdir(parents=True, exist_ok=True)
        captured = [
            {"method": item.method, "path": item.path, "status": item.status, "body": item.body}
            for item in visit.captured
        ]
        (arguments.record / f"{arguments.mint}.json").write_text(
            json.dumps(captured, indent=1, default=str), encoding="utf-8"
        )
    print(json.dumps(summary(visit), indent=2, ensure_ascii=False))  # noqa: T201 - CLI output


if __name__ == "__main__":
    main()
