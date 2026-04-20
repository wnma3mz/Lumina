from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from fastapi import HTTPException, Request

from lumina.engine.request_context import request_context


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role", "user"))
    return str(getattr(message, "role", "user"))


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content", "")
    return getattr(message, "content", "")


def content_part_type(part: Any) -> Optional[str]:
    if isinstance(part, dict):
        return part.get("type")
    return getattr(part, "type", None)


def content_part_text(part: Any) -> str:
    if isinstance(part, dict):
        return str(part.get("text", ""))
    return str(getattr(part, "text", ""))


def content_part_image(part: Any) -> dict:
    if isinstance(part, dict):
        return dict(part.get("image_url") or {})
    image_url = getattr(part, "image_url", None)
    if image_url is None:
        return {}
    if hasattr(image_url, "model_dump"):
        return image_url.model_dump(exclude_none=True)
    return dict(image_url)


def extract_system_override(messages: list[Any]) -> Optional[str]:
    system_parts: list[str] = []
    for message in messages:
        if _message_role(message) != "system":
            continue
        content = _message_content(message)
        if isinstance(content, str):
            text = content.strip()
            if text:
                system_parts.append(text)
            continue
        for part in content:
            part_type = content_part_type(part)
            if part_type == "text":
                text = content_part_text(part).strip()
                if text:
                    system_parts.append(text)
                continue
            if part_type == "image_url":
                raise HTTPException(status_code=400, detail="System message 暂不支持 image_url")
    merged = "\n\n".join(system_parts).strip()
    return merged or None


def to_provider_messages(messages: list[Any]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = _message_role(message)
        if role == "system":
            continue
        content = _message_content(message)
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue
        parts: list[dict[str, Any]] = []
        for part in content:
            part_type = content_part_type(part)
            if part_type == "text":
                parts.append({"type": "text", "text": content_part_text(part)})
                continue
            if part_type == "image_url":
                parts.append({"type": "image_url", "image_url": content_part_image(part)})
                continue
            raise HTTPException(status_code=400, detail=f"Unsupported content type: {part_type}")
        converted.append({"role": role, "content": parts})
    return converted


def build_image_chat_messages(image_ref: str, *, instruction: str = "请根据 system prompt 处理这张图片。") -> list[dict[str, Any]]:
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": image_ref}},
        ],
    }]


async def run_chat_messages(
    raw: Request,
    *,
    messages: list[dict[str, Any]],
    task: str,
    origin: str,
    client_model: str = "lumina",
    request_id: Optional[str] = None,
    system_override: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    top_k: Optional[int] = None,
    min_p: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    repetition_penalty: Optional[float] = None,
) -> str:
    llm = raw.app.state.llm
    with request_context(
        origin=origin,
        stream=False,
        client_model=client_model,
        request_id=request_id,
    ):
        return await llm.generate_messages(
            messages=messages,
            task=task,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            system=system_override,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            repetition_penalty=repetition_penalty,
        )


async def stream_chat_messages(
    raw: Request,
    *,
    messages: list[dict[str, Any]],
    task: str,
    origin: str,
    client_model: str = "lumina",
    request_id: Optional[str] = None,
    system_override: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    top_k: Optional[int] = None,
    min_p: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    repetition_penalty: Optional[float] = None,
) -> AsyncIterator[str]:
    llm = raw.app.state.llm
    with request_context(
        origin=origin,
        stream=True,
        client_model=client_model,
        request_id=request_id,
    ):
        async for token in llm.generate_messages_stream(
            messages=messages,
            task=task,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            system=system_override,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            repetition_penalty=repetition_penalty,
        ):
            yield token
