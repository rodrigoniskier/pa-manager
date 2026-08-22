from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from pa_manager.models import AccountConfig, Credentials

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=False)

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_UNSAFE_EXAMPLE_PASSWORDS = {
    "troque-por-uma-senha-longa-e-exclusiva",
}
_UNSAFE_EXAMPLE_SECRETS = {
    "gere-com-python-c-import-secrets-print-secrets-token-urlsafe-48",
}


class ConfigurationError(ValueError):
    pass


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} deve ser true ou false")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} deve ser um número inteiro") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} deve estar entre {minimum} e {maximum}")
    return value


def _resolve_path(raw: str, default: str) -> Path:
    path = Path(raw or default)
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(frozen=True, slots=True)
class Settings:
    accounts_file: Path = field(
        default_factory=lambda: _resolve_path(
            os.getenv("PAM_ACCOUNTS_FILE", ""), "config/accounts.yaml"
        )
    )
    db_path: Path = field(
        default_factory=lambda: _resolve_path(os.getenv("PAM_DB_PATH", ""), "data/pa_manager.db")
    )
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", True))
    allow_global_renew: bool = field(
        default_factory=lambda: _env_bool("PAM_ALLOW_GLOBAL_RENEW", False)
    )
    allow_reload: bool = field(default_factory=lambda: _env_bool("PAM_ALLOW_RELOAD", False))
    browser_fallback: bool = field(
        default_factory=lambda: _env_bool("PAM_BROWSER_FALLBACK", False)
    )
    secure_cookies: bool = field(
        default_factory=lambda: _env_bool("PAM_SECURE_COOKIES", False)
    )
    max_concurrent_accounts: int = field(
        default_factory=lambda: _env_int("MAX_CONCURRENT_ACCOUNTS", 3, minimum=1, maximum=5)
    )
    request_timeout_seconds: int = field(
        default_factory=lambda: _env_int(
            "PAM_REQUEST_TIMEOUT_SECONDS", 20, minimum=5, maximum=120
        )
    )
    health_timeout_seconds: int = field(
        default_factory=lambda: _env_int(
            "PAM_HEALTH_TIMEOUT_SECONDS", 12, minimum=3, maximum=60
        )
    )
    lock_ttl_seconds: int = field(
        default_factory=lambda: _env_int("PAM_LOCK_TTL_SECONDS", 1800, minimum=60, maximum=7200)
    )
    trusted_proxy_hops: int = field(
        default_factory=lambda: _env_int("PAM_TRUSTED_PROXY_HOPS", 0, minimum=0, maximum=5)
    )
    admin_username: str = field(default_factory=lambda: os.getenv("PAM_ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: os.getenv("PAM_ADMIN_PASSWORD", ""))
    session_secret: str = field(default_factory=lambda: os.getenv("PAM_SESSION_SECRET", ""))
    trusted_hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            item.strip()
            for item in os.getenv("PAM_TRUSTED_HOSTS", "localhost,127.0.0.1").split(",")
            if item.strip()
        )
    )
    allowed_real_accounts: frozenset[str] = field(
        default_factory=lambda: frozenset(
            item.strip()
            for item in os.getenv("PAM_ALLOWED_REAL_ACCOUNTS", "").split(",")
            if item.strip()
        )
    )
    log_level: str = field(default_factory=lambda: os.getenv("PAM_LOG_LEVEL", "INFO").upper())

    def validate_for_web(self) -> None:
        if (
            len(self.admin_password) < 12
            or self.admin_password in _UNSAFE_EXAMPLE_PASSWORDS
        ):
            raise ConfigurationError("PAM_ADMIN_PASSWORD deve ter ao menos 12 caracteres")
        if len(self.session_secret) < 32 or self.session_secret in _UNSAFE_EXAMPLE_SECRETS:
            raise ConfigurationError("PAM_SESSION_SECRET deve ter ao menos 32 caracteres")
        if not self.trusted_hosts:
            raise ConfigurationError("PAM_TRUSTED_HOSTS não pode ficar vazio")


def _validate_account(raw: dict[str, Any], index: int) -> AccountConfig:
    required = {"id", "label", "username", "domain", "password_env"}
    missing = sorted(required - raw.keys())
    if missing:
        raise ConfigurationError(f"Conta {index}: campos ausentes: {', '.join(missing)}")

    account_id = str(raw["id"]).strip()
    username = str(raw["username"]).strip()
    domain = str(raw["domain"]).strip()
    password_env = str(raw["password_env"]).strip()
    token_env = str(raw.get("token_env") or "").strip() or None
    host = str(raw.get("host") or "www.pythonanywhere.com").strip().lower()

    if not _ID_PATTERN.fullmatch(account_id):
        raise ConfigurationError(f"Conta {index}: id inválido")
    if not _USERNAME_PATTERN.fullmatch(username):
        raise ConfigurationError(f"Conta {account_id}: username inválido")
    if domain.lower() != f"{username.lower()}.pythonanywhere.com":
        raise ConfigurationError(
            f"Conta {account_id}: domínio deve corresponder ao username informado"
        )
    if not _ENV_PATTERN.fullmatch(password_env):
        raise ConfigurationError(f"Conta {account_id}: password_env inválido")
    if token_env and not _ENV_PATTERN.fullmatch(token_env):
        raise ConfigurationError(f"Conta {account_id}: token_env inválido")
    if host not in {"www.pythonanywhere.com", "eu.pythonanywhere.com"}:
        raise ConfigurationError(f"Conta {account_id}: host PythonAnywhere inválido")

    return AccountConfig(
        id=account_id,
        label=str(raw["label"]).strip(),
        username=username,
        domain=domain,
        password_env=password_env,
        token_env=token_env,
        host=host,
        enabled=bool(raw.get("enabled", True)),
        has_webapp=bool(raw.get("has_webapp", True)),
    )


def load_accounts(path: Path) -> list[AccountConfig]:
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Arquivo de contas não encontrado: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"YAML inválido em {path}") from exc

    raw_accounts = content.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ConfigurationError("A configuração deve conter uma lista accounts não vazia")
    accounts = [_validate_account(item, index) for index, item in enumerate(raw_accounts, 1)]
    enabled = [account for account in accounts if account.enabled]

    for attr in ("id", "domain", "password_env"):
        values = [str(getattr(account, attr)).lower() for account in enabled]
        if len(values) != len(set(values)):
            raise ConfigurationError(f"Há valores duplicados em accounts.{attr}")
    return enabled


class CredentialResolver:
    """Resolve secrets sem incorporá-los à configuração ou ao banco de dados."""

    def __init__(self, environment: dict[str, str] | None = None) -> None:
        self.environment = environment if environment is not None else os.environ
        self._json_credentials = self._load_json_credentials()

    def _load_json_credentials(self) -> dict[str, dict[str, str]]:
        raw = self.environment.get("PAM_CREDENTIALS_JSON", "").strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("PAM_CREDENTIALS_JSON contém JSON inválido") from exc
        if not isinstance(value, dict):
            raise ConfigurationError("PAM_CREDENTIALS_JSON deve ser um objeto JSON")
        return value

    def resolve(self, account: AccountConfig) -> Credentials:
        fallback = self._json_credentials.get(account.id, {})
        if not isinstance(fallback, dict):
            fallback = {}
        password = self.environment.get(account.password_env) or fallback.get("password")
        api_token = (
            self.environment.get(account.token_env, "") if account.token_env else ""
        ) or fallback.get("api_token")
        return Credentials(password=password or None, api_token=api_token or None)

    def configured(self, account: AccountConfig) -> bool:
        credentials = self.resolve(account)
        return credentials.can_login or credentials.can_use_api
