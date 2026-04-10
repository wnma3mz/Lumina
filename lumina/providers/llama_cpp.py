"""
lumina/providers/llama_cpp.py — Windows / CPU-only 推理后端，基于 llama-cpp-python。

支持 CUDA GPU（n_gpu_layers=-1 全部上 GPU）和纯 CPU（n_gpu_layers=0）。
模型格式为 GGUF，可从 Hugging Face 下载。
"""
import asyncio
import threading
from typing import AsyncIterator, Optional

from .base import BaseProvider


class LlamaCppProvider(BaseProvider):
    """llama-cpp-python 推理后端，跨平台（Windows / Linux / macOS CPU）。"""

    def __init__(self, model_path: str, n_gpu_layers: int = -1, n_ctx: int = 4096):
        """
        Args:
            model_path:   GGUF 模型文件路径（本地路径）。
            n_gpu_layers: 放 GPU 的层数。-1 = 全部；0 = 纯 CPU。
            n_ctx:        上下文长度。
        """
        self._model_path = model_path
        self._n_gpu_layers = n_gpu_layers
        self._n_ctx = n_ctx
        self._llm = None

    def load(self):
        """同步加载模型（由 LLMEngine.load() 调用）。"""
        from llama_cpp import Llama  # type: ignore[import]
        self._llm = Llama(
            model_path=self._model_path,
            n_gpu_layers=self._n_gpu_layers,
            n_ctx=self._n_ctx,
            verbose=False,
        )

    @property
    def is_ready(self) -> bool:
        return self._llm is not None

    async def generate_stream(
        self,
        user_text: str,
        system: Optional[str],
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        """流式推理，逐 token yield。llama-cpp 同步流放入线程，通过 Queue 传给协程。"""
        if self._llm is None:
            raise RuntimeError("LlamaCppProvider 未加载模型，请先调用 load()")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_text})

        def _run():
            try:
                for chunk in self._llm.create_chat_completion(  # type: ignore[union-attr]
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=True,
                ):
                    delta = chunk["choices"][0].get("delta", {})
                    text = delta.get("content", "")
                    if text:
                        loop.call_soon_threadsafe(queue.put_nowait, text)
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=_run, daemon=True).start()

        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
