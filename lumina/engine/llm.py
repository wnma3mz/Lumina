"""
LLMEngine：任务路由层。

负责：
  1. 根据 task 名查 system prompt（配置文件 → 请求显式 system）
  2. 委托给 Provider 执行实际推理（LocalProvider 或 OpenAIProvider）

不负责：模型加载、HTTP 通信——这些由各自 Provider 处理。
"""
from typing import AsyncIterator, Dict, Optional

from lumina.providers.base import BaseProvider


class LLMEngine:
    def __init__(self, provider: BaseProvider, system_prompts: Optional[Dict[str, str]] = None):
        self._provider = provider
        self._system_prompts: Dict[str, str] = system_prompts or {}

    def load(self):
        """初始化 Provider（本地模型加载、连接检查等）。"""
        self._provider.load()

    @property
    def is_loaded(self) -> bool:
        return self._provider.is_ready

    def _resolve_system(self, task: str, system_override: Optional[str]) -> Optional[str]:
        """
        请求显式传入 system_override 时直接使用，忽略配置。
        否则按 task 查配置，找不到返回 None（由 Provider 自行处理默认值）。
        """
        if system_override is not None:
            return system_override
        return self._system_prompts.get(task) or self._system_prompts.get("chat")

    async def generate_stream(
        self,
        user_text: str,
        task: str = "chat",
        max_tokens: int = 512,
        temperature: float = 0.3,
        system: Optional[str] = None,
    ) -> AsyncIterator[str]:
        system_prompt = self._resolve_system(task, system)
        async for token in self._provider.generate_stream(user_text, system_prompt, max_tokens, temperature):
            yield token

    async def generate(
        self,
        user_text: str,
        task: str = "chat",
        max_tokens: int = 512,
        temperature: float = 0.3,
        system: Optional[str] = None,
    ) -> str:
        system_prompt = self._resolve_system(task, system)
        return await self._provider.generate(user_text, system_prompt, max_tokens, temperature)
