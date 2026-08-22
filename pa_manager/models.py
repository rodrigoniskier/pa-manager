from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    AUTH_ERROR = "auth_error"
    INTERACTIVE_AUTH = "interactive_auth"
    CONFIG_ERROR = "config_error"
    RENEWAL_ERROR = "renewal_error"
    OFFLINE = "offline"


class ResultStatus(StrEnum):
    SUCCESS = "success"
    WARNING = "warning"
    FAILED = "failed"
    WOULD_RENEW = "would_renew"
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class Action(StrEnum):
    VERIFY = "verify"
    RENEW = "renew"
    RELOAD = "reload"


@dataclass(frozen=True, slots=True)
class AccountConfig:
    id: str
    label: str
    username: str
    domain: str
    password_env: str
    token_env: str | None = None
    host: str = "www.pythonanywhere.com"
    enabled: bool = True

    @property
    def public_url(self) -> str:
        return f"https://{self.domain.lower()}"


@dataclass(frozen=True, slots=True)
class Credentials:
    password: str | None = None
    api_token: str | None = None

    @property
    def can_login(self) -> bool:
        return bool(self.password)

    @property
    def can_use_api(self) -> bool:
        return bool(self.api_token)


@dataclass(slots=True)
class WebAppSnapshot:
    domain: str
    expiry: date | None
    raw_expiry: str | None
    days_remaining: int | None
    status: ProjectStatus
    enabled: bool | None = None
    has_extend_action: bool = False


@dataclass(slots=True)
class HealthResult:
    online: bool
    status_code: int | None
    response_ms: int | None
    checked_at: datetime
    final_url: str | None = None
    redirect_count: int = 0
    error: str | None = None


@dataclass(slots=True)
class AccountResult:
    account_id: str
    username: str
    domain: str
    action: Action
    result_status: ResultStatus
    project_status: ProjectStatus
    message: str
    occurred_at: datetime
    old_expiry: date | None = None
    new_expiry: date | None = None
    days_remaining: int | None = None
    health: HealthResult | None = None
    provider: str = "authenticated_http"

    def to_record(self) -> dict[str, Any]:
        data = asdict(self)
        data["action"] = self.action.value
        data["result_status"] = self.result_status.value
        data["project_status"] = self.project_status.value
        data["occurred_at"] = self.occurred_at.isoformat()
        data["old_expiry"] = self.old_expiry.isoformat() if self.old_expiry else None
        data["new_expiry"] = self.new_expiry.isoformat() if self.new_expiry else None
        if self.health:
            data["health"]["checked_at"] = self.health.checked_at.isoformat()
        return data


@dataclass(slots=True)
class RunSummary:
    id: str
    action: Action
    scope: str
    dry_run: bool
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    total: int = 0
    succeeded: int = 0
    warnings: int = 0
    failed: int = 0
    message: str | None = None
