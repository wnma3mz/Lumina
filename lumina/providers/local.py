"""
LocalProvider：使用本地 mlx-lm 模型进行推理（默认 Provider）。

并发策略（Continuous Batching，仿 tLLM AsyncEngine）：

  旧方案（Dynamic Batching）：
    - 收集短窗口内的请求，串行跑完整个 stream_generate
    - 请求 B 必须等请求 A 全部生成完才开始 → TTFT(B) = O(max_tokens_A × latency)

  新方案（Continuous Batching）：
    - prefill_queue：新请求入队
    - _active：正在 decode 的 _RequestSlot，每个持有 KV cache + step_iter
    - 调度循环每次迭代：
        1. 从 prefill_queue 取新请求执行 prefill + 首 token，put 到 slot.token_queue
        2. 对 _active 中已存在的请求各推进 1 步，put 到 slot.token_queue
        3. 结束标志（None）put 到已完成请求的 queue
    - 消费方从 token_queue.get() 流式消费，天然线程安全、无竞争
    - 效果：TTFT(B) ≈ prefill(A) + 1 step，而非等 A 全部完成
"""
import asyncio
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, List, Optional
import uuid

try:
    import mlx.core as mx
    from mlx_lm import load
    from mlx_lm.generate import BatchGenerator, _left_pad_prompts, _make_cache, cache as mlx_cache
    from mlx_lm.sample_utils import make_sampler
    _MLX_AVAILABLE = True
except ImportError:
    _MLX_AVAILABLE = False

from .base import BaseProvider

# 每次迭代最多接入的新 prefill 请求数。
# 取 4 可以更快吸收一小波同时到达的短请求，降低后到请求的排队 TTFT。
_MAX_NEW_PREFILL_PER_ITER = 4
_SYSTEM_PROMPT_CACHE_SIZE = 32
_SYSTEM_PROMPT_SENTINEL = "<lumina_system_cache_user_probe_7a93d1e4>"
_WARMUP_SYSTEM_PROMPT = "You are a helpful assistant."
_WARMUP_USER_PROMPT = "Reply with one short word."
_WARMUP_DECODE_STEPS = 4


@dataclass
class _SystemPromptCacheEntry:
    system_text: str
    prefix_tokens: List[int]
    prompt_cache: List[Any]


@dataclass
class _RequestSlot:
    """一个请求的完整生命周期状态。"""
    request_id: str
    prompt_tokens: mx.array
    max_tokens: int
    temperature: float
    system_text: str = ""
    user_text: str = ""

    # 调度线程把 token 文本 put 进来，None = 结束，Exception = 错误
    # asyncio.Queue：跨线程安全（run_coroutine_threadsafe put）+ 协程 get
    token_queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    # 调度线程写入（在 prefill 完成后）
    prompt_cache: Optional[List[Any]] = None
    sampler: Optional[Any] = None
    prompt_tail_token: Optional[int] = None
    next_input_token: Optional[int] = None
    batch_uid: Optional[int] = None
    _token_ids: List[int] = field(default_factory=list)
    decoded_text: str = ""
    n_tokens: int = 0
    done: bool = False


class LocalProvider(BaseProvider):
    def __init__(
        self,
        model_path: str,
        max_new_prefill_per_iter: int = _MAX_NEW_PREFILL_PER_ITER,
        enable_warmup: bool = True,
        warmup_decode_steps: int = _WARMUP_DECODE_STEPS,
    ):
        if not _MLX_AVAILABLE:
            raise ImportError(
                "mlx / mlx-lm 未安装，LocalProvider 不可用。"
                "macOS 用户请运行：pip install lumina[macos]"
            )
        self.model_path = model_path
        self.max_new_prefill_per_iter = max(1, max_new_prefill_per_iter)
        self.enable_warmup = enable_warmup
        self.warmup_decode_steps = max(0, warmup_decode_steps)
        self._model = None
        self._tokenizer = None
        self._prefill_queue: Optional[asyncio.Queue] = None
        self._not_empty: Optional[asyncio.Event] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._active: List[_RequestSlot] = []
        self._active_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._continuous_batch_ids: Optional[tuple[str, ...]] = None
        self._continuous_batch_cache: Optional[List[Any]] = None
        self._batch_generator: Optional[BatchGenerator] = None
        self._batch_slots: dict[int, _RequestSlot] = {}
        self._batch_executor: Optional[ThreadPoolExecutor] = None
        self._system_prompt_cache: OrderedDict[str, _SystemPromptCacheEntry] = OrderedDict()

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def load(self):
        self._model, self._tokenizer = load(self.model_path)
        mx.eval(self._model.parameters())
        self._init_batch_engine()
        self._maybe_run_warmup()

    def _init_batch_engine(self) -> None:
        if self._use_builtin_batch_engine():
            eos_ids = getattr(self._tokenizer, "eos_token_ids", None) or list(self._eos_ids)
            self._batch_generator = BatchGenerator(
                self._model,
                stop_tokens=set(eos_ids),
                prefill_batch_size=self.max_new_prefill_per_iter,
                completion_batch_size=max(8, self.max_new_prefill_per_iter * 4),
            )
            if self._use_dedicated_batch_executor():
                self._batch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lumina-batch")

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    def _ensure_worker(self):
        current_loop = asyncio.get_running_loop()
        # asyncio.run() 每次创建新 loop；Queue/Event 绑定旧 loop 时必须重建
        if self._prefill_queue is None or self._loop is not current_loop:
            self._prefill_queue = asyncio.Queue()
            self._not_empty = asyncio.Event()
            self._batch_slots = {}
        if self._worker_task is None or self._worker_task.done():
            self._loop = current_loop
            # _batch_scheduler finally 会 close batch_generator / shutdown executor；
            # 新 worker 启动前必须重建，否则下次调用时使用已关闭的对象导致卡死。
            if self._use_builtin_batch_engine():
                self._init_batch_engine()
            worker = self._batch_scheduler if self._use_builtin_batch_engine() else self._scheduler
            self._worker_task = asyncio.create_task(worker())

    # ── Prompt 构建 ───────────────────────────────────────────────────────────

    def _build_prompt_tokens(self, system: str, user_text: str) -> mx.array:
        prompt_str = self._render_prompt_text(system, user_text)
        return mx.array(self._tokenizer.encode(prompt_str))

    def _render_prompt_text(self, system: str, user_text: str) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        return self._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )

    def _should_run_warmup(self) -> bool:
        return self.enable_warmup

    def _warmup_prompt(self) -> tuple[str, str]:
        return _WARMUP_SYSTEM_PROMPT, _WARMUP_USER_PROMPT

    def _maybe_run_warmup(self) -> None:
        if not self._should_run_warmup():
            return
        try:
            self._run_warmup()
        except Exception:
            mx.clear_cache()

    def _run_warmup(self) -> None:
        system_text, user_text = self._warmup_prompt()
        prompt_tokens = self._build_prompt_tokens(system_text, user_text)
        slot = _RequestSlot(
            request_id="warmup",
            prompt_tokens=prompt_tokens,
            max_tokens=max(1, self.warmup_decode_steps),
            temperature=0.0,
            system_text=system_text,
            user_text=user_text,
        )
        suffix_tokens, prompt_cache = self._prepare_batch_generator_prompt(slot)
        if not suffix_tokens:
            return
        if prompt_cache is None:
            prompt_cache = mlx_cache.make_prompt_cache(self._model)

        if len(suffix_tokens) > 1:
            self._prefill_full_prompt_cache(suffix_tokens[:-1], prompt_cache)

        input_tokens = mx.array([[suffix_tokens[-1]]])
        logits = self._model(input_tokens, cache=prompt_cache)[:, -1, :]
        mx.eval(logits)
        next_token = int(mx.argmax(logits, axis=-1).item())
        eos_ids = self._eos_ids

        for _ in range(max(0, self.warmup_decode_steps - 1)):
            if next_token in eos_ids:
                break
            logits = self._model(mx.array([[next_token]]), cache=prompt_cache)[:, -1, :]
            mx.eval(logits)
            next_token = int(mx.argmax(logits, axis=-1).item())

        mx.clear_cache()

    # ── EOS token ids ─────────────────────────────────────────────────────────

    @property
    def _eos_ids(self) -> set:
        eos = self._tokenizer.eos_token_id
        if isinstance(eos, list):
            return set(eos)
        return {eos} if eos is not None else set()

    # ── 同步推理（executor 线程）─────────────────────────────────────────────

    def _put_token(self, slot: _RequestSlot, value) -> None:
        """线程安全地往 asyncio Queue put 值（在 executor 线程中调用）。"""
        self._loop.call_soon_threadsafe(slot.token_queue.put_nowait, value)

    def _put_token_local(self, slot: _RequestSlot, value) -> None:
        """在 event loop 线程内直接投递 token。"""
        slot.token_queue.put_nowait(value)

    def _use_dedicated_batch_executor(self) -> bool:
        return True

    def _use_builtin_batch_engine(self) -> bool:
        return type(self) is LocalProvider

    def _sample_from_logits(self, logits: mx.array, slot: _RequestSlot) -> int:
        logprobs = logits - mx.logsumexp(logits, keepdims=True)
        return int(slot.sampler(logprobs).item())

    def _clone_prompt_cache(self, prompt_cache: List[Any]) -> List[Any]:
        return [
            type(cache_layer).from_state(
                cache_layer.state,
                cache_layer.meta_state,
            )
            for cache_layer in prompt_cache
        ]

    def _prefill_full_prompt_cache(self, prompt_tokens: List[int], prompt_cache: List[Any]) -> None:
        prompt = mx.array(prompt_tokens)
        while len(prompt) > 0:
            n_to_process = min(2048, len(prompt))
            self._model(prompt[:n_to_process][None], cache=prompt_cache)
            mx.eval([c.state for c in prompt_cache])
            prompt = prompt[n_to_process:]
            mx.clear_cache()
        for cache_layer in prompt_cache:
            if hasattr(cache_layer, "finalize"):
                cache_layer.finalize()

    def _derive_system_prefix_tokens(self, system_text: str) -> Optional[List[int]]:
        prompt_text = self._render_prompt_text(system_text, _SYSTEM_PROMPT_SENTINEL)
        sentinel_start = prompt_text.find(_SYSTEM_PROMPT_SENTINEL)
        if sentinel_start < 0 or prompt_text.find(_SYSTEM_PROMPT_SENTINEL, sentinel_start + 1) != -1:
            return None

        base_tokenizer = getattr(self._tokenizer, "_tokenizer", self._tokenizer)
        encoded = base_tokenizer(
            prompt_text,
            add_special_tokens=True,
            return_offsets_mapping=True,
        )
        input_ids = encoded["input_ids"]
        offsets = encoded["offset_mapping"]
        sentinel_end = sentinel_start + len(_SYSTEM_PROMPT_SENTINEL)

        sentinel_token_indices = []
        for idx, (start, end) in enumerate(offsets):
            if end <= sentinel_start or start >= sentinel_end:
                continue
            if start < sentinel_start or end > sentinel_end:
                return None
            sentinel_token_indices.append(idx)

        if not sentinel_token_indices:
            return None

        sentinel_tokens = base_tokenizer.encode(_SYSTEM_PROMPT_SENTINEL, add_special_tokens=False)
        span_tokens = input_ids[sentinel_token_indices[0] : sentinel_token_indices[-1] + 1]
        if span_tokens != sentinel_tokens:
            return None

        return input_ids[: sentinel_token_indices[0]]

    def _get_or_create_system_prompt_cache(self, system_text: str) -> Optional[_SystemPromptCacheEntry]:
        cached = self._system_prompt_cache.get(system_text)
        if cached is not None:
            self._system_prompt_cache.move_to_end(system_text)
            return cached

        prefix_tokens = self._derive_system_prefix_tokens(system_text)
        if not prefix_tokens:
            return None

        prompt_cache = mlx_cache.make_prompt_cache(self._model)
        self._prefill_full_prompt_cache(prefix_tokens, prompt_cache)
        entry = _SystemPromptCacheEntry(
            system_text=system_text,
            prefix_tokens=prefix_tokens,
            prompt_cache=self._clone_prompt_cache(prompt_cache),
        )
        self._system_prompt_cache[system_text] = entry
        self._system_prompt_cache.move_to_end(system_text)
        while len(self._system_prompt_cache) > _SYSTEM_PROMPT_CACHE_SIZE:
            self._system_prompt_cache.popitem(last=False)
        return entry

    def _prepare_batch_generator_prompt(self, slot: _RequestSlot) -> tuple[List[int], Optional[List[Any]]]:
        prompt_tokens = [int(tok) for tok in slot.prompt_tokens]
        try:
            cache_entry = self._get_or_create_system_prompt_cache(slot.system_text)
        except Exception:
            return prompt_tokens, None
        if cache_entry is None:
            return prompt_tokens, None

        prefix_tokens = cache_entry.prefix_tokens
        if len(prompt_tokens) <= len(prefix_tokens):
            return prompt_tokens, None
        if prompt_tokens[: len(prefix_tokens)] != prefix_tokens:
            return prompt_tokens, None

        suffix_tokens = prompt_tokens[len(prefix_tokens) :]
        if not suffix_tokens:
            return prompt_tokens, None
        return suffix_tokens, self._clone_prompt_cache(cache_entry.prompt_cache)

    def _emit_token_id(self, slot: _RequestSlot, token_id: int) -> None:
        slot._token_ids.append(token_id)
        new_text = self._tokenizer.decode(slot._token_ids)
        delta = new_text[len(slot.decoded_text):]
        slot.decoded_text = new_text
        slot.n_tokens += 1
        self._put_token(slot, delta)

        if slot.n_tokens >= slot.max_tokens:
            slot.done = True
            self._put_token(slot, None)

    def _reset_continuous_batch(self) -> None:
        self._continuous_batch_ids = None
        self._continuous_batch_cache = None

    def _materialize_continuous_batch(self, slot_by_id: dict[str, _RequestSlot]) -> None:
        if self._continuous_batch_cache is None or self._continuous_batch_ids is None:
            return
        for idx, request_id in enumerate(self._continuous_batch_ids):
            slot = slot_by_id.get(request_id)
            if slot is None or slot.done:
                continue
            slot.prompt_cache = [layer_cache.extract(idx) for layer_cache in self._continuous_batch_cache]
        self._reset_continuous_batch()

    def _prefill_prompt_cache(self, prompt_tokens: mx.array, prompt_cache: List[Any]) -> mx.array:
        prompt = prompt_tokens
        while len(prompt) > 1:
            remaining = len(prompt) - 1
            n_to_process = min(2048, remaining)
            self._model(prompt[:n_to_process][None], cache=prompt_cache)
            mx.eval([c.state for c in prompt_cache])
            prompt = prompt[n_to_process:]
            mx.clear_cache()
        return prompt

    def _finish_prefill_token(self, slot: _RequestSlot, token_id: int) -> None:
        eos_ids = self._eos_ids
        if token_id in eos_ids:
            slot.done = True
            self._put_token(slot, None)
            return

        slot.next_input_token = token_id
        self._emit_token_id(slot, token_id)

    def _do_prefill(self, slot: _RequestSlot) -> None:
        """
        执行 prefill + 生成首 token。
        结果通过 slot.token_queue 传递。
        """
        slot.sampler = make_sampler(temp=slot.temperature, top_p=0.9)
        prompt_cache = mlx_cache.make_prompt_cache(self._model)
        try:
            prompt = self._prefill_prompt_cache(slot.prompt_tokens, prompt_cache)
            logits = self._model(prompt[None], cache=prompt_cache)[:, -1, :]
            mx.eval(logits)
            token_id = self._sample_from_logits(logits, slot)
            slot.prompt_cache = prompt_cache
            slot.prompt_tail_token = int(prompt[-1].item())
            self._finish_prefill_token(slot, token_id)

        except StopIteration:
            slot.done = True
            self._put_token(slot, None)
        except Exception as e:
            slot.done = True
            self._put_token(slot, e)

    def _advance_one(self, slot: _RequestSlot) -> None:
        """推进一个 decode step，结果 put 到 token_queue。"""
        if slot.done or slot.prompt_cache is None or slot.next_input_token is None:
            return

        eos_ids = self._eos_ids
        try:
            input_tokens = mx.array([[slot.next_input_token]])
            logits = self._model(input_tokens, cache=slot.prompt_cache)[:, -1, :]
            mx.eval(logits)
            token_id = self._sample_from_logits(logits, slot)
        except Exception as e:
            slot.done = True
            self._put_token(slot, e)
            return

        if token_id in eos_ids:
            slot.done = True
            self._put_token(slot, None)
            return

        slot.next_input_token = token_id
        self._emit_token_id(slot, token_id)

    def _prefill_batch(self, slots: List[_RequestSlot]) -> List[_RequestSlot]:
        """prefill 一批新请求，必要时合并最后一步首 token 计算。"""
        if type(self)._do_prefill is not LocalProvider._do_prefill:
            newly_active = []
            for slot in slots:
                self._do_prefill(slot)
                if not slot.done:
                    newly_active.append(slot)
            return newly_active

        if not slots:
            return []

        if len(slots) == 1:
            slot = slots[0]
            self._do_prefill(slot)
            return [slot] if not slot.done else []

        for slot in slots:
            slot.sampler = make_sampler(temp=slot.temperature, top_p=0.9)

        try:
            prompt_lists = [[int(tok) for tok in slot.prompt_tokens] for slot in slots]
            max_length = max(len(prompt) for prompt in prompt_lists)
            padding = [max_length - len(prompt) for prompt in prompt_lists]
            inputs = _left_pad_prompts(prompt_lists, max_length=max_length)
            prompt_cache = _make_cache(self._model, padding, None)

            while inputs.shape[1] > 1:
                n_to_process = min(2048, inputs.shape[1] - 1)
                self._model(inputs[:, :n_to_process], cache=prompt_cache)
                mx.eval([c.state for c in prompt_cache])
                inputs = inputs[:, n_to_process:]
                mx.clear_cache()

            for cache_layer in prompt_cache:
                cache_layer.finalize()

            logits = self._model(inputs, cache=prompt_cache)[:, -1, :]
            mx.eval(logits)
        except Exception:
            newly_active = []
            for slot in slots:
                self._do_prefill(slot)
                if not slot.done:
                    newly_active.append(slot)
            return newly_active

        newly_active = []
        for idx, slot in enumerate(slots):
            slot.prompt_cache = [layer_cache.extract(idx) for layer_cache in prompt_cache]
            slot.prompt_tail_token = int(inputs[idx, -1].item())
            token_id = self._sample_from_logits(logits[idx : idx + 1], slot)
            self._finish_prefill_token(slot, token_id)
            if not slot.done:
                newly_active.append(slot)
        return newly_active

    def _advance_batch(self, slots: List[_RequestSlot]) -> None:
        """推进一批 decode 请求。

        当有多个活跃请求且都已持有 prompt_cache 时，合并各层 cache，
        一次前向计算出整批请求的下一个 token；否则回退到单请求路径。
        """
        fallback_slots = [
            slot for slot in slots
            if not slot.done and (slot.prompt_cache is None or slot.next_input_token is None)
        ]
        batch_slots = [
            slot for slot in slots
            if not slot.done and slot.prompt_cache is not None and slot.next_input_token is not None
        ]

        for slot in fallback_slots:
            self._advance_one(slot)

        if len(batch_slots) <= 1:
            if self._continuous_batch_cache is not None:
                self._materialize_continuous_batch({slot.request_id: slot for slot in batch_slots})
            for slot in batch_slots:
                self._advance_one(slot)
            return

        eos_ids = self._eos_ids
        batch_ids = tuple(slot.request_id for slot in batch_slots)
        if self._continuous_batch_cache is not None and self._continuous_batch_ids != batch_ids:
            self._materialize_continuous_batch({slot.request_id: slot for slot in batch_slots})

        reuse_continuous_batch = (
            self._continuous_batch_cache is not None
            and self._continuous_batch_ids == batch_ids
        )
        try:
            if reuse_continuous_batch:
                merged_cache = self._continuous_batch_cache
            else:
                merged_cache = [
                    type(layer_caches[0]).merge(layer_caches)
                    for layer_caches in zip(*(slot.prompt_cache for slot in batch_slots))
                ]
            input_tokens = mx.array([[slot.next_input_token] for slot in batch_slots])
            logits = self._model(input_tokens, cache=merged_cache)[:, -1, :]
            mx.eval(logits)
        except Exception:
            self._reset_continuous_batch()
            for slot in batch_slots:
                self._advance_one(slot)
            return

        self._continuous_batch_ids = batch_ids
        self._continuous_batch_cache = merged_cache
        for idx, slot in enumerate(batch_slots):
            token_id = self._sample_from_logits(logits[idx : idx + 1], slot)
            if token_id in eos_ids:
                slot.done = True
                self._put_token(slot, None)
                continue
            slot.next_input_token = token_id
            self._emit_token_id(slot, token_id)

    def _run_one_iter(self, prefill_list: List[_RequestSlot]) -> None:
        """
        单次调度迭代（executor 线程）：
          1. 先快照当前 _active（Phase 2 只处理这些）
          2. prefill 新请求，加入 _active
          3. decode 快照中的请求
          4. 清理完成请求
        """
        # Phase 1 前先快照（本轮新 prefill 的请求不参与 Phase 2）
        with self._active_lock:
            decode_batch = [s for s in self._active if not s.done]

        # Phase 1: prefill
        newly_active = self._prefill_batch(prefill_list)

        with self._active_lock:
            self._active.extend(newly_active)

        # Phase 2: decode（快照，不含本轮新 prefill）
        self._advance_batch(decode_batch)

        # 清理
        with self._active_lock:
            self._active = [s for s in self._active if not s.done]

    async def _batch_scheduler(self) -> None:
        """使用 mlx-lm BatchGenerator 作为默认 batch engine。"""
        try:
            while True:
                if self._prefill_queue.empty() and not self._batch_slots and not self._batch_generator.unprocessed_prompts:
                    self._not_empty.clear()
                    await self._not_empty.wait()

                new_slots: List[_RequestSlot] = []
                while True:
                    try:
                        new_slots.append(self._prefill_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                if new_slots:
                    prompts = []
                    caches = []
                    samplers = [make_sampler(temp=slot.temperature, top_p=0.9) for slot in new_slots]
                    max_tokens = [slot.max_tokens for slot in new_slots]
                    for slot in new_slots:
                        prompt_tokens, prompt_cache = self._prepare_batch_generator_prompt(slot)
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

                if not self._batch_slots and not self._batch_generator.unprocessed_prompts:
                    continue

                if self._batch_executor is not None:
                    responses = await asyncio.get_running_loop().run_in_executor(self._batch_executor, self._batch_generator.next)
                else:
                    responses = await asyncio.get_running_loop().run_in_executor(None, self._batch_generator.next)
                for response in responses:
                    slot = self._batch_slots.get(response.uid)
                    if slot is None:
                        continue

                    if response.finish_reason != "stop":
                        self._emit_token_id_local(slot, int(response.token))

                    if response.finish_reason is not None:
                        slot.done = True
                        if response.finish_reason == "stop":
                            self._put_token_local(slot, None)
                        self._batch_slots.pop(response.uid, None)
        finally:
            if self._batch_generator is not None:
                self._batch_generator.close()
            if self._batch_executor is not None:
                self._batch_executor.shutdown(wait=False, cancel_futures=False)
                self._batch_executor = None

    def _emit_token_id_local(self, slot: _RequestSlot, token_id: int) -> None:
        slot._token_ids.append(token_id)
        new_text = self._tokenizer.decode(slot._token_ids)
        delta = new_text[len(slot.decoded_text):]
        slot.decoded_text = new_text
        slot.n_tokens += 1
        self._put_token_local(slot, delta)

        if slot.n_tokens >= slot.max_tokens:
            slot.done = True
            self._put_token_local(slot, None)

    # ── 调度主循环 ────────────────────────────────────────────────────────────

    async def _scheduler(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            with self._active_lock:
                has_active = bool(self._active)

            if not has_active and self._prefill_queue.empty():
                self._not_empty.clear()
                await self._not_empty.wait()

            prefill_list: List[_RequestSlot] = []
            while len(prefill_list) < self.max_new_prefill_per_iter:
                try:
                    prefill_list.append(self._prefill_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            await loop.run_in_executor(None, self._run_one_iter, prefill_list)

    # ── 公共接口 ──────────────────────────────────────────────────────────────

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
        prompt_tokens = self._build_prompt_tokens(system_str, user_text)

        slot = _RequestSlot(
            request_id=uuid.uuid4().hex,
            prompt_tokens=prompt_tokens,
            max_tokens=max_tokens,
            temperature=temperature,
            system_text=system_str,
            user_text=user_text,
        )
        await self._prefill_queue.put(slot)
        self._not_empty.set()

        # 从 token_queue 流式消费：None = 结束，Exception = 错误
        while True:
            item = await slot.token_queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
