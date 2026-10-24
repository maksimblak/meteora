"""T21 (G25): the browser bridge's shared token is a registry secret (spec 8.3).

Like every other secret it is masked in the effective configuration, removed from every log
record and named - never shown - on ``/settings``; only the worker's container receives it.
"""

import json
from pathlib import Path

from src.core.config import AppConfig
from src.core.secrets import SECRETS
from tests.api.support import make_panel
from tests.foundation.test_f3_secrets import logged, secret_setting

NAME = "GMGN_BRIDGE_TOKEN"  # noqa: S105 - a variable name


def test_the_bridge_token_is_registered_for_the_worker_only() -> None:
    (setting,) = [item for item in SECRETS if item.env == NAME]

    assert setting.used_by == ("worker",)
    assert setting.file_variant


def test_the_bridge_token_never_reaches_a_log_or_the_settings_page(tmp_path: Path) -> None:
    environ, secret = secret_setting(NAME)

    output = logged(environ, secret)
    redacted = AppConfig.load(environ=environ).redacted()
    panel = make_panel(tmp_path, environ=environ)
    try:
        page = panel.get("/settings")
        effective = panel.get("/api/v1/config/effective")
    finally:
        panel.repositories.close()

    assert secret not in output
    assert secret not in json.dumps(redacted) and "[REDACTED]" in redacted.values()
    names = {item["name"]: item["configured"] for item in effective.json()["secrets"]}
    assert names[NAME] is True
    assert NAME in page.text and secret not in page.text and secret not in effective.text
