from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator


class InferenceQueue:
    """Provider 请求队列的薄封装。

    调度器仍直接消费底层 asyncio.Queue/Event；这里先收拢提交、消费和取消语义。
    """

    def __init__(self, queue: asyncio.Queue, not_empty: asyncio.Event) -> None:
        self.queue = queue
        self.not_empty = not_empty

    async def submit(self, slot: Any) -> None:
        await self.queue.put(slot)
        self.not_empty.set()

    async def consume(self, slot: Any) -> AsyncIterator[str]:
        while True:
            item = await slot.token_queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item

    @staticmethod
    def cancel(slot: Any) -> None:
        slot.done = True

    @staticmethod
    def drop_pending(queue: asyncio.Queue, exc: Exception) -> None:
        while True:
            try:
                slot = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if slot.done:
                continue
            slot.done = True
            try:
                slot.token_queue.put_nowait(exc)
            except Exception:
                pass
