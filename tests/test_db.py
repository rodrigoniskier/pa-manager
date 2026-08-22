from datetime import timedelta
from pathlib import Path

from pa_manager.db import Repository
from pa_manager.models import Action, RunStatus, RunSummary, utc_now


def test_operation_lock_is_atomic_and_releasable(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "manager.db")
    repository.initialize()
    assert repository.acquire_lock("global", "owner-a", 60)
    assert not repository.acquire_lock("global", "owner-b", 60)
    repository.release_lock("global", "owner-a")
    assert repository.acquire_lock("global", "owner-b", 60)


def test_operation_lock_lease_can_only_be_refreshed_by_owner(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "manager.db")
    repository.initialize()
    assert repository.acquire_lock("global", "owner-a", 60)
    before = repository.get_lock("global")
    assert before is not None
    assert not repository.refresh_lock("global", "owner-b", 120)
    assert repository.refresh_lock("global", "owner-a", 120)
    after = repository.get_lock("global")
    assert after is not None
    assert after["expires_at"] > before["expires_at"]


def test_expired_lock_can_be_reclaimed(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "manager.db")
    repository.initialize()
    with repository.connect() as connection:
        old = utc_now() - timedelta(minutes=10)
        connection.execute(
            "INSERT INTO operation_locks VALUES (?, ?, ?, ?)",
            ("global", "stale", old.isoformat(), old.isoformat()),
        )
    assert repository.acquire_lock("global", "fresh", 60)


def test_initialize_marks_abandoned_runs_interrupted(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "manager.db")
    repository.initialize()
    repository.create_run(
        RunSummary(
            id="run-1",
            action=Action.VERIFY,
            scope="all",
            dry_run=False,
            status=RunStatus.RUNNING,
            started_at=utc_now(),
        )
    )
    repository.initialize()
    data = repository.get_run("run-1")
    assert data is not None
    assert data["run"]["status"] == RunStatus.INTERRUPTED.value
