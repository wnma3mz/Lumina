"""
lumina/api/routers/chat.py — Chat Completions 路由（OpenAI 兼容）
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

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
from lumina.request_context import request_context

router = APIRouter(tags=["chat"])

logger = logging.getLogger("lumina")


@router.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest, raw: Request):
    llm = raw.app.state.llm
    req_id = f"chatcmpl-{random_uuid()}"

    system_override: Optional[str] = None
    system_msg = next((m for m in request.messages if m.role == "system"), None)
    if system_msg is not None:
        system_override = (
            system_msg.content
            if isinstance(system_msg.content, str)
            else " ".join(
                getattr(c, "text", "")
                for c in system_msg.content
                if getattr(c, "type", "text") == "text"
            )
        )

    user_msg = next(
        (m for m in reversed(request.messages) if m.role == "user"), None
    )
    if user_msg is None:
        raise HTTPException(status_code=400, detail="No user message found")

    user_text = (
        user_msg.content
        if isinstance(user_msg.content, str)
        else " ".join(
            getattr(c, "text", "")
            for c in user_msg.content
            if getattr(c, "type", "text") == "text"
        )
    )

    if request.stream:
        return StreamingResponse(
            _stream_chat(request, raw, user_text, req_id, system_override),
            media_type="text/event-stream",
        )

    with request_context(
        origin="chat_api",
        stream=False,
        client_model=request.model,
        request_id=req_id,
    ):
        text = await llm.generate(
            user_text,
            task="chat",
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            system=system_override,
            top_k=request.top_k,
            min_p=request.min_p,
            presence_penalty=request.presence_penalty,
            repetition_penalty=request.repetition_penalty,
        )
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
    user_text: str,
    req_id: str,
    system_override: Optional[str] = None,
):
    from lumina.api.server import raw_request_disconnected

    llm = raw_req.app.state.llm
    finish_reason = "stop"
    try:
        with request_context(
            origin="chat_api",
            stream=True,
            client_model=request.model,
            request_id=req_id,
        ):
            async for token in llm.generate_stream(
                user_text,
                task="chat",
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                top_p=request.top_p,
                system=system_override,
                top_k=request.top_k,
                min_p=request.min_p,
                presence_penalty=request.presence_penalty,
                repetition_penalty=request.repetition_penalty,
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
