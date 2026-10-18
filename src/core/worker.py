"""Safe idle worker process used until pipeline jobs are registered."""

import asyncio

from src.core.config import AppConfig
from src.core.logging import configure_logging


async def run_worker() -> None:
    settings = AppConfig.load()
    configure_logging(settings)
    stop = asyncio.Event()
    await stop.wait()


if __name__ == "__main__":
    asyncio.run(run_worker())
