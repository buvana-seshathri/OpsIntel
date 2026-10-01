"""Publish/subscribe for live investigation traces.

`InMemoryEventBus` is enough for one API process. The interface is the seam for Redis
Streams or Kinesis when the API runs as several replicas.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any, Protocol

END = {"type": "end"}


class EventBus(Protocol):
    def publish(self, topic: str, event: dict[str, Any]) -> None: ...

    def close(self, topic: str) -> None: ...

    def subscribe(self, topic: str) -> AsyncIterator[dict[str, Any]]: ...


class InMemoryEventBus:
    def __init__(self) -> None:
        self._history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._queues: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        self._closed: set[str] = set()

    def open(self, topic: str) -> None:
        self._history.setdefault(topic, [])

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        self._history[topic].append(event)
        for q in self._queues[topic]:
            q.put_nowait(event)

    def close(self, topic: str) -> None:
        self._closed.add(topic)
        for q in self._queues[topic]:
            q.put_nowait(END)

    def is_known(self, topic: str) -> bool:
        return topic in self._history or topic in self._closed

    async def subscribe(self, topic: str) -> AsyncIterator[dict[str, Any]]:
        """Replays what was already published, then follows live until the topic closes."""
        # No await between registering the queue and snapshotting history, so on a single
        # event loop every event lands in exactly one of the two.
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queues[topic].append(queue)
        backlog = list(self._history[topic])
        closed = topic in self._closed
        try:
            for event in backlog:
                yield event
            if closed:
                return
            while (event := await queue.get()) is not END:
                yield event
        finally:
            self._queues[topic].remove(queue)
