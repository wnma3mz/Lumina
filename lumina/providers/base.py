"""
Provider 抽象基类。

所有 LLM 后端实现此接口，调用方（LLMEngine）只依赖这个抽象，
不关心底层是本地模型还是远程 API。
"""
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

from lumina.sampling import (
    DEFAULT_MIN_P,
    DEFAULT_PRESENCE_PENALTY,
    DEFAULT_REPETITION_PENALTY,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
)


class BaseProvider(ABC):

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

    def load(self):
        """可选的同步初始化（本地模型加载用）。"""

    @property
    def is_ready(self) -> bool:
        """Provider 是否已就绪。"""
        return True
