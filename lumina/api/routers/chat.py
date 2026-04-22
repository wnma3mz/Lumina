"""
lumina/api/routers/chat.py — Chat Completions 路由（OpenAI 兼容）
"""
import asyncio
import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from lumina.api.chat_runtime import (
    extract_system_override,
    run_chat_messages,
    stream_chat_messages,
    to_provider_messages,
)
from lumina.api.protocol import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionStreamChoice,
    ChatCompletionStreamDelta,
    ChatCompletionStreamResponse,
    ChatMessage,
    UsageInfo,
    random_uuid,
)

router = APIRouter(tags=["chat"])

logger = logging.getLogger("lumina")

# pdf_translate.py 用此前缀标记翻译请求（如 lumina-translate-zh）
_TRANSLATE_MODEL_PREFIX = "lumina-translate-"

# 翻译任务 max_tokens 下限：pdf2zh 不传 max_tokens，默认 512 会截断长段落
_TRANSLATE_MIN_MAX_TOKENS = 2048

# 重复检测：短语最小长度（字符数）和最少重复次数
_REPEAT_MIN_PHRASE_LEN = 8
_REPEAT_MAX_COUNT = 3


def _resolve_translate_task(model: str) -> Optional[str]:
    """
    从 model name 推断翻译 task。
    'lumina-translate-zh' → 'translate_to_zh'
    'lumina-translate-en' → 'translate_to_en'
    其他 → None
    """
    if model and model.lower().startswith(_TRANSLATE_MODEL_PREFIX):
        lang = model.lower()[len(_TRANSLATE_MODEL_PREFIX):]
        return f"translate_to_{lang}" if lang else None
    return None


def _dedup_translation(text: str) -> str:
    """
    检测并截断翻译输出中的重复 loop。

    策略：寻找一段短语，使其在文本中「紧密连续」出现 >= 3 次，
    即相邻两次出现之间的间隔 <= 5 字符（中间只有"，"、"并"等连接词）。
    这能区分真正的 loop（紧密反复）和正常引用列表（年份/姓名分散出现）。
    """
    if not text or len(text) < 60:
        return text
    max_phrase = min(100, len(text) // 3)
    for phrase_len in range(max_phrase, _REPEAT_MIN_PHRASE_LEN - 1, -1):
        for start in range(0, len(text) - phrase_len * 3):
            phrase = text[start:start + phrase_len]
            if not any(c.isalpha() or '\u4e00' <= c <= '\u9fff' for c in phrase):
                continue
            try:
                matches = list(re.finditer(re.escape(phrase), text))
            except re.error:
                continue
            if len(matches) < _REPEAT_MAX_COUNT:
                continue
            # 严格判断「紧密连续」：前两个相邻出现之间的间隔均 <= 5 字符
            gap01 = matches[1].start() - matches[0].end()
            gap12 = matches[2].start() - matches[1].end()
            if gap01 > 5 or gap12 > 5:
                continue
            cut = matches[1].end()
            logger.warning(
                "Translation loop detected (phrase_len=%d, count=%d, gap=%d/%d), truncating at %d/%d chars",
                phrase_len, len(matches), gap01, gap12, cut, len(text),
            )
            return text[:cut].rstrip()
    return text


@router.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest, raw: Request):
    req_id = f"chatcmpl-{random_uuid()}"

    messages = to_provider_messages(request.messages)
    system_override = extract_system_override(request.messages)
    if not any(message["role"] == "user" for message in messages):
        raise HTTPException(status_code=400, detail="No user message found")

    # 检测翻译任务（来自 pdf_translate.py 通过 pdf2zh 发出的请求）
    translate_task = _resolve_translate_task(request.model or "")
    task = translate_task or "chat"

    # 翻译任务参数覆写：
    #   max_tokens 不足时补到下限（pdf2zh 不传 max_tokens，512 会截断长段落）
    #   presence_penalty 保留适中值抑制重复循环（归零会导致模型陷入 loop）
    #   repetition_penalty 适当加强进一步抑制重复
    max_tokens = request.max_tokens
    presence_penalty = request.presence_penalty
    repetition_penalty = request.repetition_penalty
    if translate_task:
        if max_tokens is None or max_tokens < _TRANSLATE_MIN_MAX_TOKENS:
            max_tokens = _TRANSLATE_MIN_MAX_TOKENS
        if presence_penalty is None:
            presence_penalty = 1.0
        if repetition_penalty is None:
            repetition_penalty = 1.3

    if request.stream:
        return StreamingResponse(
            _stream_chat(
                request, raw, messages, req_id, system_override,
                task=task,
                max_tokens=max_tokens,
                presence_penalty=presence_penalty,
                repetition_penalty=repetition_penalty,
            ),
            media_type="text/event-stream",
        )

    text = await run_chat_messages(
        raw,
        messages=messages,
        task=task,
        origin="chat_api",
        client_model=request.model,
        request_id=req_id,
        system_override=system_override,
        max_tokens=max_tokens,
        temperature=request.temperature,
        top_p=request.top_p,
        top_k=request.top_k,
        min_p=request.min_p,
        presence_penalty=presence_penalty,
        repetition_penalty=repetition_penalty,
    )
    if translate_task:
        text = _dedup_translation(text)
    return ChatCompletionResponse(
        id=req_id,
        model=request.model,
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=text)
            )
        ],
        usage=UsageInfo(),
    )


async def _stream_chat(
    request: ChatCompletionRequest,
    raw_req: Request,
    messages: list[dict],
    req_id: str,
    system_override: Optional[str] = None,
    *,
    task: str = "chat",
    max_tokens: Optional[int] = None,
    presence_penalty: Optional[float] = None,
    repetition_penalty: Optional[float] = None,
):
    from lumina.api.server import raw_request_disconnected

    finish_reason = "stop"
    try:
        async for token in stream_chat_messages(
            raw_req,
            messages=messages,
            task=task,
            origin="chat_api",
            client_model=request.model,
            request_id=req_id,
            system_override=system_override,
            max_tokens=max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            top_k=request.top_k,
            min_p=request.min_p,
            presence_penalty=presence_penalty,
            repetition_penalty=repetition_penalty,
        ):
            chunk = ChatCompletionStreamResponse(
                id=req_id,
                model=request.model,
                choices=[
                    ChatCompletionStreamChoice(
                        delta=ChatCompletionStreamDelta(content=token)
                    )
                ],
            )
            yield f"data: {chunk.model_dump_json()}\n\n"
            if await raw_request_disconnected(raw_req):
                break
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error("stream_chat error: %s", e)
        finish_reason = "error"
    end_chunk = ChatCompletionStreamResponse(
        id=req_id,
        model=request.model,
        choices=[
            ChatCompletionStreamChoice(
                delta=ChatCompletionStreamDelta(),
                finish_reason=finish_reason,
            )
        ],
    )
    yield f"data: {end_chunk.model_dump_json()}\n\n"
    yield "data: [DONE]\n\n"
