"""Local capture bundles for the Phase 2 spikes (gitignored; docs/spikes-runbook.md).

Rules: no token is ever written; query result values are masked (Q9c); model metadata (schema
payloads) and Power BI error texts are kept as-is but only in this local, gitignored folder.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_BUNDLE_ID = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{10}$")


def user_hash(user_key: str) -> str:
    return hashlib.sha256(user_key.encode()).hexdigest()[:10]


def mask(value: Any) -> Any:
    """Keeps structure and keys, replaces every value by its type (and text length)."""
    if isinstance(value, dict):
        return {k: mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, int | float):
        return "<number>"
    if isinstance(value, str):
        return f"<text:{len(value)}>"
    return value


def mask_text_payload(text: str | None) -> Any:
    """Tool output text may be JSON or CSV rows: masked either way."""
    if text is None:
        return None
    try:
        return mask(json.loads(text))
    except ValueError:
        lines = text.splitlines()
        if len(lines) > 1 and "," in lines[0]:
            return {"csv_header": lines[0], "csv_rows": len(lines) - 1}
        return f"<text:{len(text)}>"


class CaptureBundle:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def id(self) -> str:
        return self.directory.name

    @classmethod
    def create(cls, root: Path, user_key: str) -> "CaptureBundle":
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")  # microseconds: no collisions
        directory = root / f"{stamp}-{user_hash(user_key)}"
        directory.mkdir(parents=True)
        return cls(directory)

    @classmethod
    def open(cls, root: Path, bundle_id: str, user_key: str) -> "CaptureBundle | None":
        """Only the user who created a bundle may add to it."""
        if not _BUNDLE_ID.match(bundle_id) or not bundle_id.endswith(user_hash(user_key)):
            return None
        directory = root / bundle_id
        return cls(directory) if directory.is_dir() else None

    def write_json(self, name: str, data: Any) -> None:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        (self.directory / safe).write_text(
            json.dumps(data, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
        )
