"""The one registry of secret settings (spec 8.3).

``AppConfig`` reads every entry (``NAME`` or ``NAME_FILE``, never both) and masks it in
``redacted()``; the log redactor removes each value from every record; ``/settings``
lists each name by presence only. A new secret is added here and nowhere else.

URL settings are not secrets as a whole, but a provider key often rides in them (an
``api-key`` query, a token path segment, ``user:password@``); ``CREDENTIAL_URL_FIELDS``
names those settings, and the redactor removes such parts of them as well.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit


@dataclass(frozen=True, slots=True)
class SecretSetting:
    env: str
    field: str
    # Services whose container receives the value (docker-compose); empty = not wired yet.
    used_by: tuple[str, ...]
    # ``NAME_FILE`` may hold the value instead; ``WALLET_KEYPAIR_FILE`` is a path itself.
    file_variant: bool = True


SECRETS: tuple[SecretSetting, ...] = (
    SecretSetting("DATABASE_URL", "database_url", ("api", "worker")),
    SecretSetting("APP_OPERATOR_TOKEN", "operator_token", ("api",)),
    SecretSetting("APP_SESSION_SECRET", "session_secret", ("api",)),
    SecretSetting("HELIUS_API_KEY", "helius_api_key", ("worker",)),
    SecretSetting("EXECUTOR_INTERNAL_TOKEN", "executor_internal_token", ("api", "worker")),
    SecretSetting("EXECUTOR_MODE_TOKEN", "executor_mode_token", ("worker",)),
    SecretSetting("EXECUTOR_APPROVAL_SECRET", "executor_approval_secret", ("api", "worker")),
    SecretSetting("GMGN_API_KEY", "gmgn_api_key", ("worker",)),
    # The shared token of the GMGN browser bridge on the host (G25): worker and bridge only.
    SecretSetting("GMGN_BRIDGE_TOKEN", "gmgn_bridge_token", ("worker",)),
    # The optional free key of Jupiter Tokens API v2 (T22): twice the keyless rate.
    SecretSetting("JUPITER_API_KEY", "jupiter_api_key", ("worker",)),
    SecretSetting("TELEGRAM_BOT_TOKEN", "telegram_bot_token", ()),
    SecretSetting("WALLET_KEYPAIR_FILE", "wallet_keypair_file", ("executor",), file_variant=False),
)

CREDENTIAL_URL_FIELDS: tuple[str, ...] = (
    "solana_rpc_url",
    "solana_ws_url",
    "meteora_data_api_base_url",
    "jito_block_engine_url",
)

# A derived credential shorter than this is not removed from logs: it would erase common
# words, while a real key, token or password is longer.
_MINIMUM_DERIVED = 8
# A path segment this long in a provider URL is a key (QuickNode, Alchemy style).
_MINIMUM_PATH_KEY = 20


def url_credentials(url: str) -> tuple[str, ...]:
    """The parts of a provider URL that may be a key: query values, password, long path."""

    try:
        parts = urlsplit(url)
        password = parts.password
    except ValueError:
        return (url,)
    found: list[str] = [value for _, value in parse_qsl(parts.query, keep_blank_values=False)]
    if password:
        found.append(password)
    found.extend(
        segment for segment in parts.path.split("/") if len(segment) >= _MINIMUM_PATH_KEY
    )
    return tuple(value for value in found if len(value) >= _MINIMUM_DERIVED)


def distinct(values: Iterable[str]) -> tuple[str, ...]:
    """Non-empty values, longest first, so a value inside another is removed after it."""

    return tuple(sorted({value for value in values if value}, key=len, reverse=True))
