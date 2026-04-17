"""
lumina/api/routers/text.py — 翻译 / 摘要 / 润色路由
"""
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from lumina.api.rendering import render_markdown_html
from lumina.api.protocol import (
    PolishRequest,
    RenderMarkdownRequest,
    RenderedHtmlResponse,
    SummarizeRequest,
    TextResponse,
    TranslateRequest,
)
from lumina.api.sse import stream_llm
from lumina.request_context import request_context

router = APIRouter(tags=["text"])


@router.post("/v1/translate")
async def translate(request: TranslateRequest, raw: Request):
    llm = raw.app.state.llm
    task = "translate_to_zh" if request.target_language == "zh" else "translate_to_en"
    if request.stream:
        return StreamingResponse(
            _stream_text(request.text, task, llm, origin="translate_api"),
            media_type="text/event-stream",
        )
    with request_context(origin="translate_api", stream=False):
        text = await llm.generate(request.text, task=task)
    return TextResponse(text=text)


@router.post("/v1/summarize")
async def summarize(request: SummarizeRequest, raw: Request):
    llm = raw.app.state.llm
    if request.stream:
        return StreamingResponse(
            _stream_text(request.text, "summarize", llm, origin="summarize_api"),
            media_type="text/event-stream",
        )
    with request_context(origin="summarize_api", stream=False):
        text = await llm.generate(request.text, task="summarize")
    return TextResponse(text=text)


@router.post("/v1/polish")
async def polish(request: PolishRequest, raw: Request):
    llm = raw.app.state.llm
    task = "polish_zh" if request.language == "zh" else "polish_en"
    if request.stream:
        return StreamingResponse(
            _stream_text(request.text, task, llm, origin="polish_api"),
            media_type="text/event-stream",
        )
    with request_context(origin="polish_api", stream=False):
        text = await llm.generate(request.text, task=task)
    return TextResponse(text=text)


@router.post("/v1/render_markdown")
async def render_markdown(request: RenderMarkdownRequest):
    return RenderedHtmlResponse(html=render_markdown_html(request.text))


async def _stream_text(user_text: str, task: str, llm, *, origin: str):
    async for chunk in stream_llm(
        llm,
        user_text,
        task=task,
        log_label="stream_text",
        origin=origin,
    ):
        yield chunk
