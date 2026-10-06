import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable

from app.powerbi.errors import PowerBIThrottledError


class PerUserRateLimiter:
    """Sliding one-minute window per user, per backend instance.

    Keeps the chatbot under Microsoft's Execute Queries limit (120 queries/min/user) instead of
    discovering it through 429s.
    """

    def __init__(self, per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = per_minute
        self._clock = clock
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def acquire(self, user_key: str) -> None:
        async with self._lock:
            now = self._clock()
            window = self._calls[user_key]
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= self._limit:
                retry_after = 60 - (now - window[0])
                raise PowerBIThrottledError("Per-user query limit reached", retry_after)
            window.append(now)
