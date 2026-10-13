import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import Settings
from app.reports.store import LocalReportStore, ReportStoreError, create_report_store

USER = uuid.uuid4()
REPORT = uuid.uuid4()


async def test_written_files_are_read_back_by_their_relative_path(tmp_path: Path) -> None:
    store = LocalReportStore(tmp_path)

    path = await store.write(USER, REPORT, "report.pdf", b"%PDF-1.7 data")

    assert path == f"{USER}/{REPORT}/report.pdf"
    assert await store.read(path) == b"%PDF-1.7 data"
    assert (tmp_path / str(USER) / str(REPORT) / "report.pdf").is_file()


async def test_writes_replace_the_file_and_leave_no_temporary_files(tmp_path: Path) -> None:
    store = LocalReportStore(tmp_path)

    await store.write(USER, REPORT, "report.json", b"first")
    path = await store.write(USER, REPORT, "report.json", b"second")

    assert await store.read(path) == b"second"
    assert sorted(p.name for p in (tmp_path / str(USER) / str(REPORT)).iterdir()) == ["report.json"]


@pytest.mark.parametrize("name", ["../escape.pdf", "a/b.pdf", "..", "", "Report.PDF", ".hidden"])
async def test_file_names_are_restricted(tmp_path: Path, name: str) -> None:
    with pytest.raises(ReportStoreError):
        await LocalReportStore(tmp_path).write(USER, REPORT, name, b"x")


@pytest.mark.parametrize(
    "path", ["../outside.pdf", "../../etc/passwd", "/etc/passwd", "C:/Windows/win.ini", "."]
)
async def test_reads_outside_the_storage_directory_are_refused(tmp_path: Path, path: str) -> None:
    store = LocalReportStore(tmp_path / "reports")

    with pytest.raises(ReportStoreError):
        await store.read(path)


async def test_delete_removes_the_report_folder_and_tolerates_a_missing_one(
    tmp_path: Path,
) -> None:
    store = LocalReportStore(tmp_path)
    await store.write(USER, REPORT, "report.json", b"{}")
    await store.write(USER, REPORT, "report.pdf", b"%PDF")

    await store.delete_report(USER, REPORT)
    await store.delete_report(USER, REPORT)  # already gone: fine

    assert not (tmp_path / str(USER) / str(REPORT)).exists()


async def test_old_folders_are_listed_and_unrelated_entries_ignored(tmp_path: Path) -> None:
    store = LocalReportStore(tmp_path)
    old, new = uuid.uuid4(), uuid.uuid4()
    await store.write(USER, old, "report.json", b"{}")
    await store.write(USER, new, "report.json", b"{}")
    week_ago = (datetime.now(UTC) - timedelta(days=8)).timestamp()
    os.utime(tmp_path / str(USER) / str(old), (week_ago, week_ago))
    (tmp_path / "not-a-uuid").mkdir()
    (tmp_path / str(USER) / "stray.txt").write_text("x")

    found = await store.folders_older_than(datetime.now(UTC) - timedelta(days=7))

    assert found == [(USER, old)]


async def test_a_missing_storage_directory_has_no_folders(tmp_path: Path) -> None:
    store = LocalReportStore(tmp_path / "never-created")

    assert await store.folders_older_than(datetime.now(UTC)) == []


def test_factory_uses_the_configured_directory(tmp_path: Path) -> None:
    store = create_report_store(Settings(_env_file=None, report_storage_dir=str(tmp_path)))

    assert isinstance(store, LocalReportStore)
