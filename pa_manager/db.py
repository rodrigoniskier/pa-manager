from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pa_manager.models import AccountResult, RunStatus, RunSummary, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    scope TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    succeeded INTEGER NOT NULL DEFAULT 0,
    warnings INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    username TEXT NOT NULL,
    domain TEXT NOT NULL,
    action TEXT NOT NULL,
    result_status TEXT NOT NULL,
    project_status TEXT NOT NULL,
    message TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    old_expiry TEXT,
    new_expiry TEXT,
    days_remaining INTEGER,
    health_online INTEGER,
    http_status INTEGER,
    response_ms INTEGER,
    final_url TEXT,
    redirect_count INTEGER,
    health_error TEXT,
    provider TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_results_account_date ON results(account_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS project_state (
    account_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    domain TEXT NOT NULL,
    project_status TEXT NOT NULL,
    expiry TEXT,
    days_remaining INTEGER,
    last_checked_at TEXT NOT NULL,
    last_action TEXT NOT NULL,
    last_result TEXT NOT NULL,
    last_message TEXT NOT NULL,
    health_online INTEGER,
    http_status INTEGER,
    response_ms INTEGER
);

CREATE TABLE IF NOT EXISTS operation_locks (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


class Repository:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            connection.execute(
                """
                UPDATE runs
                   SET status = ?, finished_at = ?, message = ?
                 WHERE status IN (?, ?)
                """,
                (
                    RunStatus.INTERRUPTED.value,
                    utc_now().isoformat(),
                    "Execução interrompida antes da conclusão.",
                    RunStatus.QUEUED.value,
                    RunStatus.RUNNING.value,
                ),
            )

    def create_run(self, run: RunSummary) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, action, scope, dry_run, status, started_at, total, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.action.value,
                    run.scope,
                    int(run.dry_run),
                    run.status.value,
                    run.started_at.isoformat(),
                    run.total,
                    run.message,
                ),
            )

    def set_run_running(self, run_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE runs SET status = ? WHERE id = ?",
                (RunStatus.RUNNING.value, run_id),
            )

    def add_result(self, run_id: str, result: AccountResult) -> None:
        health = result.health
        expiry = result.new_expiry or result.old_expiry
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO results (
                        run_id, account_id, username, domain, action, result_status,
                        project_status, message, occurred_at, old_expiry, new_expiry,
                        days_remaining, health_online, http_status, response_ms,
                        final_url, redirect_count, health_error, provider
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        result.account_id,
                        result.username,
                        result.domain,
                        result.action.value,
                        result.result_status.value,
                        result.project_status.value,
                        result.message,
                        result.occurred_at.isoformat(),
                        result.old_expiry.isoformat() if result.old_expiry else None,
                        result.new_expiry.isoformat() if result.new_expiry else None,
                        result.days_remaining,
                        int(health.online) if health else None,
                        health.status_code if health else None,
                        health.response_ms if health else None,
                        health.final_url if health else None,
                        health.redirect_count if health else None,
                        health.error if health else None,
                        result.provider,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO project_state (
                        account_id, username, domain, project_status, expiry,
                        days_remaining, last_checked_at, last_action, last_result,
                        last_message, health_online, http_status, response_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        username = excluded.username,
                        domain = excluded.domain,
                        project_status = excluded.project_status,
                        expiry = excluded.expiry,
                        days_remaining = excluded.days_remaining,
                        last_checked_at = excluded.last_checked_at,
                        last_action = excluded.last_action,
                        last_result = excluded.last_result,
                        last_message = excluded.last_message,
                        health_online = excluded.health_online,
                        http_status = excluded.http_status,
                        response_ms = excluded.response_ms
                    """,
                    (
                        result.account_id,
                        result.username,
                        result.domain,
                        result.project_status.value,
                        expiry.isoformat() if expiry else None,
                        result.days_remaining,
                        result.occurred_at.isoformat(),
                        result.action.value,
                        result.result_status.value,
                        result.message,
                        int(health.online) if health else None,
                        health.status_code if health else None,
                        health.response_ms if health else None,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def finish_run(self, run_id: str, *, fatal_message: str | None = None) -> None:
        with self.connect() as connection:
            counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN result_status IN ('success', 'would_renew') THEN 1 ELSE 0 END)
                        AS succeeded,
                    SUM(CASE WHEN result_status IN ('warning', 'skipped') THEN 1 ELSE 0 END)
                        AS warnings,
                    SUM(CASE WHEN result_status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM results WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            failed = int(counts["failed"] or 0)
            if fatal_message:
                status = RunStatus.FAILED
            elif failed:
                status = RunStatus.COMPLETED_WITH_ERRORS
            else:
                status = RunStatus.COMPLETED
            connection.execute(
                """
                UPDATE runs
                   SET status = ?, finished_at = ?, total = ?, succeeded = ?,
                       warnings = ?, failed = ?, message = COALESCE(?, message)
                 WHERE id = ?
                """,
                (
                    status.value,
                    utc_now().isoformat(),
                    int(counts["total"] or 0),
                    int(counts["succeeded"] or 0),
                    int(counts["warnings"] or 0),
                    failed,
                    fatal_message,
                    run_id,
                ),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return None
            results = connection.execute(
                "SELECT * FROM results WHERE run_id = ? ORDER BY occurred_at, id",
                (run_id,),
            ).fetchall()
        return {"run": dict(row), "results": [dict(result) for result in results]}

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 100))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (safe_limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_project_states(self) -> dict[str, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM project_state").fetchall()
        return {row["account_id"]: dict(row) for row in rows}

    def acquire_lock(self, name: str, owner: str, ttl_seconds: int) -> bool:
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl_seconds)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM operation_locks WHERE name = ? AND expires_at <= ?",
                    (name, now.isoformat()),
                )
                connection.execute(
                    """
                    INSERT INTO operation_locks (name, owner, acquired_at, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (name, owner, now.isoformat(), expires.isoformat()),
                )
                connection.execute("COMMIT")
                return True
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                return False
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def release_lock(self, name: str, owner: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM operation_locks WHERE name = ? AND owner = ?", (name, owner)
            )

    def refresh_lock(self, name: str, owner: str, ttl_seconds: int) -> bool:
        expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE operation_locks
                   SET expires_at = ?
                 WHERE name = ? AND owner = ?
                """,
                (expires.isoformat(), name, owner),
            )
        return cursor.rowcount == 1

    def get_lock(self, name: str) -> dict[str, Any] | None:
        now = utc_now().isoformat()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM operation_locks WHERE name = ? AND expires_at > ?",
                (name, now),
            ).fetchone()
        return dict(row) if row else None
