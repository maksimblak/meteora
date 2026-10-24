"""Strict, versioned configuration with env-only secret ingestion."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from src.core.secrets import CREDENTIAL_URL_FIELDS, SECRETS, distinct, url_credentials
from src.domain.models.trading import TradingMode


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    app_env: Literal["development", "test", "production"] = "development"
    app_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    app_timezone: str = "UTC"
    database_url: SecretStr | None = None
    meteora_data_api_base_url: str | None = None
    solana_rpc_url: str | None = None
    solana_ws_url: str | None = None
    wallet_public_key: str | None = None
    wallet_keypair_file: SecretStr | None = None
    jito_block_engine_url: str | None = None
    telegram_chat_id: str | None = None
    requested_mode: TradingMode = TradingMode.SHADOW
    operator_token: SecretStr | None = None
    session_secret: SecretStr | None = None
    helius_api_key: SecretStr | None = None
    executor_internal_token: SecretStr | None = None
    executor_mode_token: SecretStr | None = None
    executor_approval_secret: SecretStr | None = None
    gmgn_api_key: SecretStr | None = None
    gmgn_bridge_token: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None

    _ENV_FIELDS: ClassVar[dict[str, str]] = {
        "APP_ENV": "app_env",
        "APP_LOG_LEVEL": "app_log_level",
        "APP_TIMEZONE": "app_timezone",
        "METEORA_DATA_API_BASE_URL": "meteora_data_api_base_url",
        "SOLANA_RPC_URL": "solana_rpc_url",
        "SOLANA_WS_URL": "solana_ws_url",
        "WALLET_PUBLIC_KEY": "wallet_public_key",
        "WALLET_KEYPAIR_FILE": "wallet_keypair_file",
        "JITO_BLOCK_ENGINE_URL": "jito_block_engine_url",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
        "TRADING_MODE": "requested_mode",
    }
    # The secret registry (``src.core.secrets``) is the only list of secret settings.
    _SECRET_ENV_FIELDS: ClassVar[dict[str, str]] = {
        setting.env: setting.field for setting in SECRETS if setting.file_variant
    }
    _SECRET_FIELD_NAMES: ClassVar[frozenset[str]] = frozenset(
        setting.field for setting in SECRETS
    )

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("app_timezone must be an IANA timezone") from error
        return value

    @classmethod
    def load(
        cls,
        *,
        yaml_path: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "AppConfig":
        source_environment = os.environ if environ is None else environ
        values: dict[str, Any] = {}

        if yaml_path is not None and yaml_path.exists():
            loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError("configuration root must be a mapping")
            secret_keys = cls._SECRET_FIELD_NAMES.intersection(loaded)
            if secret_keys:
                names = ", ".join(sorted(secret_keys))
                raise ValueError(f"secret fields are not allowed in YAML: {names}")
            values.update(loaded)

        for environment_name, field_name in cls._ENV_FIELDS.items():
            raw_value = source_environment.get(environment_name)
            if raw_value:
                values[field_name] = (
                    TradingMode(raw_value) if field_name == "requested_mode" else raw_value
                )

        for environment_name, field_name in cls._SECRET_ENV_FIELDS.items():
            value = cls._read_secret(environment_name, source_environment)
            if value is not None:
                values[field_name] = value

        return cls.model_validate(values)

    @staticmethod
    def _read_secret(name: str, environ: Mapping[str, str]) -> str | None:
        direct = environ.get(name)
        file_name = environ.get(f"{name}_FILE")
        if direct and file_name:
            raise ValueError(f"set either {name} or {name}_FILE, not both")
        if direct:
            return direct
        if file_name:
            value = Path(file_name).read_text(encoding="utf-8").strip()
            if not value:
                raise ValueError(f"{name}_FILE contains an empty secret")
            return value
        return None

    def redacted(self) -> dict[str, Any]:
        output = self.model_dump(mode="json")
        for field_name in self._SECRET_FIELD_NAMES:
            if output.get(field_name) is not None:
                output[field_name] = "[REDACTED]"
        return output

    def secret_values(self) -> tuple[str, ...]:
        """Every value the logs must never contain: each registry secret, the password of
        ``DATABASE_URL`` and the key-like parts of provider URLs."""

        values: list[str] = []
        for setting in SECRETS:
            secret = getattr(self, setting.field)
            if isinstance(secret, SecretStr):
                values.append(secret.get_secret_value())
        database = self.database_dsn()
        if database:
            values.extend(url_credentials(database))
        for field_name in CREDENTIAL_URL_FIELDS:
            url = getattr(self, field_name)
            if url:
                values.extend(url_credentials(url))
        return distinct(values)

    def database_dsn(self) -> str | None:
        return self.database_url.get_secret_value() if self.database_url else None
