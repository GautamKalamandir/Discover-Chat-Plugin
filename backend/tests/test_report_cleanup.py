import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db.models import User
from app.db.repositories.chat import ChatRepository
from app.db.repositories.reports import ReportRepository
from app.reports.cleanup import REPORT_CLEANUP_LOCK_KEY, ReportCleanupResult, run_report_cleanup
from app.reports.store import LocalReportStore
from app.retention import scheduler as scheduler_module
from app.retention.cleanup import run_cleanup
from app.retention.scheduler import ReportCleanupScheduler, seconds_until
from tests.db import BACKEND_DIR
from tests.test_repositories import make_user

pytestmark = pytest.mark.integration

TODAY = date(2026, 10, 13)
IST = ZoneInfo("Asia/Kolkata")


def settings(retention_days: int = 7, timeout: int = 600) -> Settings:
    return Settings(
        _env_file=None,
        report_retention_days=retention_days,
        report_build_timeout_seconds=timeout,
    )


async def count(db: AsyncSession, table: str) -> int:
    return int(await db.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608


async def stored_report(
    db: AsyncSession, store: LocalReportStore, owner: User, *, age_days: float
) -> uuid.UUID:
    """A ready report with both files, built `age_days` ago."""
    repo = ReportRepository(db, retention_days=7, build_timeout_seconds=600)
    report, _ = await repo.start_build(owner, TODAY)
    json_path = await store.write(owner.id, report.id, "report.json", b"{}")
    pdf_path = await store.write(owner.id, report.id, "report.pdf", b"%PDF")
    await repo.mark_ready(
        report.id,
        models=[],
        headline="h",
        suggestion_counts={},
        json_path=json_path,
        pdf_path=pdf_path,
    )
    await db.execute(
        text("UPDATE summary_reports SET created_at = now() - CAST(:d AS interval) WHERE id = :id"),
        {"d": timedelta(days=age_days), "id": report.id},
    )
    return report.id


def folder(root: Path, owner: User, report_id: uuid.UUID) -> Path:
    return root / str(owner.id) / str(report_id)


async def test_reports_older_than_retention_lose_files_then_rows(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    store = LocalReportStore(tmp_path)
    owner = await make_user(db_session)
    old = await stored_report(db_session, store, owner, age_days=7.01)
    recent = await stored_report(db_session, store, owner, age_days=6.9)
    wide = ReportRepository(db_session, retention_days=30, build_timeout_seconds=600)
    await wide.create_download_token(await wide.get_owned(old, owner), ttl_seconds=3600)

    result = await run_report_cleanup(db_session, store, settings(7))

    assert (result.reports, result.kept_for_retry) == (1, 0)
    assert not folder(tmp_path, owner, old).exists()
    assert folder(tmp_path, owner, recent).is_dir()
    ids = list(await db_session.scalars(text("SELECT id FROM summary_reports")))
    assert ids == [recent]
    assert await count(db_session, "report_download_tokens") == 0  # cascaded with the report


@pytest.mark.parametrize(("days", "deleted"), [(7, 1), (10, 0)])
async def test_retention_follows_configuration(
    db_session: AsyncSession, tmp_path: Path, days: int, deleted: int
) -> None:
    store = LocalReportStore(tmp_path)
    owner = await make_user(db_session)
    await stored_report(db_session, store, owner, age_days=8)

    result = await run_report_cleanup(db_session, store, settings(days))

    assert result.reports == deleted


async def test_a_report_whose_files_cannot_be_deleted_keeps_its_row_for_the_next_run(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalReportStore(tmp_path)
    owner = await make_user(db_session)
    stuck = await stored_report(db_session, store, owner, age_days=9)
    gone = await stored_report(db_session, store, owner, age_days=9)
    real_delete = store.delete_report

    async def locked(user_id: uuid.UUID, report_id: uuid.UUID) -> None:
        if report_id == stuck:
            raise PermissionError("file in use")
        await real_delete(user_id, report_id)

    monkeypatch.setattr(store, "delete_report", locked)

    result = await run_report_cleanup(db_session, store, settings(7))

    assert (result.reports, result.kept_for_retry) == (1, 1)
    ids = list(await db_session.scalars(text("SELECT id FROM summary_reports")))
    assert ids == [stuck]
    assert not folder(tmp_path, owner, gone).exists()


async def test_builds_lost_past_their_time_limit_are_failed_and_their_files_removed(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    store = LocalReportStore(tmp_path)
    owner, other = await make_user(db_session, "a"), await make_user(db_session, "b")
    repo = ReportRepository(db_session, retention_days=7, build_timeout_seconds=600)
    lost, _ = await repo.start_build(owner, TODAY)
    running, _ = await repo.start_build(other, TODAY)
    await store.write(owner.id, lost.id, "report.json", b"{partial")
    await db_session.execute(
        text("UPDATE summary_reports SET created_at = now() - interval '12 minutes' WHERE id = :i"),
        {"i": lost.id},
    )

    result = await run_report_cleanup(db_session, store, settings(timeout=600))

    assert result.lost_builds == 1
    statuses: dict[uuid.UUID, str] = dict(
        (await db_session.execute(text("SELECT id, status FROM summary_reports"))).all()
    )
    assert statuses == {lost.id: "failed", running.id: "building"}
    assert not folder(tmp_path, owner, lost.id).exists()


async def test_expired_and_used_download_links_are_deleted(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    store = LocalReportStore(tmp_path)
    owner = await make_user(db_session)
    repo = ReportRepository(db_session, retention_days=7, build_timeout_seconds=600)
    report = await repo.get_owned(await stored_report(db_session, store, owner, age_days=0), owner)
    used = await repo.create_download_token(report, ttl_seconds=3600)
    await repo.consume_download_token(used)
    await repo.create_download_token(report, ttl_seconds=3600)  # still valid
    await repo.create_download_token(report, ttl_seconds=3600)
    await db_session.execute(
        text(
            "UPDATE report_download_tokens SET expires_at = now() - interval '1 second' "
            "WHERE token_hash = (SELECT token_hash FROM report_download_tokens "
            "WHERE used_at IS NULL LIMIT 1)"
        )
    )

    result = await run_report_cleanup(db_session, store, settings())

    assert result.download_links == 2
    assert await count(db_session, "report_download_tokens") == 1


async def test_old_folders_without_a_row_are_deleted(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    store = LocalReportStore(tmp_path)
    owner = await make_user(db_session)
    expired = await stored_report(db_session, store, owner, age_days=8)
    orphan_old, orphan_new = uuid.uuid4(), uuid.uuid4()
    for orphan in (orphan_old, orphan_new):
        await store.write(owner.id, orphan, "report.pdf", b"%PDF")
    week_ago = (datetime.now(UTC) - timedelta(days=8)).timestamp()
    for report_id in (orphan_old, expired):
        os.utime(folder(tmp_path, owner, report_id), (week_ago, week_ago))

    result = await run_report_cleanup(db_session, store, settings(7))

    assert (result.orphan_folders, result.reports) == (1, 1)
    assert not folder(tmp_path, owner, orphan_old).exists()
    assert folder(tmp_path, owner, orphan_new).is_dir()  # may still be a build in progress


async def test_report_cleanup_is_skipped_while_another_instance_holds_the_lock(
    db_session: AsyncSession, tmp_path: Path, test_database_url: str
) -> None:
    store = LocalReportStore(tmp_path)
    owner = await make_user(db_session)
    await stored_report(db_session, store, owner, age_days=30)
    other_instance = create_async_engine(test_database_url, poolclass=NullPool)
    try:
        async with other_instance.connect() as conn, conn.begin():
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:k)"), {"k": REPORT_CLEANUP_LOCK_KEY}
            )

            result = await run_report_cleanup(db_session, store, settings())
    finally:
        await other_instance.dispose()

    assert result == ReportCleanupResult(ran=False)
    assert await count(db_session, "summary_reports") == 1


async def test_conversation_cleanup_keeps_users_who_still_have_reports(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    store = LocalReportStore(tmp_path)
    with_report = await make_user(db_session, "with-report")
    await make_user(db_session, "without")
    await stored_report(db_session, store, with_report, age_days=1)
    await db_session.execute(text("UPDATE users SET last_seen_at = now() - interval '5000 hours'"))

    result = await run_cleanup(db_session, Settings(_env_file=None))

    assert result.users == 1
    remaining = list(await db_session.scalars(text("SELECT entra_object_id FROM users")))
    assert remaining == ["with-report"]


async def test_user_cleanup_still_considers_conversations(db_session: AsyncSession) -> None:
    with_chat = await make_user(db_session, "with-chat")
    await ChatRepository(db_session, retention_hours=12).create_session(with_chat)
    await db_session.execute(text("UPDATE users SET last_seen_at = now() - interval '5000 hours'"))

    result = await run_cleanup(db_session, Settings(_env_file=None))

    assert result.users == 0


def test_standalone_report_cleanup_command_succeeds(test_database_url: str, tmp_path: Path) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": test_database_url,
        "LOG_LEVEL": "INFO",
        "REPORT_STORAGE_DIR": str(tmp_path),
    }

    completed = subprocess.run(
        [sys.executable, "-m", "app.jobs.report_cleanup"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Report cleanup" in completed.stdout


# --- daily schedule -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now_ist", "expected_hours"),
    [
        (datetime(2026, 10, 13, 1, 0, tzinfo=IST), 1.0),  # later today
        (datetime(2026, 10, 13, 2, 0, tzinfo=IST), 24.0),  # exactly now: tomorrow
        (datetime(2026, 10, 13, 23, 30, tzinfo=IST), 2.5),  # tomorrow
    ],
)
def test_next_run_is_at_the_configured_local_time(now_ist: datetime, expected_hours: float) -> None:
    # The clock is UTC on servers; the schedule is in REPORT_TIMEZONE.
    seconds = seconds_until(time(2, 0), IST, now_ist.astimezone(UTC))

    assert seconds == pytest.approx(expected_hours * 3600)


def test_next_run_crosses_a_daylight_saving_change_correctly() -> None:
    berlin = ZoneInfo("Europe/Berlin")  # clocks go back 1 h in the night to 25 Oct 2026
    now = datetime(2026, 10, 24, 12, 0, tzinfo=berlin)

    assert seconds_until(time(12, 0), berlin, now) == pytest.approx(25 * 3600)


async def test_scheduler_runs_at_startup_then_daily_and_survives_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs: list[str] = []
    sleeps: list[float] = []

    async def flaky(*_: object) -> ReportCleanupResult:
        runs.append("run")
        if len(runs) == 2:
            raise ConnectionError("database briefly unavailable")
        return ReportCleanupResult(ran=True)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(scheduler_module, "run_report_cleanup_once", flaky)
    config = Settings(_env_file=None, report_cleanup_time="02:00", report_timezone="Asia/Kolkata")
    at_8pm_ist = datetime(2026, 10, 13, 14, 30, tzinfo=UTC)  # 20:00 IST
    scheduler = ReportCleanupScheduler(
        None,  # type: ignore[arg-type]
        LocalReportStore(tmp_path),
        config,
        clock=lambda: at_8pm_ist,
        sleep=fake_sleep,
    )

    scheduler.start()
    for _ in range(200):
        if len(runs) >= 3:
            break
        await asyncio.sleep(0)
    await scheduler.stop()

    assert len(runs) >= 3  # startup run, a failed run, and the run after it
    assert sleeps[0] == pytest.approx(6 * 3600)  # until 02:00 IST
    assert sleeps[1] == pytest.approx(3600)  # after the failure: retried within the hour


async def test_scheduler_never_runs_twice_when_it_wakes_a_moment_early(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sleeps: list[float] = []
    calls: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise asyncio.CancelledError

    async def ok(*_: object) -> ReportCleanupResult:
        calls.append("run")
        return ReportCleanupResult(ran=True)

    monkeypatch.setattr(scheduler_module, "run_report_cleanup_once", ok)
    just_before_two = datetime(2026, 10, 12, 20, 29, 59, 900_000, tzinfo=UTC)  # 01:59:59.9 IST
    scheduler = ReportCleanupScheduler(
        None,  # type: ignore[arg-type]
        LocalReportStore(tmp_path),
        Settings(_env_file=None),
        clock=lambda: just_before_two,
        sleep=fake_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await scheduler._loop()

    assert calls == ["run"]

    assert sleeps == [pytest.approx(24 * 3600 + 0.1)]
