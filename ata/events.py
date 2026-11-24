"""Pub/sub em memória para o stream SSE.

Os workers (threads) publicam; os assinantes (corrotinas do FastAPI) consomem.
A ponte thread -> loop é feita com `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
from typing import Any


class EventHub:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, meeting_id: str, event: dict[str, Any]) -> None:
        """Thread-safe. Guarda no histórico e entrega aos assinantes."""
        self._history.setdefault(meeting_id, []).append(event)
        if len(self._history[meeting_id]) > 5000:
            self._history[meeting_id] = self._history[meeting_id][-4000:]
        loop = self._loop
        if loop is None:
            return
        for queue in list(self._subs.get(meeting_id, ())):
            loop.call_soon_threadsafe(queue.put_nowait, event)

    def clear(self, meeting_id: str) -> None:
        self._history.pop(meeting_id, None)

    async def subscribe(self, meeting_id: str, replay: bool = True):
        queue: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(meeting_id, set()).add(queue)
        try:
            if replay:
                for event in list(self._history.get(meeting_id, ())):
                    yield event
            while True:
                yield await queue.get()
        finally:
            subs = self._subs.get(meeting_id)
            if subs:
                subs.discard(queue)
                if not subs:
                    self._subs.pop(meeting_id, None)
