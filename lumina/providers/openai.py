"""
OpenAIProvider：将请求转发到任意 OpenAI 兼容的 HTTP API。

适用于：
  - 远程 OpenAI / Azure OpenAI
  - 自部署的 vLLM / Ollama / LocalAI
  - 其他 Lumina 实例（级联）
"""
import json
from typing import AsyncIterator, Optional

import aiohttp

from .base import BaseProvider


class OpenAIProvider(BaseProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str = "lumina",
        model: str = "lumina",
        timeout: int = 120,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        user_text: str,
        system: Optional[str],
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_text})
        return {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }

    async def generate_stream(
        self,
        user_text: str,
        system: Optional[str],
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        payload = self._payload(user_text, system, max_tokens, temperature, stream=True)
        url = f"{self.base_url}/chat/completions"

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                resp.raise_for_status()
                async for raw_line in resp.content:
                    line = raw_line.decode().strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"]
                        content = delta.get("content")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

    async def generate(
        self,
        user_text: str,
        system: Optional[str],
        max_tokens: int,
        temperature: float,
    ) -> str:
        payload = self._payload(user_text, system, max_tokens, temperature, stream=False)
        url = f"{self.base_url}/chat/completions"

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data["choices"][0]["message"]["content"]
