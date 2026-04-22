import asyncio

import pytest

from lumina.api.sse import stream_llm


class _CancelledLLM:
    async def generate_stream(self, user_text: str, task: str):
        raise asyncio.CancelledError()
        yield  # pragma: no cover


@pytest.mark.anyio
async def test_stream_llm_propagates_cancelled_error():
    with pytest.raises(asyncio.CancelledError):
        async for _ in stream_llm(_CancelledLLM(), "hello", task="chat"):
            pass
