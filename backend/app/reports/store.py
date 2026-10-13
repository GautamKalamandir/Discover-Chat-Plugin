"""Where /summary report files live (ADR 0015 §10).

Each report has its own folder: `<user id>/<report id>/report.json` and `report.pdf`. Paths
stored in the database are relative to the store's root, so the store can move (local disk now;
cloud storage in Phase 13 behind the same interface).
"""

import asyncio
import os
import re
import shutil
import tempfile
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path, PurePosixPath

from app.core.config import Settings

_FILE_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class ReportStoreError(Exception):
    """A path outside the store, or a malformed name. Never caused by user input alone."""


class ReportStore(ABC):
    @abstractmethod
    async def write(self, user_id: uuid.UUID, report_id: uuid.UUID, name: str, data: bytes) -> str:
        """Stores one file and returns its relative path (what the database keeps)."""

    @abstractmethod
    async def read(self, path: str) -> bytes: ...

    @abstractmethod
    async def delete_report(self, user_id: uuid.UUID, report_id: uuid.UUID) -> None:
        """Removes the report's folder. Already gone is fine."""

    @abstractmethod
    async def folders_older_than(self, cutoff: datetime) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(user id, report id) of report folders last written before `cutoff`."""


class LocalReportStore(ReportStore):
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    async def write(self, user_id: uuid.UUID, report_id: uuid.UUID, name: str, data: bytes) -> str:
        if not _FILE_NAME.match(name):
            raise ReportStoreError(f"invalid report file name {name!r}")
        relative = PurePosixPath(str(user_id), str(report_id), name)
        await asyncio.to_thread(self._write_atomic, self._resolve(str(relative)), data)
        return str(relative)

    async def read(self, path: str) -> bytes:
        return await asyncio.to_thread(self._resolve(path).read_bytes)

    async def delete_report(self, user_id: uuid.UUID, report_id: uuid.UUID) -> None:
        folder = self._resolve(f"{user_id}/{report_id}")
        await asyncio.to_thread(_remove_folder, folder)

    async def folders_older_than(self, cutoff: datetime) -> list[tuple[uuid.UUID, uuid.UUID]]:
        return await asyncio.to_thread(self._scan, cutoff.timestamp())

    def _resolve(self, relative: str) -> Path:
        """Absolute path for a stored relative path; never outside the root."""
        candidate = (self._root / relative).resolve()
        if candidate == self._root or not candidate.is_relative_to(self._root):
            raise ReportStoreError("report path outside the storage directory")
        return candidate

    @staticmethod
    def _write_atomic(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Written next to the target, then renamed: readers never see a half-written file.
        handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _scan(self, cutoff: float) -> list[tuple[uuid.UUID, uuid.UUID]]:
        found: list[tuple[uuid.UUID, uuid.UUID]] = []
        if not self._root.is_dir():
            return found
        for user_dir in self._root.iterdir():
            user_id = _as_uuid(user_dir.name)
            if user_id is None or not user_dir.is_dir():
                continue
            for report_dir in user_dir.iterdir():
                report_id = _as_uuid(report_dir.name)
                if report_id is not None and report_dir.is_dir():
                    if report_dir.stat().st_mtime < cutoff:
                        found.append((user_id, report_id))
        return found


def _remove_folder(folder: Path) -> None:
    # The (empty) user folder is left in place: removing it could race with a new report.
    if folder.exists():
        shutil.rmtree(folder)


def _as_uuid(name: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(name)
    except ValueError:
        return None


def create_report_store(settings: Settings) -> ReportStore:
    return LocalReportStore(settings.report_storage_dir)
