"""
lumina/api/routers/text.py — 翻译 / 摘要 / 润色路由
"""
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from lumina.api.protocol import (
    PolishRequest,
    SummarizeRequest,
    TextResponse,
    TranslateRequest,
)

router = APIRouter(tags=["text"])

logger = logging.getLogger("lumina")


@router.post("/v1/translate")
async def translate(request: TranslateRequest, raw: Request):
    llm = raw.app.state.llm
    task = "translate_to_zh" if request.target_language == "zh" else "translate_to_en"
    if request.stream:
        return StreamingResponse(
            _stream_text(request.text, task, llm),
            media_type="text/event-stream",
        )
    text = await llm.generate(request.text, task=task)
    return TextResponse(text=text)


@router.post("/v1/summarize")
async def summarize(request: SummarizeRequest, raw: Request):
    llm = raw.app.state.llm
    if request.stream:
        return StreamingResponse(
            _stream_text(request.text, "summarize", llm),
            media_type="text/event-stream",
        )
    text = await llm.generate(request.text, task="summarize")
    return TextResponse(text=text)


@router.post("/v1/polish")
async def polish(request: PolishRequest, raw: Request):
    llm = raw.app.state.llm
    task = "polish_zh" if request.language == "zh" else "polish_en"
    if request.stream:
        return StreamingResponse(
            _stream_text(request.text, task, llm),
            media_type="text/event-stream",
        )
    text = await llm.generate(request.text, task=task)
    return TextResponse(text=text)


async def _stream_text(user_text: str, task: str, llm):
    try:
        async for token in llm.generate_stream(user_text, task=task):
            yield f"data: {json.dumps({'text': token})}\n\n"
    except Exception as e:
        logger.error("stream_text error: %s", e)
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
    yield "data: [DONE]\n\n"
