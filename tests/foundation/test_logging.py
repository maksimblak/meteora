import io
import json
import logging
from uuid import uuid4

import structlog

from src.core.config import AppConfig
from src.core.logging import configure_logging


def test_python_logging_is_json_and_redacts_configured_secrets() -> None:
    secret = f"fixture-{uuid4()}"
    settings = AppConfig.load(environ={"APP_OPERATOR_TOKEN": secret})
    stream = io.StringIO()
    configure_logging(settings, stream=stream)

    logging.getLogger("stdlib-test").warning("operator=%s", secret)
    structlog.get_logger("structlog-test").info("configured", api_key=secret)

    output = stream.getvalue()
    records = [json.loads(line) for line in output.splitlines()]
    assert len(records) == 2
    assert secret not in output
    assert all(record["level"] in {"info", "warning"} for record in records)
    assert "[REDACTED]" in output
