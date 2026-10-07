"""Turn protection for the chat API (ADR 0009).

- one turn at a time per conversation (409)
- per-user parallel turns and questions per minute (429)
- a hard time limit per turn; the agent is cancelled when it is exceeded or the client leaves

State is per backend instance, like the REST rate limiter (shared limits revisited in Phase 13).
Slots also expire on their own after the turn timeout, so a stream that never started can't
leak one.
"""

import asyncio
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from app.agent.events import AgentEvent, DoneEvent, ErrorEvent
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode

SLOT_GRACE_SECONDS = 30
TIMEOUT_MESSAGE = "This is taking too long. Please try a narrower question."


@dataclass(frozen=True)
class TurnTicket:
    user_key: str
    session_id: uuid.UUID
    started: float
    id: uuid.UUID = field(default_factory=uuid.uuid4)


class TurnGuard:
    def __init__(self, settings: Settings, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._per_minute = settings.chat_questions_per_minute
        self._max_parallel = settings.chat_max_concurrent_turns
        self._max_age = settings.chat_turn_timeout_seconds + SLOT_GRACE_SECONDS
        self._clock = clock
        self._active: dict[uuid.UUID, TurnTicket] = {}
        self._recent: dict[str, deque[float]] = defaultdict(deque)

    def acquire(self, user_key: str, session_id: uuid.UUID) -> TurnTicket:
        """Synchronous on the event loop, so check-and-reserve can't interleave."""
        now = self._clock()
        self._active = {t.id: t for t in self._active.values() if now - t.started < self._max_age}

        if any(t.session_id == session_id for t in self._active.values()):
            raise AppError(
                ErrorCode.TURN_IN_PROGRESS,
                "I'm still answering your previous question in this chat.",
                409,
            )
        if sum(t.user_key == user_key for t in self._active.values()) >= self._max_parallel:
            raise AppError(
                ErrorCode.TOO_MANY_PARALLEL_QUESTIONS,
                "Please wait for your other questions to finish.",
                429,
            )
        recent = self._recent[user_key]
        while recent and now - recent[0] >= 60:
            recent.popleft()
        if len(recent) >= self._per_minute:
            raise AppError(
                ErrorCode.TOO_MANY_QUESTIONS,
                "You're asking questions very quickly. Please wait a moment and try again.",
                429,
                headers={"Retry-After": str(max(1, int(60 - (now - recent[0]))))},
            )
        recent.append(now)
        ticket = TurnTicket(user_key, session_id, now)
        self._active[ticket.id] = ticket
        return ticket

    def release(self, ticket: TurnTicket) -> None:
        self._active.pop(ticket.id, None)

    def active_count(self) -> int:
        return len(self._active)


async def with_time_limit(
    events: AsyncIterator[AgentEvent], timeout_seconds: float
) -> AsyncIterator[AgentEvent]:
    """Relays agent events until done or the deadline; on timeout the agent is cancelled and the
    client gets an error event. Closing this generator (client disconnect) closes the agent too."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    iterator = aiter(events)
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            try:
                event = await asyncio.wait_for(anext(iterator), remaining)
            except StopAsyncIteration:
                return
            yield event
    except TimeoutError:
        yield ErrorEvent(str(ErrorCode.TURN_TIMEOUT), TIMEOUT_MESSAGE)
        yield DoneEvent(None)
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()
