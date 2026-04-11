"""
LocalProvider：使用本地 mlx-lm 模型进行推理（默认 Provider）。

──────────────────────────────────────────────────────────────────────────────
架构分层（代码在类内按此顺序排列）
──────────────────────────────────────────────────────────────────────────────

Layer 0 — 生命周期（load / _init_batch_engine / _maybe_run_warmup）
  模型加载、BatchGenerator 初始化、warmup。外部只调用 load()。

Layer 1 — Prompt 构建（_build_prompt_tokens / _render_prompt_text）
  tokenizer 编码，chat_template 渲染。

Layer 2 — System Prompt 缓存（_SystemPromptCacheEntry / _get_or_create_system_prompt_cache）
  对高频 system prompt 做 KV-cache 预填充并缓存，避免每次重算。
  LRU 上限 _SYSTEM_PROMPT_CACHE_SIZE（32 条）。

Layer 3 — 请求槽（_RequestSlot）
  每个推理请求的完整生命周期状态，token 通过 asyncio.Queue 流式交付给消费方。

Layer 4a — 旧版调度器（_legacy_scheduler，即原 _scheduler）
  单请求串行路径：prefill → decode 逐步推进，批量由 _run_one_iter 协调。
  用于 LocalProvider 被子类覆盖 _do_prefill 时的 fallback 路径。

Layer 4b — mlx-lm BatchGenerator 调度器（_mlx_batch_scheduler，即原 _batch_scheduler）
  默认路径：将多请求提交给 mlx-lm 官方 BatchGenerator，每轮 .next() 批量推进。
  支持专属 ThreadPoolExecutor（隔离 GPU 线程），通过 asyncio.Queue 与事件循环通信。

  两套调度器互斥，由 _use_builtin_batch_engine() 决定走哪条路径：
    type(self) is LocalProvider → True  → 走 _mlx_batch_scheduler
    子类覆盖了 _do_prefill      → False → 走 _legacy_scheduler

Layer 5 — 公共接口（generate_stream / generate）
  协程 API，提交请求到 prefill_queue，从 token_queue 流式消费。

──────────────────────────────────────────────────────────────────────────────
Continuous Batching 工作原理（_legacy_scheduler 路径）
──────────────────────────────────────────────────────────────────────────────
  旧方案（Dynamic Batching）：请求 B 等 A 全部生成完 → TTFT(B) = O(max_tokens_A)
  新方案：每次迭代 Phase1 prefill 新请求 + Phase2 推进已有请求，交错推进
  效果：TTFT(B) ≈ prefill(A) + 1 decode step，而非等 A 全部完成
"""
import asyncio
import inspect
import logging
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
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
_DEFAULT_MODEL_REPO_ID = "mlx-community/Qwen3.5-0.8B-4bit"
_DEFAULT_MODEL_DIRNAME = "qwen3.5-0.8b-4bit"

logger = logging.getLogger("lumina")


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
        self._supports_enable_thinking: Optional[bool] = None

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 0 — 生命周期
    # ══════════════════════════════════════════════════════════════════════════

    def _hf_hub_cache_dir(self) -> Path:
        hub_cache = os.environ.get("HUGGINGFACE_HUB_CACHE")
        if hub_cache:
            return Path(hub_cache).expanduser()
        hf_home = os.environ.get("HF_HOME")
        if hf_home:
            return Path(hf_home).expanduser() / "hub"
        return Path.home() / ".cache" / "huggingface" / "hub"

    def _find_cached_repo_snapshot(self, repo_id: str) -> Optional[str]:
        repo_cache_dir = self._hf_hub_cache_dir() / f"models--{repo_id.replace('/', '--')}" / "snapshots"
        if not repo_cache_dir.exists():
            return None

        candidates = [p for p in repo_cache_dir.iterdir() if p.is_dir()]
        if not candidates:
            return None

        # 优先返回最近访问/修改的快照目录。
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for snapshot_dir in candidates:
            if any(snapshot_dir.glob("*.safetensors")):
                return str(snapshot_dir)
        return None

    def _resolve_load_target(self) -> str:
        raw_target = (self.model_path or "").strip()
        if not raw_target:
            cached = self._find_cached_repo_snapshot(_DEFAULT_MODEL_REPO_ID)
            if cached:
                logger.info("Use cached model snapshot: %s", cached)
                return cached
            return _DEFAULT_MODEL_REPO_ID

        expanded = Path(raw_target).expanduser()
        # 目录已存在：按本地模型目录加载。
        if expanded.exists():
            return str(expanded)

        # 不存在的本地路径不能直接传给 mlx_lm.load（会被当作 repo id 并校验失败）。
        is_path_like = (
            expanded.is_absolute()
            or raw_target.startswith(("~", ".", ".."))
            or os.path.sep in raw_target
            or (os.path.altsep and os.path.altsep in raw_target)
        )
        if is_path_like and expanded.name.lower() == _DEFAULT_MODEL_DIRNAME:
            cached = self._find_cached_repo_snapshot(_DEFAULT_MODEL_REPO_ID)
            if cached:
                logger.info(
                    "Local model path missing, use cached snapshot: %s -> %s",
                    expanded,
                    cached,
                )
                return cached
            logger.info(
                "Local model path not found, fallback to repo id download: %s -> %s",
                expanded,
                _DEFAULT_MODEL_REPO_ID,
            )
            return _DEFAULT_MODEL_REPO_ID

        return raw_target

    def load(self):
        load_target = self._resolve_load_target()
        self._model, self._tokenizer = load(load_target)
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
            worker = self._mlx_batch_scheduler if self._use_builtin_batch_engine() else self._legacy_scheduler
            self._worker_task = asyncio.create_task(worker())

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 1 — Prompt 构建
    # ══════════════════════════════════════════════════════════════════════════

    def _build_prompt_tokens(self, system: str, user_text: str) -> mx.array:
        prompt_str = self._render_prompt_text(system, user_text)
        return mx.array(self._tokenizer.encode(prompt_str))

    def _render_prompt_text(self, system: str, user_text: str) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        kwargs = {
            "tokenize": False,
            "add_generation_prompt": True,
        }
        if self._supports_enable_thinking is None:
            try:
                sig = inspect.signature(self._tokenizer.apply_chat_template)
                self._supports_enable_thinking = "enable_thinking" in sig.parameters
            except (TypeError, ValueError):
                self._supports_enable_thinking = False
        if self._supports_enable_thinking:
            kwargs["enable_thinking"] = False

        return self._tokenizer.apply_chat_template(messages, **kwargs)

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

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 2 — System Prompt 缓存（KV-cache 预填充，LRU 32 条）
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def _eos_ids(self) -> set:
        eos = self._tokenizer.eos_token_id
        if isinstance(eos, list):
            return set(eos)
        return {eos} if eos is not None else set()

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 3 — 请求槽操作（token 投递、decode 推进）
    # ══════════════════════════════════════════════════════════════════════════

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
            logger.debug(
                "system_prompt_cache HIT  key_len=%d prefix_tokens=%d cache_size=%d",
                len(system_text), len(cached.prefix_tokens), len(self._system_prompt_cache),
            )
            return cached

        prefix_tokens = self._derive_system_prefix_tokens(system_text)
        if not prefix_tokens:
            logger.debug(
                "system_prompt_cache SKIP  key_len=%d (prefix derivation failed)",
                len(system_text),
            )
            return None

        logger.debug(
            "system_prompt_cache MISS  key_len=%d → building prefix cache (%d tokens)",
            len(system_text), len(prefix_tokens),
        )
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
            evicted = next(iter(self._system_prompt_cache))
            self._system_prompt_cache.popitem(last=False)
            logger.debug("system_prompt_cache EVICT  key_len=%d (LRU)", len(evicted))
        return entry

    def _prepare_batch_generator_prompt(self, slot: _RequestSlot) -> tuple[List[int], Optional[List[Any]]]:
        prompt_tokens = [int(tok) for tok in slot.prompt_tokens]
        try:
            cache_entry = self._get_or_create_system_prompt_cache(slot.system_text)
        except Exception as e:
            logger.debug("system_prompt_cache error during lookup: %s", e)
            return prompt_tokens, None
        if cache_entry is None:
            return prompt_tokens, None

        prefix_tokens = cache_entry.prefix_tokens
        if len(prompt_tokens) <= len(prefix_tokens):
            logger.debug(
                "system_prompt_cache prefix mismatch: prompt_len=%d <= prefix_len=%d, skipping cache",
                len(prompt_tokens), len(prefix_tokens),
            )
            return prompt_tokens, None
        if prompt_tokens[: len(prefix_tokens)] != prefix_tokens:
            logger.warning(
                "system_prompt_cache prefix TOKEN MISMATCH: prompt_len=%d prefix_len=%d "
                "— cache not applied (possible cache corruption)",
                len(prompt_tokens), len(prefix_tokens),
            )
            return prompt_tokens, None

        suffix_tokens = prompt_tokens[len(prefix_tokens):]
        if not suffix_tokens:
            return prompt_tokens, None
        logger.debug(
            "system_prompt_cache APPLIED: prefix=%d tokens skipped, suffix=%d tokens to prefill",
            len(prefix_tokens), len(suffix_tokens),
        )
        return suffix_tokens, self._clone_prompt_cache(cache_entry.prompt_cache)

    def _emit_token_id(self, slot: _RequestSlot, token_id: int) -> None:
        slot._token_ids.append(token_id)
        # 增量解码：decode 整个序列仍是最安全的方式（BPE tokenizer 对上下文敏感），
        # 但只在序列较短（≤512）或每 16 个 token 时全量解码一次，其余用单 token decode 估算。
        # 避免长回答时 O(n²) 的性能退化。
        n = len(slot._token_ids)
        if n <= 512 or n % 16 == 0:
            new_text = self._tokenizer.decode(slot._token_ids)
            delta = new_text[len(slot.decoded_text):]
            slot.decoded_text = new_text
        else:
            # 单 token 快速解码：精度略低（无法处理跨边界多字节），但不影响最终输出
            # 因为每 16 个 token 会做一次精确同步
            delta = self._tokenizer.decode([token_id])
            slot.decoded_text += delta
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

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 4a — 旧版调度器（_legacy_scheduler）：连续 batching，手动 KV cache 管理
    # ══════════════════════════════════════════════════════════════════════════

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

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 4b — mlx-lm BatchGenerator 调度器（_mlx_batch_scheduler，默认路径）
    # ══════════════════════════════════════════════════════════════════════════

    async def _mlx_batch_scheduler(self) -> None:
        """Layer 4b — mlx-lm BatchGenerator 调度器（默认路径）。

        将请求批量提交给 mlx-lm BatchGenerator，每轮 .next() 推进一步，
        通过 _batch_slots 映射 uid → slot，把 token/终止信号投入各自的 token_queue。
        """
        try:
            while True:
                if (
                    self._prefill_queue.empty()
                    and not self._batch_slots
                    and not self._batch_generator_has_unprocessed_prompts()
                ):
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

                if (
                    not self._batch_slots
                    and not self._batch_generator_has_unprocessed_prompts()
                ):
                    continue

                if self._batch_executor is not None:
                    responses = await asyncio.get_running_loop().run_in_executor(self._batch_executor, self._batch_generator.next)
                else:
                    responses = await asyncio.get_running_loop().run_in_executor(None, self._batch_generator.next)
                generation_responses = self._extract_generation_responses(responses)
                for response in self._iter_batch_responses(generation_responses):
                    uid = self._response_uid(response)
                    if uid is None:
                        continue
                    slot = self._batch_slots.get(uid)
                    if slot is None:
                        continue

                    finish_reason = self._response_finish_reason(response)
                    token = self._response_token(response)
                    if finish_reason != "stop" and token is not None:
                        self._emit_token_id_local(slot, token)

                    if finish_reason is not None:
                        slot.done = True
                        self._put_token_local(slot, None)
                        self._batch_slots.pop(uid, None)
        except Exception as e:
            logger.error("mlx_batch_scheduler crashed: %s", e, exc_info=True)
            for slot in list(self._batch_slots.values()):
                if not slot.done:
                    slot.done = True
                    self._put_token_local(slot, RuntimeError(f"Scheduler crashed: {e}"))
            self._batch_slots.clear()
        finally:
            if self._batch_generator is not None:
                self._batch_generator.close()
            if self._batch_executor is not None:
                self._batch_executor.shutdown(wait=False, cancel_futures=False)
                self._batch_executor = None

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

    def _response_finish_reason(self, response: Any):
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

    async def _legacy_scheduler(self) -> None:
        loop = asyncio.get_running_loop()
        try:
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
        except Exception as e:
            logger.error("legacy_scheduler crashed: %s", e, exc_info=True)
            with self._active_lock:
                slots = list(self._active)
            for slot in slots:
                if not slot.done:
                    slot.done = True
                    self._put_token_local(slot, RuntimeError(f"Scheduler crashed: {e}"))

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 5 — 公共接口
    # ══════════════════════════════════════════════════════════════════════════

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

        system_str = system if system is not None else "You are a helpful assistant."
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
