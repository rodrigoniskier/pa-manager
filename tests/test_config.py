import json
from pathlib import Path

import pytest

from pa_manager.config import ConfigurationError, CredentialResolver, Settings, load_accounts

ROOT = Path(__file__).resolve().parents[1]


def test_real_account_configuration_contains_18_unique_projects() -> None:
    accounts = load_accounts(ROOT / "config/accounts.yaml")
    assert len(accounts) == 18
    assert len({account.id for account in accounts}) == 18
    assert len({account.domain.lower() for account in accounts}) == 18
    assert accounts[0].domain == "rodrigoniskier.pythonanywhere.com"
    assert accounts[-1].domain == "monografiaspn.pythonanywhere.com"
    pathology = next(account for account in accounts if account.id == "patologiadigital")
    assert pathology.has_webapp is False


def test_credentials_can_come_from_individual_env_or_single_json_secret() -> None:
    account = load_accounts(ROOT / "config/accounts.yaml")[0]
    resolver = CredentialResolver(
        {
            account.password_env: "individual-password",
            "PAM_CREDENTIALS_JSON": json.dumps(
                {account.id: {"password": "json-password", "api_token": "json-token"}}
            ),
        }
    )
    credentials = resolver.resolve(account)
    assert credentials.password == "individual-password"
    assert credentials.api_token == "json-token"


def test_invalid_json_secret_fails_closed() -> None:
    with pytest.raises(ConfigurationError, match="JSON inválido"):
        CredentialResolver({"PAM_CREDENTIALS_JSON": "{"})


def test_duplicate_account_ids_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "accounts.yaml"
    config.write_text(
        """
accounts:
  - id: same
    label: One
    username: one
    domain: one.pythonanywhere.com
    password_env: PA_ONE_PASSWORD
  - id: same
    label: Two
    username: two
    domain: two.pythonanywhere.com
    password_env: PA_TWO_PASSWORD
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicados"):
        load_accounts(config)


def test_web_rejects_published_example_secrets() -> None:
    settings = Settings(
        admin_password="troque-por-uma-senha-longa-e-exclusiva",
        session_secret="gere-com-python-c-import-secrets-print-secrets-token-urlsafe-48",
    )
    with pytest.raises(ConfigurationError):
        settings.validate_for_web()
