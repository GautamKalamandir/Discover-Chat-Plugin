"""Run the retention cleanup once and exit (for OS cron / container or cloud schedulers).

Usage (from backend/):
    uv run python -m app.jobs.cleanup

Exit code 0 on success (including "skipped: another instance holds the lock"), 1 on failure.
"""

import asyncio
import logging
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine, create_sessionmaker
from app.retention.scheduler import run_cleanup_once

logger = logging.getLogger("app.jobs.cleanup")


async def _main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    engine = create_engine(settings)
    try:
        result = await run_cleanup_once(create_sessionmaker(engine), settings)
    except Exception:
        logger.exception("Retention cleanup failed")
        return 1
    finally:
        await engine.dispose()
    logger.info("Done: %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
