"""
OpenAIProvider：将请求转发到任意 OpenAI 兼容的 HTTP API。

适用于：
  - 远程 OpenAI / Azure OpenAI
  - 自部署的 vLLM / Ollama / LocalAI
  - 其他 Lumina 实例（级联）
"""
import json
from typing import Any, AsyncIterator, Optional

import aiohttp

from .base import BaseProvider, ProviderCapabilities
from lumina.engine.sampling import (
    DEFAULT_MIN_P,
    DEFAULT_PRESENCE_PENALTY,
    DEFAULT_REPETITION_PENALTY,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
)


class OpenAIProvider(BaseProvider):
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_image_input=True)

    def __init__(
        self,
        base_url: str,
        api_key: str = "lumina",
        model: str = "lumina",
        timeout: int = 120,
        strict_openai: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        # strict_openai=True 时不发送 top_k/min_p/repetition_penalty 等 mlx 专有字段，
        # 用于对接标准 OpenAI / Azure OpenAI，避免 400 "Unrecognized field"
        self.strict_openai = strict_openai

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
        stream: bool,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_p: float = DEFAULT_MIN_P,
        presence_penalty: float = DEFAULT_PRESENCE_PENALTY,
        repetition_penalty: float = DEFAULT_REPETITION_PENALTY,
    ) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_text})
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "presence_penalty": presence_penalty,
            "stream": stream,
        }
        if not self.strict_openai:
            payload["top_k"] = top_k
            payload["min_p"] = min_p
            payload["repetition_penalty"] = repetition_penalty
        return payload

    def _payload_messages(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str],
        max_tokens: int,
        stream: bool,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_p: float = DEFAULT_MIN_P,
        presence_penalty: float = DEFAULT_PRESENCE_PENALTY,
        repetition_penalty: float = DEFAULT_REPETITION_PENALTY,
    ) -> dict:
        merged_messages: list[dict[str, Any]] = []
        if system:
            merged_messages.append({"role": "system", "content": system})
        merged_messages.extend(messages)
        payload: dict = {
            "model": self.model,
            "messages": merged_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "presence_penalty": presence_penalty,
            "stream": stream,
        }
        if not self.strict_openai:
            payload["top_k"] = top_k
            payload["min_p"] = min_p
            payload["repetition_penalty"] = repetition_penalty
        return payload

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
        payload = self._payload(
            user_text,
            system,
            max_tokens,
            stream=True,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            repetition_penalty=repetition_penalty,
        )
        url = f"{self.base_url}/chat/completions"

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                resp.raise_for_status()
                buf = ""
                async for chunk in resp.content.iter_any():
                    buf += chunk.decode(errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            return
                        try:
                            obj = json.loads(data)
                            delta = obj["choices"][0]["delta"]
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
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float = DEFAULT_TOP_P,
        *,
        top_k: int = DEFAULT_TOP_K,
        min_p: float = DEFAULT_MIN_P,
        presence_penalty: float = DEFAULT_PRESENCE_PENALTY,
        repetition_penalty: float = DEFAULT_REPETITION_PENALTY,
    ) -> str:
        payload = self._payload(
            user_text,
            system,
            max_tokens,
            stream=False,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            repetition_penalty=repetition_penalty,
        )
        url = f"{self.base_url}/chat/completions"

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()
                try:
                    usage = data.get("usage") or {}
                    pt = int(usage.get("prompt_tokens") or 0)
                    ct = int(usage.get("completion_tokens") or 0)
                    if pt or ct:
                        from lumina.engine.token_counter import set_token_counts
                        set_token_counts(pt, ct)
                except Exception:
                    pass
                return data["choices"][0]["message"]["content"] or ""

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
        payload = self._payload_messages(
            messages,
            system,
            max_tokens,
            stream=True,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            repetition_penalty=repetition_penalty,
        )
        url = f"{self.base_url}/chat/completions"

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                resp.raise_for_status()
                buf = ""
                async for chunk in resp.content.iter_any():
                    buf += chunk.decode(errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            return
                        try:
                            obj = json.loads(data)
                            delta = obj["choices"][0]["delta"]
                            content = delta.get("content")
                            if content:
                                yield content
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue

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
        payload = self._payload_messages(
            messages,
            system,
            max_tokens,
            stream=False,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            repetition_penalty=repetition_penalty,
        )
        url = f"{self.base_url}/chat/completions"

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(url, headers=self._headers(), json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()
                try:
                    usage = data.get("usage") or {}
                    pt = int(usage.get("prompt_tokens") or 0)
                    ct = int(usage.get("completion_tokens") or 0)
                    if pt or ct:
                        from lumina.engine.token_counter import set_token_counts
                        set_token_counts(pt, ct)
                except Exception:
                    pass
                return data["choices"][0]["message"]["content"] or ""
