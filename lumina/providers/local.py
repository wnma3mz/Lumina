"""
LocalProvider：使用本地 mlx-lm 模型进行推理（默认 Provider）。

并发策略：
  MLX 的 GPU 推理不可真正并行，但可以通过请求队列做 Dynamic Batching——
  把短时间内到达的多个请求合并成一个 batch 一起推理，显著提升吞吐量。

  实现方式：
    - 每个 generate_stream 调用将请求放入全局 _RequestQueue
    - 后台 BatchWorker 轮询队列，收集 pending 请求后一起送入 mlx-lm
    - 每个请求独立维护自己的结果 asyncio.Queue，流式 yield 给调用方
"""
import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional
import uuid

import mlx.core as mx
from mlx_lm import load, stream_generate
from mlx_lm.sample_utils import make_sampler

from .base import BaseProvider

# 每次 batch 最多合并的请求数（避免 OOM）
_MAX_BATCH = 8
# 批次聚合等待窗口（秒）——等这么久收集更多请求后再一起处理
_BATCH_WINDOW = 0.02


@dataclass
class _PendingRequest:
    request_id: str
    prompt: str
    max_tokens: int
    temperature: float
    # 结果通道：worker 通过此 queue 向调用方 yield token
    result_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    done: bool = False


class LocalProvider(BaseProvider):
    def __init__(self, model_path: str):
        self.model_path = model_path
        self._model = None
        self._tokenizer = None
        self._queue: asyncio.Queue[_PendingRequest] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

    def load(self):
        self._model, self._tokenizer = load(self.model_path)
        mx.eval(self._model.parameters())

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    def _ensure_worker(self):
        """确保后台 BatchWorker 已启动（懒启动，首次请求时创建）。"""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._batch_worker())

    def _build_prompt(self, system: str, user_text: str) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        return self._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )

    def _run_batch_sync(self, requests: List[_PendingRequest], loop: asyncio.AbstractEventLoop):
        """
        在 executor 线程中同步执行一批请求。
        mlx-lm 当前不原生支持多 prompt batch 流式，因此逐个串行推理，
        但仍比"锁住整个 generate_stream 协程"更优：
          - 批次内请求顺序处理，批次外请求可在当前批次收集期间入队
          - 未来可换成真正的 batch forward（mlx-lm roadmap）
        """
        for req in requests:
            sampler = make_sampler(temp=req.temperature, top_p=0.9)
            try:
                for response in stream_generate(
                    self._model,
                    self._tokenizer,
                    prompt=req.prompt,
                    max_tokens=req.max_tokens,
                    sampler=sampler,
                ):
                    asyncio.run_coroutine_threadsafe(
                        req.result_queue.put(response.text), loop
                    )
                    if response.finish_reason is not None:
                        break
            except Exception as e:
                asyncio.run_coroutine_threadsafe(
                    req.result_queue.put(RuntimeError(str(e))), loop
                )
            finally:
                asyncio.run_coroutine_threadsafe(
                    req.result_queue.put(None), loop  # 结束哨兵
                )

    async def _batch_worker(self):
        """
        后台协程：持续从队列收集请求，凑成 batch 后送去推理。
        """
        loop = asyncio.get_running_loop()
        while True:
            # 等待至少一个请求
            first = await self._queue.get()
            batch = [first]

            # 在短窗口内继续收集更多请求（Dynamic Batching）
            deadline = loop.time() + _BATCH_WINDOW
            while len(batch) < _MAX_BATCH:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    req = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    batch.append(req)
                except asyncio.TimeoutError:
                    break

            # 在线程池中执行同步推理，避免阻塞事件循环
            await loop.run_in_executor(None, self._run_batch_sync, batch, loop)

    async def generate_stream(
        self,
        user_text: str,
        system: Optional[str],
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        if not self.is_ready:
            raise RuntimeError("LocalProvider not loaded. Call load() first.")

        self._ensure_worker()

        system_str = system or "You are a helpful assistant."
        prompt = self._build_prompt(system_str, user_text)

        req = _PendingRequest(
            request_id=uuid.uuid4().hex,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        await self._queue.put(req)

        # 从结果队列 yield token
        while True:
            item = await req.result_queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
