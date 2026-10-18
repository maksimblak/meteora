from pathlib import Path

import pytest

from src.core.config import AppConfig
from src.domain.models.trading import TradingMode, restore_trading_mode


def test_config_is_strict_redacts_secrets_and_recovers_in_shadow(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("schema_version: 1\napp_env: test\n", encoding="utf-8")
    settings = AppConfig.load(
        yaml_path=config_file,
        environ={
            "APP_OPERATOR_TOKEN": "operator-secret",
            "APP_SESSION_SECRET": "session-secret",
            "DATABASE_URL": "postgresql+asyncpg://app:password@postgres/app",
            "TRADING_MODE": "LIVE",
        },
    )

    rendered = str(settings.redacted())
    assert "operator-secret" not in rendered
    assert "session-secret" not in rendered
    assert settings.requested_mode is TradingMode.LIVE
    assert restore_trading_mode(None) is TradingMode.SHADOW

    config_file.write_text("schema_version: 1\nunknown_setting: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown_setting"):
        AppConfig.load(yaml_path=config_file, environ={})


def test_database_secret_can_be_loaded_from_docker_secret_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "database-url"
    database_url = "postgresql+asyncpg://app:password@postgres/app"
    secret_file.write_text(database_url, encoding="utf-8")

    settings = AppConfig.load(environ={"DATABASE_URL_FILE": str(secret_file)})

    assert settings.database_dsn() == database_url
    assert database_url not in str(settings.redacted())

    with pytest.raises(ValueError, match="either DATABASE_URL or DATABASE_URL_FILE"):
        AppConfig.load(
            environ={
                "DATABASE_URL": database_url,
                "DATABASE_URL_FILE": str(secret_file),
            }
        )
