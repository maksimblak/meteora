"""``python -m src.worker`` - the scheduled worker process (shadow-only by default)."""

import asyncio

from src.worker.runtime import run_worker

if __name__ == "__main__":
    asyncio.run(run_worker())
