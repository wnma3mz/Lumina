from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class MessagePart:
    type: str
    text: str = ""
    image_url: str = ""


@dataclass(frozen=True)
class MessageEntry:
    role: str
    text: Optional[str] = None
    parts: tuple[MessagePart, ...] = ()


def parse_messages(messages: list[dict[str, Any]]) -> list[MessageEntry]:
    parsed: list[MessageEntry] = []
    for msg in messages:
        role = str(msg.get("role", "user"))
        content = msg.get("content", "")
        if isinstance(content, str):
            parsed.append(MessageEntry(role=role, text=content))
            continue
        if not isinstance(content, list):
            raise TypeError("消息 content 格式不支持")
        parts: list[MessagePart] = []
        for raw_part in content:
            if not isinstance(raw_part, dict):
                continue
            part_type = str(raw_part.get("type", "") or "")
            if part_type == "text":
                parts.append(MessagePart(type="text", text=str(raw_part.get("text", ""))))
                continue
            if part_type == "image_url":
                payload = raw_part.get("image_url") or {}
                parts.append(MessagePart(type="image_url", image_url=str(payload.get("url", ""))))
                continue
            parts.append(MessagePart(type=part_type or "unknown"))
        parsed.append(MessageEntry(role=role, parts=tuple(parts)))
    return parsed


def messages_include_images(messages: list[dict[str, Any]]) -> bool:
    return any(part.type == "image_url" for entry in parse_messages(messages) for part in entry.parts)


def to_provider_text(messages: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for entry in parse_messages(messages):
        if entry.text is not None:
            text = entry.text.strip()
            if text:
                chunks.append(f"{entry.role}: {text}")
            continue
        text_parts: list[str] = []
        for part in entry.parts:
            if part.type == "text":
                text = part.text.strip()
                if text:
                    text_parts.append(text)
                continue
            raise NotImplementedError("当前模型后端不支持图片输入")
        if text_parts:
            chunks.append(f"{entry.role}: {' '.join(text_parts)}")
    return "\n\n".join(chunks).strip()


def to_history_text(messages: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for entry in parse_messages(messages):
        if entry.text is not None:
            chunks.append(f"{entry.role}: {entry.text}")
            continue
        parts: list[str] = []
        for part in entry.parts:
            if part.type == "text":
                text = part.text.strip()
                if text:
                    parts.append(text)
                continue
            if part.type == "image_url":
                url = part.image_url
                if url.startswith("data:"):
                    parts.append("[image:data-url omitted]")
                elif url:
                    parts.append(f"[image:{url}]")
                else:
                    parts.append("[image]")
        text = " ".join(parts).strip()
        if text:
            chunks.append(f"{entry.role}: {text}")
    return "\n\n".join(chunk for chunk in chunks if chunk.strip()).strip()


def to_vlm_messages_and_images(
    messages: list[dict[str, Any]],
    *,
    system: Optional[str],
    normalize_image_input: Callable[[str], Any],
) -> tuple[list[dict[str, str]], list[Any]]:
    vlm_messages: list[dict[str, str]] = []
    image_inputs: list[Any] = []
    if system:
        vlm_messages.append({"role": "system", "content": system})
    for entry in parse_messages(messages):
        if entry.text is not None:
            vlm_messages.append({"role": entry.role, "content": entry.text})
            continue
        text_parts: list[str] = []
        for part in entry.parts:
            if part.type == "text":
                text = part.text.strip()
                if text:
                    text_parts.append(text)
                continue
            if part.type == "image_url":
                image_inputs.append(normalize_image_input(part.image_url.strip()))
                continue
            raise ValueError(f"不支持的消息内容类型：{part.type}")
        vlm_messages.append({"role": entry.role, "content": "\n".join(text_parts).strip()})
    if not image_inputs:
        raise ValueError("未找到图片输入")
    return vlm_messages, image_inputs
