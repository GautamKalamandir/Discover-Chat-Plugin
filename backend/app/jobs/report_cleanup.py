"""Run the /summary report deletion once and exit (for OS cron / container or cloud schedulers).

Usage (from backend/):
    uv run python -m app.jobs.report_cleanup

Deletes reports older than REPORT_RETENTION_DAYS (files first, then rows; ADR 0015).
Exit code 0 on success (including "skipped: another instance holds the lock"), 1 on failure.
"""

import asyncio
import logging
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine, create_sessionmaker
from app.reports.store import create_report_store
from app.retention.scheduler import run_report_cleanup_once

logger = logging.getLogger("app.jobs.report_cleanup")


async def _main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    engine = create_engine(settings)
    try:
        result = await run_report_cleanup_once(
            create_sessionmaker(engine), create_report_store(settings), settings
        )
    except Exception:
        logger.exception("Report cleanup failed")
        return 1
    finally:
        await engine.dispose()
    logger.info("Done: %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
