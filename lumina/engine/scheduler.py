"""
lumina/engine/scheduler.py — 共享调度层

GenerationRequest：纯 Python 请求控制结构（无 mlx 依赖），
供所有 Provider 的 slot 子类继承。

EngineScheduler：封装 legacy scheduler 路径的 idle-wait / drain / iterate
连续批处理循环，具体 forward 由 LocalProvider._run_one_iter 回调提供。

与 MlxBatchScheduler（providers/mlx/scheduler.py）并列，互不继承：
- EngineScheduler    — legacy 路径（_do_prefill 被子类覆盖时使用）
- MlxBatchScheduler — mlx BatchGenerator 路径（默认路径）

未来新增 Provider（如 LlamaCppProvider）可复用 EngineScheduler，
只需提供 iteration_fn / get_active_fn / put_error_fn 三个回调。
"""
import asyncio
import logging
from dataclasses import dataclass, field

from lumina.engine.sampling import (
    DEFAULT_MIN_P,
    DEFAULT_PRESENCE_PENALTY,
    DEFAULT_REPETITION_PENALTY,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
)

logger = logging.getLogger("lumina")


@dataclass
class GenerationRequest:
    """Provider 无关的请求控制结构。

    子类（如 providers/local.py 中的 _RequestSlot）可添加
    Provider 专有字段（prompt_tokens、sampler 等），
    但控制流所需的最小状态均在此处定义，以便 EngineScheduler
    在不依赖任何 mlx 符号的情况下操作 slot。
    """

    request_id: str
    max_tokens: int
    temperature: float
    top_p: float = DEFAULT_TOP_P
    top_k: int = DEFAULT_TOP_K
    min_p: float = DEFAULT_MIN_P
    presence_penalty: float = DEFAULT_PRESENCE_PENALTY
    repetition_penalty: float = DEFAULT_REPETITION_PENALTY
    # 调度线程把 token 文本 put 进来，None = 结束，Exception = 错误
    token_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    done: bool = False
    n_tokens: int = 0


class EngineScheduler:
    """通用连续批处理循环（legacy scheduler 路径）。

    使用方式：在 Provider 的调度协程中实例化，传入三个回调，
    然后 ``await scheduler.run(prefill_queue, not_empty, executor)``。

    示例（LocalProvider._legacy_scheduler）::

        scheduler = EngineScheduler(
            iteration_fn=self._run_one_iter,
            get_active_fn=lambda: list(self._active),   # 需持锁
            put_error_fn=self._put_token_local,
        )
        await scheduler.run(self._prefill_queue, self._not_empty, self._legacy_executor)
    """

    def __init__(
        self,
        iteration_fn,               # Callable[[list[GenerationRequest]], None]
        get_active_fn,              # Callable[[], list[GenerationRequest]]
        put_error_fn,               # Callable[[GenerationRequest, Exception], None]
        max_new_prefill_per_iter: int = 4,
    ) -> None:
        self._iteration_fn = iteration_fn
        self._get_active_fn = get_active_fn
        self._put_error_fn = put_error_fn
        self.max_new_prefill_per_iter = max_new_prefill_per_iter

    async def run(
        self,
        prefill_queue: asyncio.Queue,
        not_empty: asyncio.Event,
        executor,
    ) -> None:
        """主调度循环。从 asyncio.Task 中调用。

        循环逻辑：
        1. 无活跃 slot 且队列为空 → 清除事件、再次检查后 await
        2. 从队列中取最多 max_new_prefill_per_iter 个新请求
        3. 在 executor 线程中执行 iteration_fn（同步阻塞的 forward pass）
        4. 崩溃时排空队列并通知所有等待方
        """
        loop = asyncio.get_running_loop()
        try:
            while True:
                has_active = bool(self._get_active_fn())
                if not has_active and prefill_queue.empty():
                    not_empty.clear()
                    # clear() 后重新检查，防止 put+set 与 clear+wait 交错时丢唤醒
                    has_active = bool(self._get_active_fn())
                    if not has_active and prefill_queue.empty():
                        await not_empty.wait()

                prefill_list = []
                while len(prefill_list) < self.max_new_prefill_per_iter:
                    try:
                        prefill_list.append(prefill_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                await loop.run_in_executor(
                    executor, self._iteration_fn, prefill_list
                )
        except asyncio.CancelledError:
            self._drain(prefill_queue, RuntimeError("Scheduler cancelled"))
            raise
        except Exception as e:
            logger.error("EngineScheduler crashed: %s", e, exc_info=True)
            self._drain(prefill_queue, e)

    def _drain(self, prefill_queue: asyncio.Queue, exc: Exception) -> None:
        """crash 后排空队列并向所有等待方注入错误，避免请求永久挂起。"""
        err = RuntimeError(f"Scheduler crashed: {exc}")
        for slot in self._get_active_fn():
            if not slot.done:
                slot.done = True
                self._put_error_fn(slot, err)
        while True:
            try:
                slot = prefill_queue.get_nowait()
                if not slot.done:
                    slot.done = True
                    self._put_error_fn(slot, err)
            except asyncio.QueueEmpty:
                break
