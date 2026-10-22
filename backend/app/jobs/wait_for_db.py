"""Wait until the database accepts connections, then exit (container start, before migrations).

Usage (from backend/):
    python -m app.jobs.wait_for_db [--timeout 60]

Docker Desktop starts every `restart: unless-stopped` container at once and ignores compose's
`depends_on`, so the backend can start before the `postgres` host name even resolves. Waiting here
keeps that race out of the logs; a real migration error still fails the start straight away.
Exit code 0 when the database is reachable, 1 when it still isn't after the timeout.
"""

import argparse
import logging
import socket
import sys
import time
from collections.abc import Callable

from sqlalchemy.engine import make_url

from app.core.config import get_settings

logger = logging.getLogger("app.jobs.wait_for_db")

DEFAULT_PORT = 5432


def reachable(host: str, port: int, *, timeout: float = 2.0) -> bool:
    """The host name resolves and something accepts a TCP connection on the port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:  # includes socket.gaierror (the name doesn't resolve yet)
        return False


def wait(
    host: str,
    port: int,
    *,
    timeout: float,
    interval: float = 1.0,
    check: Callable[[str, int], bool] = reachable,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = clock() + timeout
    while True:
        if check(host, port):
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="app.jobs.wait_for_db")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds (default 60)")
    args = parser.parse_args(argv)
    url = make_url(get_settings().database_url)
    host, port = url.host or "localhost", url.port or DEFAULT_PORT
    if wait(host, port, timeout=args.timeout):
        return 0
    # Host and port only: the URL holds the password.
    print(f"error: database {host}:{port} not reachable after {args.timeout:.0f}s", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
