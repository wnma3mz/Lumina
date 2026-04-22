"""
lumina/api/sse.py — SSE 流式响应辅助

公开接口：
    stream_llm  — 将 llm.generate_stream() 包装为 SSE 数据行 AsyncIterator
"""
import json
import logging
import asyncio
from typing import AsyncIterator

from lumina.engine.request_context import request_context

logger = logging.getLogger("lumina")


async def stream_llm(
    llm,
    user_text: str,
    *,
    task: str,
    log_label: str = "stream",
    origin: str = "unknown",
    client_model: str = None,
    request_id: str = None,
) -> AsyncIterator[str]:
    """
    驱动 llm.generate_stream(user_text, task=task)，yield SSE 数据行。

    最后 yield "data: [DONE]\\n\\n"，异常时 yield error 事件。
    """
    try:
        with request_context(
            origin=origin,
            stream=True,
            client_model=client_model,
            request_id=request_id,
        ):
            async for token in llm.generate_stream(user_text, task=task):
                yield f"data: {json.dumps({'text': token})}\n\n"
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("%s error: %s", log_label, e)
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
    yield "data: [DONE]\n\n"
