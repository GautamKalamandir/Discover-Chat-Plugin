import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.db.models import QueryExecution, QueryStatus, ReportDownloadToken, ReportStatus, User
from app.db.repositories.chat import ChatRepository
from app.db.repositories.queries import QueryExecutionRepository
from app.db.repositories.reports import ReportRepository, hash_token
from tests.test_repositories import make_user

pytestmark = pytest.mark.integration

TODAY = date(2026, 10, 13)


def repo(db: AsyncSession, *, retention_days: int = 7, timeout: int = 600) -> ReportRepository:
    return ReportRepository(db, retention_days=retention_days, build_timeout_seconds=timeout)


async def age(db: AsyncSession, report_id: uuid.UUID, **delta: float) -> None:
    await db.execute(
        text("UPDATE summary_reports SET created_at = now() - CAST(:d AS interval) WHERE id = :id"),
        {"d": timedelta(**delta), "id": report_id},
    )


async def ready_report(db: AsyncSession, owner: User, day: date = TODAY) -> uuid.UUID:
    report, _ = await repo(db).start_build(owner, day)
    await repo(db).mark_ready(
        report.id,
        models=[{"dataset_id": "sales-ds", "name": "Sales", "status": "ok"}],
        headline="Sales up",
        suggestion_counts={"high": 1, "medium": 0, "low": 2},
        json_path=f"{owner.id}/{report.id}/report.json",
        pdf_path=f"{owner.id}/{report.id}/report.pdf",
    )
    return report.id


# --- builds -------------------------------------------------------------------------------------


async def test_one_build_at_a_time_per_user(db_session: AsyncSession) -> None:
    alice, bob = await make_user(db_session, "alice"), await make_user(db_session, "bob")

    first, created = await repo(db_session).start_build(alice, TODAY)
    again, created_again = await repo(db_session).start_build(alice, TODAY)
    other, created_other = await repo(db_session).start_build(bob, TODAY)

    assert created and first.status is ReportStatus.BUILDING
    assert (again.id, created_again) == (first.id, False)
    assert created_other and other.id != first.id


async def test_the_database_itself_refuses_a_second_running_build(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    await repo(db_session).start_build(owner, TODAY)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO summary_reports (user_id, status, report_date) "
                    "VALUES (:u, 'building', :d)"
                ),
                {"u": owner.id, "d": TODAY},
            )


async def test_a_finished_build_allows_the_next_one(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    await ready_report(db_session, owner)

    report, created = await repo(db_session).start_build(owner, TODAY)

    assert created and report.status is ReportStatus.BUILDING


async def test_a_build_lost_past_its_time_limit_no_longer_blocks_the_user(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session)
    lost, _ = await repo(db_session, timeout=600).start_build(owner, TODAY)
    await age(db_session, lost.id, seconds=600 + 61)

    fresh, created = await repo(db_session, timeout=600).start_build(owner, TODAY)

    assert created and fresh.id != lost.id
    await db_session.refresh(lost)
    assert (lost.status, lost.error_code) == (ReportStatus.FAILED, "build_lost")


async def test_mark_failed_records_the_reason_and_only_touches_running_builds(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session)
    report, _ = await repo(db_session).start_build(owner, TODAY)

    await repo(db_session).mark_failed(report.id, "all_models_failed", models=[{"name": "Sales"}])
    await repo(db_session).mark_ready(
        report.id, models=[], headline="x", suggestion_counts={}, json_path="a", pdf_path="b"
    )

    await db_session.refresh(report)
    assert report.status is ReportStatus.FAILED
    assert report.error_code == "all_models_failed"
    assert report.models == [{"name": "Sales"}]
    assert report.completed_at is not None


# --- reading ------------------------------------------------------------------------------------


async def test_todays_ready_report_is_reused_but_not_other_days_or_failed_ones(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session)
    await ready_report(db_session, owner, TODAY - timedelta(days=1))
    failed, _ = await repo(db_session).start_build(owner, TODAY)
    await repo(db_session).mark_failed(failed.id, "x")
    assert await repo(db_session).latest_ready_for_day(owner, TODAY) is None

    today = await ready_report(db_session, owner, TODAY)

    found = await repo(db_session).latest_ready_for_day(owner, TODAY)
    assert found is not None and found.id == today


async def test_reports_are_owner_only_and_identical_404s(db_session: AsyncSession) -> None:
    alice, bob = await make_user(db_session, "alice"), await make_user(db_session, "bob")
    report_id = await ready_report(db_session, alice)

    assert (await repo(db_session).get_owned(report_id, alice)).id == report_id
    for owner, wanted in ((bob, report_id), (alice, uuid.uuid4())):
        with pytest.raises(AppError) as caught:
            await repo(db_session).get_owned(wanted, owner)
        assert caught.value.code is ErrorCode.REPORT_NOT_FOUND
        assert caught.value.status_code == 404
    assert await repo(db_session).list_recent(bob) == []


async def test_reports_past_retention_are_unreachable_before_cleanup(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session)
    old = await ready_report(db_session, owner, TODAY - timedelta(days=8))
    recent = await ready_report(db_session, owner)
    await age(db_session, old, days=7, minutes=1)

    listed = [r.id for r in await repo(db_session, retention_days=7).list_recent(owner)]

    assert listed == [recent]
    with pytest.raises(AppError):
        await repo(db_session, retention_days=7).get_owned(old, owner)
    assert (await repo(db_session, retention_days=8).get_owned(old, owner)).id == old


async def test_history_is_newest_first_and_includes_running_and_failed(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session)
    first = await ready_report(db_session, owner)
    await age(db_session, first, hours=2)
    running, _ = await repo(db_session).start_build(owner, TODAY)

    listed = await repo(db_session).list_recent(owner)

    assert [(r.id, r.status) for r in listed] == [
        (running.id, ReportStatus.BUILDING),
        (first, ReportStatus.READY),
    ]


# --- one-time download links --------------------------------------------------------------------


async def test_a_download_link_works_exactly_once(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    report = await repo(db_session).get_owned(await ready_report(db_session, owner), owner)

    token = await repo(db_session).create_download_token(report, ttl_seconds=60)

    assert (await repo(db_session).consume_download_token(token)).id == report.id
    with pytest.raises(AppError) as caught:
        await repo(db_session).consume_download_token(token)
    assert caught.value.code is ErrorCode.REPORT_NOT_FOUND


async def test_only_the_hash_of_a_download_link_is_stored(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    report = await repo(db_session).get_owned(await ready_report(db_session, owner), owner)

    token = await repo(db_session).create_download_token(report, ttl_seconds=60)

    stored = (await db_session.scalars(select(ReportDownloadToken))).one()
    assert stored.token_hash == hash_token(token) != token
    assert len(token) >= 40  # 32 random bytes, URL-safe


@pytest.mark.parametrize("case", ["expired", "unknown", "report_expired", "report_failed"])
async def test_invalid_download_links_all_look_the_same(
    db_session: AsyncSession, case: str
) -> None:
    owner = await make_user(db_session)
    if case == "report_failed":
        failed, _ = await repo(db_session).start_build(owner, TODAY)
        await repo(db_session).mark_failed(failed.id, "x")
        report = failed
    else:
        report = await repo(db_session).get_owned(await ready_report(db_session, owner), owner)
    token = await repo(db_session).create_download_token(report, ttl_seconds=60)
    if case == "expired":
        await db_session.execute(
            text("UPDATE report_download_tokens SET expires_at = now() - interval '1 second'")
        )
    elif case == "unknown":
        token = "not-a-real-token"
    elif case == "report_expired":
        await age(db_session, report.id, days=8)

    with pytest.raises(AppError) as caught:
        await repo(db_session).consume_download_token(token)

    assert caught.value.code is ErrorCode.REPORT_NOT_FOUND
    assert caught.value.status_code == 404


# --- query records ------------------------------------------------------------------------------


async def test_report_queries_are_recorded_and_deleted_with_their_report(
    db_session: AsyncSession,
) -> None:
    owner = await make_user(db_session)
    report, _ = await repo(db_session).start_build(owner, TODAY)

    execution = await QueryExecutionRepository(db_session).record(
        None, report_id=report.id, gateway="rest", dax="EVALUATE {1}", status=QueryStatus.SUCCEEDED
    )
    assert (execution.session_id, execution.report_id) == (None, report.id)

    await db_session.execute(text("DELETE FROM summary_reports WHERE id = :id"), {"id": report.id})
    assert (await db_session.scalars(select(QueryExecution))).all() == []


async def test_a_query_record_has_exactly_one_owner(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    chat = await ChatRepository(db_session, retention_hours=12).create_session(owner)
    report, _ = await repo(db_session).start_build(owner, TODAY)
    queries = QueryExecutionRepository(db_session)

    with pytest.raises(ValueError, match="exactly one"):
        await queries.record(
            chat, report_id=report.id, gateway="rest", dax="EVALUATE {1}", status=QueryStatus.FAILED
        )
    with pytest.raises(ValueError, match="exactly one"):
        await queries.record(None, gateway="rest", dax="EVALUATE {1}", status=QueryStatus.FAILED)
    with pytest.raises(IntegrityError):  # the database enforces it too
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO query_executions (gateway, dax, status) "
                    "VALUES ('rest', 'EVALUATE {1}', 'failed')"
                )
            )
