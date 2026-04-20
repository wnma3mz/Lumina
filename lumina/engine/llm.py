"""
LLMEngine：任务路由层。

负责：
  1. 根据 task 名查 system prompt（配置文件 → 请求显式 system）
  2. 委托给 Provider 执行实际推理（LocalProvider 或 OpenAIProvider）
  3. 统一记录请求历史（异步入队，后台落盘）

不负责：模型加载、HTTP 通信——这些由各自 Provider 处理。
"""
import asyncio
import hashlib
import time
import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Dict, Optional

from lumina.providers.base import BaseProvider
from lumina.engine.request_context import get_request_context
from lumina.engine import request_history
from lumina.engine.sampling import resolve_sampling


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

    @property
    def provider_capabilities(self):
        return self._provider.capabilities

    def _resolve_system(self, task: str, system_override: Optional[str]) -> Optional[str]:
        """
        请求显式传入 system_override 时直接使用，忽略配置。
        否则按 task 查配置，找不到返回 None（由 Provider 自行处理默认值）。

        语义约定（调用方须知）：
          system=None  → 使用配置文件里该 task 的 system prompt（或 "chat" 兜底）
          system=""    → 显式传空字符串，会直接覆盖配置，省略 system 段
          两者不等价；不想传 system 时应传 None，而非 ""。
        """
        if system_override is not None:
            return system_override
        return self._system_prompts.get(task) or self._system_prompts.get("chat")

    def _provider_type(self) -> str:
        name = type(self._provider).__name__
        return name.removesuffix("Provider").lower()

    def _provider_model(self) -> Optional[str]:
        for attr in ("model", "model_path", "_model_path"):
            value = getattr(self._provider, attr, None)
            if value:
                return str(value)
        return None

    @staticmethod
    def _text_hash(text: Optional[str]) -> Optional[str]:
        if text is None:
            return None
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _text_chars(text: Optional[str]) -> int:
        return len(text) if text is not None else 0

    def _history_entry(
        self,
        *,
        request_id: str,
        task: str,
        stream: bool,
        system_prompt: Optional[str],
        user_text: str,
        response_text: Optional[str],
        status: str,
        started_at: datetime,
        duration_ms: int,
        error: Optional[BaseException] = None,
    ) -> dict:
        ctx = get_request_context()
        ended_at = datetime.now()
        return {
            "ts_start": started_at.isoformat(),
            "ts_end": ended_at.isoformat(),
            "request_id": request_id,
            "origin": ctx.get("origin") or "unknown",
            "task": task,
            "stream": stream if ctx.get("stream") is None else bool(ctx["stream"]),
            "provider_type": self._provider_type(),
            "provider_model": self._provider_model(),
            "client_model": ctx.get("client_model"),
            "max_tokens": None,
            "temperature": None,
            "top_p": None,
            "top_k": None,
            "min_p": None,
            "presence_penalty": None,
            "repetition_penalty": None,
            "system_text": system_prompt,
            "user_text": user_text,
            "response_text": response_text,
            "system_sha256": self._text_hash(system_prompt),
            "user_sha256": self._text_hash(user_text),
            "response_sha256": self._text_hash(response_text),
            "system_chars": self._text_chars(system_prompt),
            "user_chars": self._text_chars(user_text),
            "response_chars": self._text_chars(response_text),
            "status": status,
            "error_type": type(error).__name__ if error is not None else None,
            "error_message": str(error) if error is not None else None,
            "duration_ms": duration_ms,
        }

    def _resolve_sampling(
        self,
        max_tokens: Optional[int],
        temperature: Optional[float],
        top_p: Optional[float],
        top_k: Optional[int],
        min_p: Optional[float],
        presence_penalty: Optional[float],
        repetition_penalty: Optional[float],
    ) -> dict:
        from lumina.config import get_config
        cfg = get_config()
        return resolve_sampling(
            cfg.provider.sampling,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            repetition_penalty=repetition_penalty,
        )

    @staticmethod
    def _messages_to_history_text(messages: list[dict[str, Any]]) -> str:
        chunks: list[str] = []
        for msg in messages:
            role = str(msg.get("role", "user"))
            content = msg.get("content", "")
            if isinstance(content, str):
                chunks.append(f"{role}: {content}")
                continue
            if not isinstance(content, list):
                continue
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "text":
                    text = str(item.get("text", "")).strip()
                    if text:
                        parts.append(text)
                elif item_type == "image_url":
                    payload = item.get("image_url") or {}
                    url = str(payload.get("url", ""))
                    if url.startswith("data:"):
                        parts.append("[image:data-url omitted]")
                    elif url:
                        parts.append(f"[image:{url}]")
                    else:
                        parts.append("[image]")
            chunks.append(f"{role}: {' '.join(parts).strip()}")
        return "\n\n".join(chunk for chunk in chunks if chunk.strip()).strip()

    @property
    def provider_model_name(self) -> str:
        return self._provider_model() or "lumina"

    async def generate_stream(
        self,
        user_text: str,
        task: str = "chat",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        system: Optional[str] = None,
        *,
        top_k: Optional[int] = None,
        min_p: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        repetition_penalty: Optional[float] = None,
    ) -> AsyncIterator[str]:
        system_prompt = self._resolve_system(task, system)
        params = self._resolve_sampling(max_tokens, temperature, top_p, top_k, min_p, presence_penalty, repetition_penalty)
        started_at = datetime.now()
        started_perf = time.perf_counter()
        request_id = get_request_context().get("request_id") or uuid.uuid4().hex
        chunks = []
        status = "ok"
        error = None
        try:
            async for token in self._provider.generate_stream(
                user_text=user_text,
                system=system_prompt,
                max_tokens=params["max_tokens"],
                temperature=params["temperature"],
                top_p=params["top_p"],
                top_k=params["top_k"],
                min_p=params["min_p"],
                presence_penalty=params["presence_penalty"],
                repetition_penalty=params["repetition_penalty"],
            ):
                chunks.append(token)
                yield token
        except asyncio.CancelledError as e:
            status = "cancelled"
            error = e
            raise
        except Exception as e:
            status = "error"
            error = e
            raise
        finally:
            entry = self._history_entry(
                request_id=request_id,
                task=task,
                stream=True,
                system_prompt=system_prompt,
                user_text=user_text,
                response_text="".join(chunks),
                status=status,
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started_perf) * 1000),
                error=error,
            )
            entry.update(params)
            request_history.record(entry)

    async def generate(
        self,
        user_text: str,
        task: str = "chat",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        system: Optional[str] = None,
        *,
        top_k: Optional[int] = None,
        min_p: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        repetition_penalty: Optional[float] = None,
    ) -> str:
        system_prompt = self._resolve_system(task, system)
        params = self._resolve_sampling(max_tokens, temperature, top_p, top_k, min_p, presence_penalty, repetition_penalty)
        started_at = datetime.now()
        started_perf = time.perf_counter()
        request_id = get_request_context().get("request_id") or uuid.uuid4().hex
        status = "ok"
        error = None
        response_text = ""
        try:
            response_text = await self._provider.generate(
                user_text=user_text,
                system=system_prompt,
                max_tokens=params["max_tokens"],
                temperature=params["temperature"],
                top_p=params["top_p"],
                top_k=params["top_k"],
                min_p=params["min_p"],
                presence_penalty=params["presence_penalty"],
                repetition_penalty=params["repetition_penalty"],
            )
            return response_text
        except asyncio.CancelledError as e:
            status = "cancelled"
            error = e
            raise
        except Exception as e:
            status = "error"
            error = e
            raise
        finally:
            entry = self._history_entry(
                request_id=request_id,
                task=task,
                stream=False,
                system_prompt=system_prompt,
                user_text=user_text,
                response_text=response_text,
                status=status,
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started_perf) * 1000),
                error=error,
            )
            entry.update(params)
            request_history.record(entry)

    async def generate_messages_stream(
        self,
        messages: list[dict[str, Any]],
        task: str = "chat",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        system: Optional[str] = None,
        *,
        top_k: Optional[int] = None,
        min_p: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        repetition_penalty: Optional[float] = None,
    ) -> AsyncIterator[str]:
        system_prompt = self._resolve_system(task, system)
        params = self._resolve_sampling(max_tokens, temperature, top_p, top_k, min_p, presence_penalty, repetition_penalty)
        started_at = datetime.now()
        started_perf = time.perf_counter()
        request_id = get_request_context().get("request_id") or uuid.uuid4().hex
        chunks = []
        status = "ok"
        error = None
        history_text = self._messages_to_history_text(messages)
        try:
            async for token in self._provider.generate_messages_stream(
                messages=messages,
                system=system_prompt,
                max_tokens=params["max_tokens"],
                temperature=params["temperature"],
                top_p=params["top_p"],
                top_k=params["top_k"],
                min_p=params["min_p"],
                presence_penalty=params["presence_penalty"],
                repetition_penalty=params["repetition_penalty"],
            ):
                chunks.append(token)
                yield token
        except asyncio.CancelledError as e:
            status = "cancelled"
            error = e
            raise
        except Exception as e:
            status = "error"
            error = e
            raise
        finally:
            entry = self._history_entry(
                request_id=request_id,
                task=task,
                stream=True,
                system_prompt=system_prompt,
                user_text=history_text,
                response_text="".join(chunks),
                status=status,
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started_perf) * 1000),
                error=error,
            )
            entry.update(params)
            request_history.record(entry)

    async def generate_messages(
        self,
        messages: list[dict[str, Any]],
        task: str = "chat",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        system: Optional[str] = None,
        *,
        top_k: Optional[int] = None,
        min_p: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        repetition_penalty: Optional[float] = None,
    ) -> str:
        system_prompt = self._resolve_system(task, system)
        params = self._resolve_sampling(max_tokens, temperature, top_p, top_k, min_p, presence_penalty, repetition_penalty)
        started_at = datetime.now()
        started_perf = time.perf_counter()
        request_id = get_request_context().get("request_id") or uuid.uuid4().hex
        status = "ok"
        error = None
        response_text = ""
        history_text = self._messages_to_history_text(messages)
        try:
            response_text = await self._provider.generate_messages(
                messages=messages,
                system=system_prompt,
                max_tokens=params["max_tokens"],
                temperature=params["temperature"],
                top_p=params["top_p"],
                top_k=params["top_k"],
                min_p=params["min_p"],
                presence_penalty=params["presence_penalty"],
                repetition_penalty=params["repetition_penalty"],
            )
            return response_text
        except asyncio.CancelledError as e:
            status = "cancelled"
            error = e
            raise
        except Exception as e:
            status = "error"
            error = e
            raise
        finally:
            entry = self._history_entry(
                request_id=request_id,
                task=task,
                stream=False,
                system_prompt=system_prompt,
                user_text=history_text,
                response_text=response_text,
                status=status,
                started_at=started_at,
                duration_ms=int((time.perf_counter() - started_perf) * 1000),
                error=error,
            )
            entry.update(params)
            request_history.record(entry)
