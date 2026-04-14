"""
lumina/providers/scheduler.py — MlxBatchScheduler

将 mlx-lm BatchGenerator 的调度逻辑从 LocalProvider 中提取为独立类。

调用方式：
    scheduler = MlxBatchScheduler(
        model=..., tokenizer=...,
        batch_generator=..., batch_executor=..., loop=...,
        prepare_prompt_fn=provider._prepare_batch_generator_prompt,
        emit_token_fn=provider._emit_token_id_local,
    )
    await scheduler.run(prefill_queue, not_empty)

_RequestSlot 保留在 local.py，此模块通过延迟导入引用，避免循环 import。
"""
import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, List, Optional

from lumina.sampling import build_mlx_sampler

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("lumina")

try:
    _MLX_AVAILABLE = True
except ImportError:
    _MLX_AVAILABLE = False


class MlxBatchScheduler:
    """mlx-lm BatchGenerator 调度器（LocalProvider 默认路径）。

    将多请求批量提交给 mlx-lm BatchGenerator，每轮 .next() 推进一步，
    通过 _batch_slots 映射 uid → slot，把 token/终止信号投入各自的 token_queue。

    Args:
        model:              已加载的 mlx 模型实例。
        tokenizer:          已加载的 tokenizer 实例。
        batch_generator:    mlx_lm.generate.BatchGenerator 实例。
        batch_executor:     专属 ThreadPoolExecutor（隔离 GPU 线程），可为 None。
        loop:               当前 asyncio event loop（用于线程安全的 put）。
        prepare_prompt_fn:  Callable[[_RequestSlot], tuple[list[int], Optional[list]]]
                            由 LocalProvider._prepare_batch_generator_prompt 提供。
        emit_token_fn:      Callable[[_RequestSlot, int], None]
                            由 LocalProvider._emit_token_id_local 提供。
    """

    def __init__(
        self,
        model: Any,
        tokenizer: Any,
        batch_generator: Any,
        batch_executor: Optional["ThreadPoolExecutor"],
        loop: asyncio.AbstractEventLoop,
        prepare_prompt_fn: Callable,
        emit_token_fn: Callable,
    ) -> None:
        self._model = model
        self._tokenizer = tokenizer
        self._batch_generator = batch_generator
        self._batch_executor = batch_executor
        self._loop = loop
        self._prepare_prompt_fn = prepare_prompt_fn
        self._emit_token_fn = emit_token_fn
        self._batch_slots: dict = {}

    async def run(
        self,
        prefill_queue: asyncio.Queue,
        not_empty: asyncio.Event,
    ) -> None:
        """主调度循环。由 LocalProvider._mlx_batch_scheduler 调用。"""
        try:
            while True:
                if (
                    prefill_queue.empty()
                    and not self._batch_slots
                    and not self._batch_generator_has_unprocessed_prompts()
                ):
                    not_empty.clear()
                    # clear() 后重新检查，防止 put+set 与 clear+wait 交错时丢唤醒
                    if (
                        prefill_queue.empty()
                        and not self._batch_slots
                        and not self._batch_generator_has_unprocessed_prompts()
                    ):
                        await not_empty.wait()

                new_slots: List[Any] = []
                while True:
                    try:
                        new_slots.append(prefill_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                if new_slots:
                    prompts = []
                    caches = []
                    samplers = [
                        build_mlx_sampler(
                            temperature=slot.temperature,
                            top_p=slot.top_p,
                            min_p=slot.min_p,
                            top_k=slot.top_k,
                            presence_penalty=slot.presence_penalty,
                            repetition_penalty=slot.repetition_penalty,
                            token_ids=slot._token_ids,
                        )
                        for slot in new_slots
                    ]
                    max_tokens = [slot.max_tokens for slot in new_slots]
                    for slot in new_slots:
                        prompt_tokens, prompt_cache = self._prepare_prompt_fn(slot)
                        prompts.append(prompt_tokens)
                        caches.append(prompt_cache)
                    uids = self._batch_generator.insert(
                        prompts,
                        max_tokens=max_tokens,
                        samplers=samplers,
                        caches=caches,
                    )
                    for slot, uid, sampler in zip(new_slots, uids, samplers):
                        slot.batch_uid = uid
                        slot.sampler = sampler
                        self._batch_slots[uid] = slot

                if (
                    not self._batch_slots
                    and not self._batch_generator_has_unprocessed_prompts()
                ):
                    continue

                if self._batch_executor is not None:
                    responses = await asyncio.get_running_loop().run_in_executor(
                        self._batch_executor, self._batch_generator.next
                    )
                else:
                    responses = await asyncio.get_running_loop().run_in_executor(
                        None, self._batch_generator.next
                    )
                generation_responses = self._extract_generation_responses(responses)
                for response in self._iter_batch_responses(generation_responses):
                    uid = self._response_uid(response)
                    if uid is None:
                        continue
                    slot = self._batch_slots.get(uid)
                    if slot is None:
                        continue

                    # 客户端取消时 generate_stream finally 会设置 slot.done=True
                    if slot.done:
                        # 尝试通知底层 BatchGenerator 剔除废弃请求（兼容不同 mlx-lm 版本）
                        _remove_fn = (
                            getattr(self._batch_generator, "remove", None)
                            or getattr(self._batch_generator, "cancel", None)
                        )
                        if _remove_fn is not None:
                            try:
                                _remove_fn([uid])
                            except Exception:
                                pass
                        self._batch_slots.pop(uid, None)
                        continue

                    finish_reason = self._response_finish_reason(response)
                    token = self._response_token(response)
                    if finish_reason != "stop" and token is not None:
                        self._emit_token_fn(slot, token)

                    if finish_reason is not None:
                        slot.done = True
                        slot.token_queue.put_nowait(None)
                        self._batch_slots.pop(uid, None)
        except Exception as e:
            logger.error("mlx_batch_scheduler crashed: %s", e, exc_info=True)
            for slot in list(self._batch_slots.values()):
                if not slot.done:
                    slot.done = True
                    slot.token_queue.put_nowait(RuntimeError(f"Scheduler crashed: {e}"))
            self._batch_slots.clear()
            # 排空 prefill_queue，避免已入队但未被取走的请求永久悬挂
            while True:
                try:
                    slot = prefill_queue.get_nowait()
                    if not slot.done:
                        slot.done = True
                        slot.token_queue.put_nowait(RuntimeError(f"Scheduler crashed: {e}"))
                except asyncio.QueueEmpty:
                    break
        finally:
            if self._batch_generator is not None:
                self._batch_generator.close()
            if self._batch_executor is not None:
                self._batch_executor.shutdown(wait=False, cancel_futures=False)
                self._batch_executor = None

    # ── Response 解析 helpers ─────────────────────────────────────────────────

    def _extract_generation_responses(self, responses: Any) -> Any:
        """
        兼容 mlx-lm 0.31.x: next() 返回 (prompt_responses, generation_responses)。
        仅处理 generation_responses，避免在高并发下无效遍历 prompt 进度响应。
        """
        if isinstance(responses, tuple) and len(responses) == 2:
            return responses[1]
        return responses

    def _iter_batch_responses(self, responses: Any):
        """兼容 mlx-lm 不同版本 next() 返回结构（list / 嵌套 list / tuple）。"""
        if responses is None:
            return
        stack = [responses]
        while stack:
            item = stack.pop()
            if isinstance(item, (list, tuple)):
                stack.extend(reversed(item))
                continue
            yield item

    def _batch_generator_has_unprocessed_prompts(self) -> bool:
        """兼容不同 mlx-lm 版本的 pending prompts 字段。"""
        if self._batch_generator is None:
            return False

        bg = self._batch_generator
        for attr in (
            "unprocessed_prompts",
            "_unprocessed_prompts",
            "pending_prompts",
            "_pending_prompts",
        ):
            if not hasattr(bg, attr):
                continue
            value = getattr(bg, attr)
            try:
                return len(value) > 0
            except Exception:
                return bool(value)

        count = getattr(bg, "num_unprocessed_prompts", 0)
        try:
            return int(count) > 0
        except Exception:
            return bool(count)

    def _response_uid(self, response: Any) -> Optional[int]:
        if hasattr(response, "uid"):
            return getattr(response, "uid")
        if isinstance(response, dict):
            return response.get("uid")
        return None

    def _response_finish_reason(self, response: Any) -> Any:
        if hasattr(response, "finish_reason"):
            return getattr(response, "finish_reason")
        if isinstance(response, dict):
            return response.get("finish_reason")
        return None

    def _response_token(self, response: Any) -> Optional[int]:
        if hasattr(response, "token"):
            token = getattr(response, "token")
        elif isinstance(response, dict):
            token = response.get("token")
        else:
            return None
        if token is None:
            return None
        return int(token)
