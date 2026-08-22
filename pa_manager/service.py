from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import date
from functools import partial
from typing import Any
from uuid import uuid4

from pa_manager.config import CredentialResolver, Settings
from pa_manager.dates import classify_expiry, days_remaining
from pa_manager.db import Repository
from pa_manager.models import (
    AccountConfig,
    AccountResult,
    Action,
    HealthResult,
    ProjectStatus,
    ResultStatus,
    RunStatus,
    RunSummary,
    utc_now,
)
from pa_manager.playwright_adapter import PlaywrightRenewalClient
from pa_manager.pythonanywhere import (
    AuthenticatedHttpClient,
    AuthenticationError,
    HealthChecker,
    InteractiveAuthRequired,
    InterfaceChangedError,
    PythonAnywhereError,
)

logger = logging.getLogger(__name__)
GLOBAL_LOCK = "pa-manager-operation"


class OperationInProgress(RuntimeError):
    pass


class ActionNotAllowed(RuntimeError):
    pass


ClientFactory = Callable[..., AuthenticatedHttpClient]
HealthFactory = Callable[..., HealthChecker]


class ManagerService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        accounts: Sequence[AccountConfig],
        *,
        credential_resolver: CredentialResolver | None = None,
        client_factory: ClientFactory = AuthenticatedHttpClient,
        health_factory: HealthFactory = HealthChecker,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.accounts = list(accounts)
        self.account_map = {account.id: account for account in accounts}
        self.credentials = credential_resolver or CredentialResolver()
        self.client_factory = client_factory
        self.health_factory = health_factory
        self._tasks: set[asyncio.Task[Any]] = set()

    def _select_accounts(self, account_ids: Sequence[str] | None) -> list[AccountConfig]:
        if account_ids is None:
            return list(self.accounts)
        unknown = sorted(set(account_ids) - self.account_map.keys())
        if unknown:
            raise ActionNotAllowed(f"Conta desconhecida: {', '.join(unknown)}")
        return [self.account_map[account_id] for account_id in account_ids]

    def _authorize_action(
        self,
        action: Action,
        accounts: Sequence[AccountConfig],
        dry_run: bool,
    ) -> None:
        if action is Action.VERIFY:
            return
        if action is Action.RELOAD:
            if self.settings.dry_run:
                raise ActionNotAllowed("Reload está bloqueado enquanto DRY_RUN=true.")
            if not self.settings.allow_reload:
                raise ActionNotAllowed("Reload bloqueado por PAM_ALLOW_RELOAD=false.")
            allowed = self.settings.allowed_real_accounts
            denied = [
                account.id
                for account in accounts
                if "*" not in allowed and account.id not in allowed
            ]
            if denied:
                raise ActionNotAllowed(
                    "Reload não autorizado para: " + ", ".join(denied)
                )
        if action is Action.RENEW and not dry_run:
            if self.settings.dry_run:
                raise ActionNotAllowed(
                    "Renovação real está bloqueada enquanto DRY_RUN=true."
                )
            if len(accounts) > 1 and not self.settings.allow_global_renew:
                raise ActionNotAllowed(
                    "Renovação global real bloqueada por PAM_ALLOW_GLOBAL_RENEW=false."
                )
            allowed = self.settings.allowed_real_accounts
            denied = [
                account.id
                for account in accounts
                if "*" not in allowed and account.id not in allowed
            ]
            if denied:
                raise ActionNotAllowed(
                    "Renovação real não autorizada para: " + ", ".join(denied)
                )

    def launch(
        self,
        action: Action,
        *,
        account_ids: Sequence[str] | None = None,
        dry_run: bool | None = None,
    ) -> str:
        accounts = self._select_accounts(account_ids)
        effective_dry_run = self.settings.dry_run if dry_run is None else dry_run
        if action is not Action.RENEW:
            effective_dry_run = False
        self._authorize_action(action, accounts, effective_dry_run)

        run_id = uuid4().hex
        if not self.repository.acquire_lock(
            GLOBAL_LOCK, run_id, self.settings.lock_ttl_seconds
        ):
            raise OperationInProgress("Já existe uma operação em andamento.")
        run = RunSummary(
            id=run_id,
            action=action,
            scope="all" if account_ids is None else ",".join(account_ids),
            dry_run=effective_dry_run,
            status=RunStatus.QUEUED,
            started_at=utc_now(),
            total=len(accounts),
        )
        try:
            self.repository.create_run(run)
        except Exception:
            self.repository.release_lock(GLOBAL_LOCK, run_id)
            raise
        task = asyncio.create_task(self._execute(run_id, action, accounts, effective_dry_run))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def run_now(
        self,
        action: Action,
        *,
        account_ids: Sequence[str] | None = None,
        dry_run: bool | None = None,
    ) -> str:
        accounts = self._select_accounts(account_ids)
        effective_dry_run = self.settings.dry_run if dry_run is None else dry_run
        if action is not Action.RENEW:
            effective_dry_run = False
        self._authorize_action(action, accounts, effective_dry_run)
        run_id = uuid4().hex
        if not self.repository.acquire_lock(
            GLOBAL_LOCK, run_id, self.settings.lock_ttl_seconds
        ):
            raise OperationInProgress("Já existe uma operação em andamento.")
        try:
            self.repository.create_run(
                RunSummary(
                    id=run_id,
                    action=action,
                    scope="all" if account_ids is None else ",".join(account_ids),
                    dry_run=effective_dry_run,
                    status=RunStatus.QUEUED,
                    started_at=utc_now(),
                    total=len(accounts),
                )
            )
        except Exception:
            self.repository.release_lock(GLOBAL_LOCK, run_id)
            raise
        await self._execute(run_id, action, accounts, effective_dry_run)
        return run_id

    async def _execute(
        self,
        run_id: str,
        action: Action,
        accounts: Sequence[AccountConfig],
        dry_run: bool,
    ) -> None:
        self.repository.set_run_running(run_id)
        semaphore = asyncio.Semaphore(self.settings.max_concurrent_accounts)
        heartbeat = asyncio.create_task(self._refresh_lock_until_cancelled(run_id))

        async def guarded(account: AccountConfig) -> None:
            async with semaphore:
                result = await self._process_account(account, action, dry_run)
                self.repository.add_result(run_id, result)

        logger.info(
            "operation_started",
            extra={"run_id": run_id, "action": action.value, "accounts": len(accounts)},
        )
        fatal_message: str | None = None
        try:
            await asyncio.gather(*(guarded(account) for account in accounts))
        except Exception:
            logger.exception("operation_fatal_error", extra={"run_id": run_id})
            fatal_message = "Falha interna ao coordenar a operação."
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            self.repository.finish_run(run_id, fatal_message=fatal_message)
            self.repository.release_lock(GLOBAL_LOCK, run_id)
            logger.info("operation_finished", extra={"run_id": run_id, "action": action.value})

    async def _refresh_lock_until_cancelled(self, run_id: str) -> None:
        interval = max(5.0, min(self.settings.lock_ttl_seconds / 3, 60.0))
        while True:
            await asyncio.sleep(interval)
            try:
                refreshed = self.repository.refresh_lock(
                    GLOBAL_LOCK,
                    run_id,
                    self.settings.lock_ttl_seconds,
                )
            except Exception:
                logger.exception("operation_lock_refresh_failed", extra={"run_id": run_id})
                continue
            if not refreshed:
                logger.error("operation_lock_lost", extra={"run_id": run_id})
                return

    async def _health(self, account: AccountConfig) -> HealthResult:
        checker = self.health_factory(timeout_seconds=self.settings.health_timeout_seconds)
        return await asyncio.to_thread(checker.check, account.public_url)

    def _client(self, account: AccountConfig) -> AuthenticatedHttpClient:
        return self.client_factory(
            account,
            self.credentials.resolve(account),
            timeout_seconds=self.settings.request_timeout_seconds,
        )

    async def _process_account(
        self,
        account: AccountConfig,
        action: Action,
        dry_run: bool,
    ) -> AccountResult:
        credentials = self.credentials.resolve(account)
        if not account.has_webapp:
            return self._result(
                account,
                action,
                ResultStatus.SKIPPED,
                ProjectStatus.NOT_APPLICABLE,
                "Conta sem web app cadastrado; esta operação não se aplica.",
                provider="configuration",
            )
        if action in {Action.VERIFY, Action.RENEW} and not credentials.can_login:
            health = await self._health(account)
            status = ProjectStatus.CONFIG_ERROR if health.online else ProjectStatus.OFFLINE
            result_status = ResultStatus.WARNING if action is Action.VERIFY else ResultStatus.FAILED
            return self._result(
                account,
                action,
                result_status,
                status,
                "Senha ainda não configurada; o health check público foi executado.",
                health=health,
            )
        if action is Action.RELOAD and not (credentials.can_use_api or credentials.can_login):
            return self._result(
                account,
                action,
                ResultStatus.FAILED,
                ProjectStatus.CONFIG_ERROR,
                "Configure API token ou senha antes de executar reload.",
            )

        try:
            if action is Action.VERIFY:
                return await self._verify(account)
            if action is Action.RENEW:
                return await self._renew(account, dry_run)
            return await self._reload(account)
        except InteractiveAuthRequired as exc:
            health = await self._health(account)
            return self._result(
                account,
                action,
                ResultStatus.WARNING,
                ProjectStatus.INTERACTIVE_AUTH,
                str(exc),
                health=health,
            )
        except AuthenticationError as exc:
            health = await self._health(account)
            return self._result(
                account,
                action,
                ResultStatus.FAILED,
                ProjectStatus.AUTH_ERROR,
                str(exc),
                health=health,
            )
        except PythonAnywhereError as exc:
            health = await self._health(account)
            status = (
                ProjectStatus.RENEWAL_ERROR
                if action is Action.RENEW
                else ProjectStatus.UNKNOWN
            )
            return self._result(
                account,
                action,
                ResultStatus.FAILED,
                status,
                str(exc),
                health=health,
            )
        except Exception:
            logger.exception(
                "unexpected_account_error",
                extra={"account_id": account.id, "action": action.value},
            )
            health = await self._health(account)
            return self._result(
                account,
                action,
                ResultStatus.FAILED,
                ProjectStatus.UNKNOWN,
                "Erro interno inesperado; consulte os logs pelo identificador da execução.",
                health=health,
            )

    async def _verify(self, account: AccountConfig) -> AccountResult:
        snapshot = await asyncio.to_thread(self._client(account).inspect)
        health = await self._health(account)
        message = "Status e data consultados; site respondeu normalmente."
        result_status = ResultStatus.SUCCESS
        project_status = snapshot.status
        if not health.online:
            result_status = ResultStatus.WARNING
            project_status = ProjectStatus.OFFLINE
            message = "Status consultado, mas o health check não confirmou disponibilidade."
        elif snapshot.expiry is None:
            result_status = ResultStatus.WARNING
            message = "Site online, mas a data de expiração não pôde ser lida."
        return self._result(
            account,
            Action.VERIFY,
            result_status,
            project_status,
            message,
            old_expiry=snapshot.expiry,
            days=snapshot.days_remaining,
            health=health,
        )

    async def _renew(self, account: AccountConfig, dry_run: bool) -> AccountResult:
        client = self._client(account)
        provider = client.provider_name
        try:
            outcome = await asyncio.to_thread(partial(client.renew, dry_run=dry_run))
        except InterfaceChangedError:
            if not self.settings.browser_fallback:
                raise
            fallback = PlaywrightRenewalClient(
                account,
                self.credentials.resolve(account),
                timeout_seconds=max(30, self.settings.request_timeout_seconds),
            )
            outcome = await asyncio.to_thread(partial(fallback.renew, dry_run=dry_run))
            provider = fallback.provider_name
        health = await self._health(account)

        if dry_run:
            found = outcome.before.has_extend_action
            message = (
                "Simulação concluída: a ação de renovação foi localizada; nenhum clique foi feito."
                if found
                else "Simulação concluída, mas a ação de renovação não foi localizada."
            )
            return self._result(
                account,
                Action.RENEW,
                ResultStatus.WOULD_RENEW if found else ResultStatus.WARNING,
                outcome.before.status,
                message,
                old_expiry=outcome.before.expiry,
                new_expiry=outcome.before.expiry,
                days=outcome.before.days_remaining,
                health=health,
                provider=provider,
            )

        if outcome.reactivated:
            message = "Web app reativado e nova data confirmada."
        elif outcome.changed:
            message = "Web app renovado e nova data confirmada."
        else:
            message = "A renovação foi aceita; a data já estava no limite máximo disponível."
        result_status = ResultStatus.SUCCESS if outcome.changed else ResultStatus.WARNING
        if not health.online:
            message += " O health check posterior não confirmou disponibilidade."
        return self._result(
            account,
            Action.RENEW,
            result_status,
            outcome.after.status,
            message,
            old_expiry=outcome.before.expiry,
            new_expiry=outcome.after.expiry,
            days=outcome.after.days_remaining,
            health=health,
            provider=provider,
        )

    async def _reload(self, account: AccountConfig) -> AccountResult:
        client = self._client(account)
        await asyncio.to_thread(client.reload)
        health = await self._health(account)
        return self._result(
            account,
            Action.RELOAD,
            ResultStatus.SUCCESS if health.online else ResultStatus.WARNING,
            ProjectStatus.ACTIVE if health.online else ProjectStatus.OFFLINE,
            (
                "Reload concluído; site respondeu normalmente."
                if health.online
                else "Reload concluído, mas o health check não confirmou disponibilidade."
            ),
            health=health,
            provider=client.provider_name,
        )

    @staticmethod
    def _result(
        account: AccountConfig,
        action: Action,
        result_status: ResultStatus,
        project_status: ProjectStatus,
        message: str,
        *,
        old_expiry: date | None = None,
        new_expiry: date | None = None,
        days: int | None = None,
        health: HealthResult | None = None,
        provider: str = "authenticated_http",
    ) -> AccountResult:
        effective_expiry = new_expiry or old_expiry
        if days is None and effective_expiry:
            days = days_remaining(effective_expiry)
        if project_status is ProjectStatus.UNKNOWN and effective_expiry:
            project_status = classify_expiry(effective_expiry)
        return AccountResult(
            account_id=account.id,
            username=account.username,
            domain=account.domain,
            action=action,
            result_status=result_status,
            project_status=project_status,
            message=message,
            occurred_at=utc_now(),
            old_expiry=old_expiry,
            new_expiry=new_expiry,
            days_remaining=days,
            health=health,
            provider=provider,
        )
