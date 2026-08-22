from datetime import date
from pathlib import Path
from typing import Any

import pytest

from pa_manager.config import CredentialResolver, Settings
from pa_manager.db import Repository
from pa_manager.models import (
    AccountConfig,
    Action,
    HealthResult,
    ProjectStatus,
    WebAppSnapshot,
    utc_now,
)
from pa_manager.pythonanywhere import AuthenticationError, RenewalOutcome
from pa_manager.service import ActionNotAllowed, ManagerService


def accounts() -> list[AccountConfig]:
    return [
        AccountConfig("good", "Good", "good", "good.pythonanywhere.com", "PA_GOOD_PASSWORD"),
        AccountConfig("bad", "Bad", "bad", "bad.pythonanywhere.com", "PA_BAD_PASSWORD"),
    ]


class FakeClient:
    provider_name = "fake"

    def __init__(self, account: AccountConfig, *_args: Any, **_kwargs: Any) -> None:
        self.account = account

    def renew(self, *, dry_run: bool) -> RenewalOutcome:
        if self.account.id == "bad":
            raise AuthenticationError("Login recusado.")
        snapshot = WebAppSnapshot(
            domain=self.account.domain,
            expiry=date(2026, 9, 22),
            raw_expiry="Tuesday 22 September 2026",
            days_remaining=31,
            status=ProjectStatus.ACTIVE,
            enabled=True,
            has_extend_action=True,
        )
        return RenewalOutcome(snapshot, snapshot, changed=False, reactivated=False)


class FakeHealthChecker:
    def __init__(self, **_kwargs: Any) -> None:
        pass

    def check(self, url: str) -> HealthResult:
        return HealthResult(True, 200, 10, utc_now(), final_url=url)


async def test_one_account_failure_does_not_stop_others(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "manager.db")
    repository.initialize()
    settings = Settings(
        db_path=tmp_path / "manager.db",
        dry_run=True,
        admin_password="a-very-long-password",
        session_secret="x" * 48,
    )
    resolver = CredentialResolver(
        {"PA_GOOD_PASSWORD": "secret", "PA_BAD_PASSWORD": "secret"}
    )
    service = ManagerService(
        settings,
        repository,
        accounts(),
        credential_resolver=resolver,
        client_factory=FakeClient,
        health_factory=FakeHealthChecker,
    )
    run_id = await service.run_now(Action.RENEW, dry_run=True)
    data = repository.get_run(run_id)
    assert data is not None
    assert data["run"]["total"] == 2
    assert data["run"]["succeeded"] == 1
    assert data["run"]["failed"] == 1
    assert {item["account_id"] for item in data["results"]} == {"good", "bad"}


async def test_missing_credentials_are_reported_without_client_call(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "manager.db")
    repository.initialize()
    settings = Settings(db_path=tmp_path / "manager.db", dry_run=True)
    service = ManagerService(
        settings,
        repository,
        [accounts()[0]],
        credential_resolver=CredentialResolver({}),
        client_factory=FakeClient,
        health_factory=FakeHealthChecker,
    )
    run_id = await service.run_now(Action.RENEW, dry_run=True)
    data = repository.get_run(run_id)
    assert data is not None
    assert data["results"][0]["project_status"] == ProjectStatus.CONFIG_ERROR.value
    assert data["results"][0]["result_status"] == "failed"


async def test_real_renewal_cannot_override_global_dry_run(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "manager.db")
    repository.initialize()
    settings = Settings(
        db_path=tmp_path / "manager.db",
        dry_run=True,
        allowed_real_accounts=frozenset({"good"}),
    )
    service = ManagerService(settings, repository, [accounts()[0]])
    with pytest.raises(ActionNotAllowed, match="DRY_RUN=true"):
        await service.run_now(Action.RENEW, account_ids=["good"], dry_run=False)


async def test_reload_requires_explicit_enable_flag(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "manager.db")
    repository.initialize()
    settings = Settings(
        db_path=tmp_path / "manager.db",
        dry_run=False,
        allow_reload=False,
        allowed_real_accounts=frozenset({"good"}),
    )
    service = ManagerService(settings, repository, [accounts()[0]])
    with pytest.raises(ActionNotAllowed, match="PAM_ALLOW_RELOAD=false"):
        await service.run_now(Action.RELOAD, account_ids=["good"])
