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
from .message_parts import messages_include_images, to_provider_text


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


@dataclass(frozen=True)
class ProviderMetadata:
    provider_type: str
    model: Optional[str] = None


class BaseProvider(ABC):
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    @property
    def metadata(self) -> ProviderMetadata:
        model = None
        for attr in ("model", "model_path", "_model_path"):
            value = getattr(self, attr, None)
            if value:
                model = str(value)
                break
        provider_type = type(self).__name__.removesuffix("Provider").lower()
        return ProviderMetadata(provider_type=provider_type, model=model)

    @staticmethod
    def _flatten_messages(messages: list[dict[str, Any]]) -> str:
        return to_provider_text(messages)

    def _validate_messages(self, messages: list[dict[str, Any]]) -> None:
        if not self.capabilities.supports_messages:
            raise NotImplementedError("当前模型后端不支持 messages 输入")
        if not self.capabilities.supports_image_input and messages_include_images(messages):
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

    async def close(self) -> None:
        """可选的异步清理钩子（远程连接池等资源释放用）。"""

    @property
    def is_ready(self) -> bool:
        """Provider 是否已就绪。"""
        return True
