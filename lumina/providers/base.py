"""
Provider 抽象基类。

所有 LLM 后端实现此接口，调用方（LLMEngine）只依赖这个抽象，
不关心底层是本地模型还是远程 API。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

from lumina.engine.sampling import (
    DEFAULT_MIN_P,
    DEFAULT_PRESENCE_PENALTY,
    DEFAULT_REPETITION_PENALTY,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
)


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_text: bool = True
    supports_messages: bool = True
    supports_streaming: bool = True
    supports_image_input: bool = False
    supported_sampling_params: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "max_tokens",
                "temperature",
                "top_p",
                "top_k",
                "min_p",
                "presence_penalty",
                "repetition_penalty",
            }
        )
    )


class BaseProvider(ABC):
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    @staticmethod
    def _flatten_messages(messages: list[dict[str, Any]]) -> str:
        chunks: list[str] = []
        for msg in messages:
            role = str(msg.get("role", "user"))
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content.strip()
                if text:
                    chunks.append(f"{role}: {text}")
                continue
            if isinstance(content, list):
                text_parts: list[str] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text = str(part.get("text", "")).strip()
                        if text:
                            text_parts.append(text)
                        continue
                    raise NotImplementedError("当前模型后端不支持图片输入")
                if text_parts:
                    chunks.append(f"{role}: {' '.join(text_parts)}")
                continue
            raise TypeError("消息 content 格式不支持")
        return "\n\n".join(chunks).strip()

    def _validate_messages(self, messages: list[dict[str, Any]]) -> None:
        if not self.capabilities.supports_messages:
            raise NotImplementedError("当前模型后端不支持 messages 输入")
        if self.capabilities.supports_image_input:
            return
        for msg in messages:
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    raise NotImplementedError("当前模型后端不支持图片输入")

    @abstractmethod
    async def generate_stream(
        self,
        user_text: str,
        system: Optional[str],
        max_tokens: int,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_p: float = DEFAULT_MIN_P,
        presence_penalty: float = DEFAULT_PRESENCE_PENALTY,
        repetition_penalty: float = DEFAULT_REPETITION_PENALTY,
    ) -> AsyncIterator[str]:
        """流式生成文本，逐 token yield。"""
        ...

    async def generate(
        self,
        user_text: str,
        system: Optional[str],
        max_tokens: int,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_p: float = DEFAULT_MIN_P,
        presence_penalty: float = DEFAULT_PRESENCE_PENALTY,
        repetition_penalty: float = DEFAULT_REPETITION_PENALTY,
    ) -> str:
        """非流式，收集完整结果。默认实现基于 generate_stream。"""
        parts = []
        async for token in self.generate_stream(
            user_text,
            system,
            max_tokens,
            temperature,
            top_p,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            repetition_penalty=repetition_penalty,
        ):
            parts.append(token)
        return "".join(parts)

    async def generate_messages_stream(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str],
        max_tokens: int,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_p: float = DEFAULT_MIN_P,
        presence_penalty: float = DEFAULT_PRESENCE_PENALTY,
        repetition_penalty: float = DEFAULT_REPETITION_PENALTY,
    ) -> AsyncIterator[str]:
        self._validate_messages(messages)
        user_text = self._flatten_messages(messages)
        async for token in self.generate_stream(
            user_text,
            system,
            max_tokens,
            temperature,
            top_p,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            repetition_penalty=repetition_penalty,
        ):
            yield token

    async def generate_messages(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str],
        max_tokens: int,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_p: float = DEFAULT_MIN_P,
        presence_penalty: float = DEFAULT_PRESENCE_PENALTY,
        repetition_penalty: float = DEFAULT_REPETITION_PENALTY,
    ) -> str:
        self._validate_messages(messages)
        user_text = self._flatten_messages(messages)
        return await self.generate(
            user_text,
            system,
            max_tokens,
            temperature,
            top_p,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            repetition_penalty=repetition_penalty,
        )

    def load(self):
        """可选的同步初始化（本地模型加载用）。"""

    @property
    def is_ready(self) -> bool:
        """Provider 是否已就绪。"""
        return True
