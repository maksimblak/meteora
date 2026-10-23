"""``python -m gmgn_service``: the internal service on ``GMGN_HOST:GMGN_PORT``."""

import logging
import os

import uvicorn

from gmgn_service.app import create_app
from gmgn_service.config import ServiceSettings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = ServiceSettings.from_environment(os.environ)
    uvicorn.run(create_app(settings=settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
