"""Keeps LLM calls under a tokens-per-minute limit (LLM_TOKENS_PER_MINUTE; ADR 0016 amendment 1).

Free provider tiers refuse a request when the tokens of the last minute plus this request exceed
the limit (Groq free tier: 8,000 per minute for gpt-oss-120b). Instead of failing, a call waits
until it fits. Each call reserves its estimate (input + answer room); when the answer arrives the
reservation is replaced by what the provider says it used (cached input doesn't count).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0


@dataclass
class Reservation:
    at: float
    tokens: int


class TokenPacer:
    def __init__(
        self,
        tokens_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.limit = tokens_per_minute
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._window: list[Reservation] = []

    async def reserve(self, tokens: int) -> Reservation:
        """Waits until `tokens` more fit in the last minute, then books them."""
        if tokens > self.limit:
            logger.warning(
                "LLM request of about %d tokens is over the %d per-minute limit", tokens, self.limit
            )
        async with self._lock:
            while True:
                now = self._clock()
                self._window = [r for r in self._window if now - r.at < WINDOW_SECONDS]
                used = sum(r.tokens for r in self._window)
                if not self._window or used + tokens <= self.limit:
                    reservation = Reservation(now, tokens)
                    self._window.append(reservation)
                    return reservation
                wait = self._window[0].at + WINDOW_SECONDS - now
                logger.info("Pacing LLM calls: waiting %.1fs for the per-minute limit", wait)
                await self._sleep(max(wait, 0.1))

    def settle(self, reservation: Reservation, used: int) -> None:
        """Replaces the estimate with the tokens the provider counted."""
        reservation.tokens = max(used, 0)


def estimate_tokens(texts: list[str]) -> int:
    """A cautious count for planning (about 3 characters a token for schema-heavy text)."""
    return sum(len(t) for t in texts) // 3 + 8 * len(texts)
