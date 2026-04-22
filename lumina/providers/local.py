"""
LocalProvider：使用本地 mlx-lm 模型进行推理（默认 Provider）。

──────────────────────────────────────────────────────────────────────────────
架构分层（代码在类内按此顺序排列）
──────────────────────────────────────────────────────────────────────────────

Layer 0 — 生命周期（load / _maybe_run_warmup）
  委托给 MlxModelLoader（providers/mlx_loader.py）：路径解析、模型加载、
  BatchGenerator 初始化。warmup 留在此处（依赖 Layer 1/2 能力）。

Layer 1 — Prompt 构建（_build_prompt_tokens / _render_prompt_text）
  委托给 MlxPromptBuilder（providers/mlx_prompt.py）：tokenizer 编码，
  chat_template 渲染。保留单行委托方法，保证测试 patch 和子类兼容。

Layer 2 — System Prompt 缓存（_get_or_create_system_prompt_cache）
  委托给 SystemPromptCache（providers/system_prompt_cache.py）。
  对高频 system prompt 做 KV-cache 预填充并缓存（LRU 32 条）。

Layer 3 — 请求槽（_RequestSlot）
  每个推理请求的完整生命周期状态，token 通过 asyncio.Queue 流式交付给消费方。

Layer 4a — 旧版调度器（_legacy_scheduler，即原 _scheduler）
  单请求串行路径：prefill → decode 逐步推进，批量由 _run_one_iter 协调。
  用于 LocalProvider 被子类覆盖 _do_prefill 时的 fallback 路径。

Layer 4b — mlx-lm BatchGenerator 调度器（_mlx_batch_scheduler，即原 _batch_scheduler）
  委托给 MlxBatchScheduler（providers/scheduler.py）。
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
from __future__ import annotations

import asyncio
import base64
import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, List, Optional
import uuid

try:
    import mlx.core as mx
    from mlx_lm.generate import _left_pad_prompts, _make_cache, cache as mlx_cache
    _MLX_AVAILABLE = True
except ImportError:
    mx = None  # type: ignore[assignment]
    _MLX_AVAILABLE = False

from lumina.engine.scheduler import GenerationRequest
from .base import BaseProvider, ProviderCapabilities
from .mlx_loader import MlxModelLoader
from .mlx_prompt import MlxPromptBuilder
from .system_prompt_cache import SystemPromptCache, SystemPromptCacheEntry
from lumina.engine.sampling import (
    DEFAULT_MIN_P,
    DEFAULT_PRESENCE_PENALTY,
    DEFAULT_REPETITION_PENALTY,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_K,
    DEFAULT_TOP_P,
    build_mlx_sampler,
)

try:
    from mlx_vlm import generate as vlm_generate
    from mlx_vlm import load as vlm_load
    from mlx_vlm.generate import stream_generate as vlm_stream_generate
    from mlx_vlm.prompt_utils import apply_chat_template as vlm_apply_chat_template
    from mlx_vlm.utils import load_config as vlm_load_config
    _MLX_VLM_AVAILABLE = True
except ImportError:
    vlm_generate = None  # type: ignore[assignment]
    vlm_load = None  # type: ignore[assignment]
    vlm_stream_generate = None  # type: ignore[assignment]
    vlm_apply_chat_template = None  # type: ignore[assignment]
    vlm_load_config = None  # type: ignore[assignment]
    _MLX_VLM_AVAILABLE = False

# 每次迭代最多接入的新 prefill 请求数。
# 取 4 可以更快吸收一小波同时到达的短请求，降低后到请求的排队 TTFT。
_MAX_NEW_PREFILL_PER_ITER = 4
_SYSTEM_PROMPT_CACHE_SIZE = 32
_WARMUP_SYSTEM_PROMPT = "You are a helpful assistant."
_WARMUP_USER_PROMPT = "Reply with one short word."
_WARMUP_DECODE_STEPS = 4

logger = logging.getLogger("lumina")



@dataclass
class _RequestSlot(GenerationRequest):
    """一个请求的完整生命周期状态（mlx 专有扩展）。

    继承 GenerationRequest（engine/scheduler.py）中的通用控制字段：
      request_id, max_tokens, temperature, top_p, token_queue, done, n_tokens

    此处只定义 mlx 专有字段，避免在 engine 层引入 mlx 依赖。
    """
    # dataclass 继承约束：父类有默认值的字段后不能出现无默认值字段，
    # 故 prompt_tokens 改为带默认值（调用方仍通过 keyword arg 传入）。
    prompt_tokens: Any = field(default_factory=lambda: mx.array([]) if mx is not None else [])
    system_text: str = ""
    user_text: str = ""

    # 调度线程写入（在 prefill 完成后）
    prompt_cache: Optional[List[Any]] = None
    sampler: Optional[Any] = None
    prompt_tail_token: Optional[int] = None
    next_input_token: Optional[int] = None
    batch_uid: Optional[int] = None
    _token_ids: List[int] = field(default_factory=list)
    decoded_text: str = ""


class LocalProvider(BaseProvider):
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_image_input=_MLX_VLM_AVAILABLE)

    def __init__(
        self,
        model_path: str,
        max_new_prefill_per_iter: int = _MAX_NEW_PREFILL_PER_ITER,
        enable_warmup: bool = True,
        warmup_decode_steps: int = _WARMUP_DECODE_STEPS,
        offload_embedding: bool = True,
        offload_vision: bool = True,
        offload_audio: bool = True,
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
        self.offload_embedding = offload_embedding
        self.offload_vision = offload_vision
        self.offload_audio = offload_audio
        self._model = None
        self._tokenizer = None
        self._prefill_queue: Optional[asyncio.Queue] = None
        self._not_empty: Optional[asyncio.Event] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._active: List[_RequestSlot] = []
        self._active_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._continuous_batch_ids: Optional[tuple] = None
        self._continuous_batch_cache: Optional[List[Any]] = None
        self._batch_generator = None
        self._batch_executor: Optional[ThreadPoolExecutor] = None
        self._legacy_executor: Optional[ThreadPoolExecutor] = None
        self._spc: Optional[SystemPromptCache] = None
        self._prompt_builder: Optional[MlxPromptBuilder] = None
        self._vlm_model = None
        self._vlm_processor = None
        self._vlm_config = None
        self._vlm_lock = threading.Lock()
        self._worker_lock = threading.Lock()  # BUG-01: 保护 _ensure_worker 的 check-and-set
        self._loader = MlxModelLoader(
            model_path=model_path,
            max_new_prefill_per_iter=self.max_new_prefill_per_iter,
            use_builtin_batch_engine_fn=self._use_builtin_batch_engine,
            use_dedicated_batch_executor_fn=self._use_dedicated_batch_executor,
            eos_ids_fn=lambda: self._eos_ids,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 0 — 生命周期（委托给 MlxModelLoader，warmup 留在此处）
    # ══════════════════════════════════════════════════════════════════════════

    def load(self):
        model, tokenizer, batch_generator, batch_executor = self._loader.load(
            offload_embedding=self.offload_embedding,
            offload_vision=self.offload_vision,
            offload_audio=self.offload_audio
        )
        self._model = model
        self._tokenizer = tokenizer
        self._batch_generator = batch_generator
        self._batch_executor = batch_executor
        self._prompt_builder = MlxPromptBuilder(self._tokenizer)
        self._spc = SystemPromptCache(
            self._model, self._tokenizer, loaded_as_vlm=self._loader.loaded_as_vlm
        )
        self._maybe_run_warmup()

    def _init_batch_engine(self) -> None:
        """重建 BatchGenerator / Executor（worker 重启时调用）。"""
        bg, be = self._loader._init_batch_engine(self._model, self._tokenizer)
        self._batch_generator = bg
        self._batch_executor = be

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    def _ensure_worker(self):
        # BUG-01: 用 _worker_lock 保护整个 check-and-set，防止多 event loop 场景下
        # 创建两个并行 worker 或旧 Queue 里的 slot 永久挂起
        with self._worker_lock:
            current_loop = asyncio.get_running_loop()
            # asyncio.run() 每次创建新 loop；Queue/Event 绑定旧 loop 时必须重建
            if self._prefill_queue is None or self._loop is not current_loop:
                self._prefill_queue = asyncio.Queue()
                self._not_empty = asyncio.Event()
            if self._worker_task is None or self._worker_task.done():
                self._loop = current_loop
                # _batch_scheduler finally 会 close batch_generator / shutdown executor；
                # 新 worker 启动前必须重建，否则下次调用时使用已关闭的对象导致卡死。
                # BUG-02: 缓存调用结果，避免 _use_builtin_batch_engine() 被调用两次
                use_batch = self._use_builtin_batch_engine()
                if use_batch:
                    self._init_batch_engine()
                if use_batch:
                    worker = self._mlx_batch_scheduler
                else:
                    if self._legacy_executor is None:
                        self._legacy_executor = ThreadPoolExecutor(
                            max_workers=1, thread_name_prefix="lumina_legacy"
                        )
                    worker = self._legacy_scheduler
                self._worker_task = asyncio.create_task(worker())

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 1 — Prompt 构建（委托给 MlxPromptBuilder）
    # 单行委托方法保留：测试用 monkeypatch.setattr(provider, "_xxx") 和子类覆盖均依赖方法名。
    # ══════════════════════════════════════════════════════════════════════════

    def _build_prompt_tokens(self, system: str, user_text: str):
        return self._prompt_builder.encode(system, user_text)

    def _render_prompt_text(self, system: str, user_text: str) -> str:
        return self._prompt_builder.render(system, user_text)

    def _should_run_warmup(self) -> bool:
        return self.enable_warmup

    def _warmup_prompt(self) -> tuple:
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
            prompt_cache = self._make_prompt_cache()

        if len(suffix_tokens) > 1:
            self._prefill_full_prompt_cache(suffix_tokens[:-1], prompt_cache)

        input_tokens = mx.array([[suffix_tokens[-1]]])
        logits = self._extract_logits(self._model(input_tokens, cache=prompt_cache))[:, -1, :]
        mx.eval(logits)
        next_token = int(mx.argmax(logits, axis=-1).item())
        eos_ids = self._eos_ids

        for _ in range(max(0, self.warmup_decode_steps - 1)):
            if next_token in eos_ids:
                break
            logits = self._extract_logits(self._model(mx.array([[next_token]]), cache=prompt_cache))[:, -1, :]
            mx.eval(logits)
            next_token = int(mx.argmax(logits, axis=-1).item())

        mx.clear_cache()

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 2 — System Prompt 缓存（委托给 SystemPromptCache）
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def _eos_ids(self) -> set:
        tok = getattr(self._tokenizer, "tokenizer", self._tokenizer)
        eos = getattr(tok, "eos_token_id", None)
        if isinstance(eos, list):
            return set(eos)
        return {eos} if eos is not None else set()

    def _derive_system_prefix_tokens(self, system_text: str) -> Optional[List[int]]:
        """将 system_text 渲染为 prefix token ids（供 SystemPromptCache.render_fn 使用）。"""
        return self._prompt_builder.derive_prefix_tokens(system_text)

    def _get_or_create_system_prompt_cache(self, system_text: str) -> Optional[SystemPromptCacheEntry]:
        """委托给 SystemPromptCache，注入 render_fn（避免反向依赖）。"""
        if self._spc is None:
            return None
        try:
            return self._spc.get_or_create(
                system_text,
                render_fn=self._derive_system_prefix_tokens,
            )
        except Exception as e:
            logger.debug("system_prompt_cache error during lookup: %s", e)
            return None

    def _clone_prompt_cache(self, prompt_cache: List[Any]) -> List[Any]:
        """委托给 SystemPromptCache.clone_cache_raw。"""
        return SystemPromptCache.clone_cache_raw(prompt_cache)

    def _prepare_batch_generator_prompt(self, slot: _RequestSlot) -> tuple:
        prompt_tokens = [int(tok) for tok in slot.prompt_tokens]
        cache_entry = self._get_or_create_system_prompt_cache(slot.system_text)
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
        return type(self) is LocalProvider and not self._loader.loaded_as_vlm

    def _sample_from_logits(self, logits: mx.array, slot: _RequestSlot) -> int:
        logprobs = logits - mx.logsumexp(logits, keepdims=True)
        return int(slot.sampler(logprobs).item())

    def _prefill_full_prompt_cache(self, prompt_tokens: List[int], prompt_cache: List[Any]) -> None:
        prompt = mx.array(prompt_tokens)
        while len(prompt) > 0:
            n_to_process = min(2048, len(prompt))
            self._model(prompt[:n_to_process][None], cache=prompt_cache)
            self._eval_cache_state(prompt_cache)
            prompt = prompt[n_to_process:]
            mx.clear_cache()
        for cache_layer in prompt_cache:
            if hasattr(cache_layer, "finalize"):
                cache_layer.finalize()

    def _emit_token_id(self, slot: _RequestSlot, token_id: int) -> None:
        slot._token_ids.append(token_id)
        # 始终全量解码：BPE tokenizer 对上下文敏感，单独 decode 单个 token 在多字节
        # 字符（中文、emoji 等）跨 token 边界时会产生乱码（U+FFFD）。
        # GPU 推理是实际瓶颈，CPU decode 的 O(n) 开销可忽略不计。
        new_text = self._tokenizer.decode(slot._token_ids)
        delta = new_text[len(slot.decoded_text):]
        slot.decoded_text = new_text
        slot.n_tokens += 1
        self._put_token(slot, delta)

        if slot.n_tokens >= slot.max_tokens:
            slot.done = True
            self._put_token(slot, None)

    def _emit_token_id_local(self, slot: _RequestSlot, token_id: int) -> None:
        slot._token_ids.append(token_id)
        # 始终全量解码，理由同 _emit_token_id。
        new_text = self._tokenizer.decode(slot._token_ids)
        delta = new_text[len(slot.decoded_text):]
        slot.decoded_text = new_text
        slot.n_tokens += 1
        self._put_token_local(slot, delta)

        if slot.n_tokens >= slot.max_tokens:
            slot.done = True
            self._put_token_local(slot, None)

    def _reset_continuous_batch(self) -> None:
        self._continuous_batch_ids = None
        self._continuous_batch_cache = None

    def _materialize_continuous_batch(self, slot_by_id: dict) -> None:
        if self._continuous_batch_cache is None or self._continuous_batch_ids is None:
            return
        for idx, request_id in enumerate(self._continuous_batch_ids):
            slot = slot_by_id.get(request_id)
            if slot is None or slot.done:
                continue
            slot.prompt_cache = [layer_cache.extract(idx) for layer_cache in self._continuous_batch_cache]
        self._reset_continuous_batch()

    @staticmethod
    def _extract_logits(output) -> "mx.array":
        """从模型输出中提取 logits array。

        mlx_lm 模型直接返回 mx.array；mlx_vlm 的 LanguageModel 返回
        LanguageModelOutput(logits=...)，需要手动解包。
        """
        return getattr(output, "logits", output)

    @staticmethod
    def _eval_cache_state(prompt_cache: List[Any]) -> None:
        """安全地 eval cache state，兼容 KVCache（mx.array pair）和 ArraysCache（list，含 None）。"""
        flat = []
        for c in prompt_cache:
            s = c.state
            if isinstance(s, list):
                flat.extend(x for x in s if x is not None)
            elif s is not None:
                flat.append(s)
        if flat:
            mx.eval(flat)

    def _make_prompt_cache(self):
        """为当前模型创建正确的 prompt cache。

        VLM 模型（loaded_as_vlm=True）的顶层 Model 没有 make_cache()，
        需要用 language_model 的 make_cache() 才能得到正确的
        混合 KVCache/ArraysCache 结构（Qwen3.5 Mamba-Hybrid 需要）。
        """
        if self._loader.loaded_as_vlm:
            inner = getattr(self._model, "language_model", None) or self._model
            return mlx_cache.make_prompt_cache(inner)
        return mlx_cache.make_prompt_cache(self._model)

    def _get_embeddings(self, input_ids: mx.array) -> Optional[mx.array]:
        """优化：在 CPU 上执行 Embedding 查找，允许权重留在磁盘。"""
        if not self.offload_embedding:
            return None
            
        # 针对 Qwen/Llama 模型寻找 embed_tokens 层
        model_internal = getattr(self._model, "model", self._model)
        embed_layer = getattr(model_internal, "embed_tokens", None)
        
        if embed_layer is not None:
            with mx.stream(mx.cpu):
                return embed_layer(input_ids)
        return None

    def _prefill_prompt_cache(self, prompt_tokens: mx.array, prompt_cache: List[Any]) -> mx.array:
        prompt = prompt_tokens
        while len(prompt) > 1:
            remaining = len(prompt) - 1
            n_to_process = min(2048, remaining)
            inputs = prompt[:n_to_process][None]
            
            embeddings = self._get_embeddings(inputs)
            if embeddings is not None:
                self._model(embeddings, cache=prompt_cache)
            else:
                self._model(inputs, cache=prompt_cache)
                
            self._eval_cache_state(prompt_cache)
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
        slot.sampler = build_mlx_sampler(
            temperature=slot.temperature,
            top_p=slot.top_p,
            min_p=slot.min_p,
            top_k=slot.top_k,
            presence_penalty=slot.presence_penalty,
            repetition_penalty=slot.repetition_penalty,
            token_ids=slot._token_ids,
        )
        prompt_cache = self._make_prompt_cache()
        try:
            prompt = self._prefill_prompt_cache(slot.prompt_tokens, prompt_cache)
            inputs = prompt[None]
            
            embeddings = self._get_embeddings(inputs)
            if embeddings is not None:
                logits = self._extract_logits(self._model(embeddings, cache=prompt_cache))[:, -1, :]
            else:
                logits = self._extract_logits(self._model(inputs, cache=prompt_cache))[:, -1, :]
                
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
            inputs = mx.array([[slot.next_input_token]])
            embeddings = self._get_embeddings(inputs)
            if embeddings is not None:
                logits = self._extract_logits(self._model(embeddings, cache=slot.prompt_cache))[:, -1, :]
            else:
                logits = self._extract_logits(self._model(inputs, cache=slot.prompt_cache))[:, -1, :]
                
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
            slot.sampler = build_mlx_sampler(
                temperature=slot.temperature,
                top_p=slot.top_p,
                min_p=slot.min_p,
                top_k=slot.top_k,
                presence_penalty=slot.presence_penalty,
                repetition_penalty=slot.repetition_penalty,
                token_ids=slot._token_ids,
            )

        try:
            prompt_lists = [[int(tok) for tok in slot.prompt_tokens] for slot in slots]
            max_length = max(len(prompt) for prompt in prompt_lists)
            padding = [max_length - len(prompt) for prompt in prompt_lists]
            inputs = _left_pad_prompts(prompt_lists, max_length=max_length)
            prompt_cache = _make_cache(self._model, padding, None)

            while inputs.shape[1] > 1:
                n_to_process = min(2048, inputs.shape[1] - 1)
                batch_inputs = inputs[:, :n_to_process]
                
                embeddings = self._get_embeddings(batch_inputs)
                if embeddings is not None:
                    self._model(embeddings, cache=prompt_cache)
                else:
                    self._model(batch_inputs, cache=prompt_cache)
                    
                self._eval_cache_state(prompt_cache)
                inputs = inputs[:, n_to_process:]
                mx.clear_cache()

            for cache_layer in prompt_cache:
                cache_layer.finalize()

            embeddings = self._get_embeddings(inputs)
            if embeddings is not None:
                logits = self._extract_logits(self._model(embeddings, cache=prompt_cache))[:, -1, :]
            else:
                logits = self._extract_logits(self._model(inputs, cache=prompt_cache))[:, -1, :]
                
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
            
            embeddings = self._get_embeddings(input_tokens)
            if embeddings is not None:
                logits = self._extract_logits(self._model(embeddings, cache=merged_cache))[:, -1, :]
            else:
                logits = self._extract_logits(self._model(input_tokens, cache=merged_cache))[:, -1, :]
                
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
    # Layer 4b — mlx-lm BatchGenerator 调度器（委托给 MlxBatchScheduler）
    # ══════════════════════════════════════════════════════════════════════════

    async def _mlx_batch_scheduler(self) -> None:
        """Layer 4b — 委托给 MlxBatchScheduler（providers/scheduler.py）。"""
        from .scheduler import MlxBatchScheduler
        scheduler = MlxBatchScheduler(
            model=self._model,
            tokenizer=self._tokenizer,
            batch_generator=self._batch_generator,
            batch_executor=self._batch_executor,
            loop=self._loop,
            prepare_prompt_fn=self._prepare_batch_generator_prompt,
            emit_token_fn=self._emit_token_id_local,
        )
        await scheduler.run(self._prefill_queue, self._not_empty)

    async def _legacy_scheduler(self) -> None:
        """Layer 4a — 委托给 EngineScheduler（engine/scheduler.py）。"""
        from lumina.engine.scheduler import EngineScheduler

        def _get_active():
            with self._active_lock:
                return list(self._active)

        scheduler = EngineScheduler(
            iteration_fn=self._run_one_iter,
            get_active_fn=_get_active,
            put_error_fn=self._put_token_local,
            max_new_prefill_per_iter=self.max_new_prefill_per_iter,
        )
        try:
            await scheduler.run(self._prefill_queue, self._not_empty, self._legacy_executor)
        finally:
            with self._active_lock:
                self._active.clear()

    # ══════════════════════════════════════════════════════════════════════════
    # Layer 5 — 公共接口
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _messages_include_images(messages: list[dict[str, Any]]) -> bool:
        for message in messages:
            content = message.get("content", "")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
        return False

    @staticmethod
    def _decode_data_url_image(image_ref: str):
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Pillow 未安装，无法解析图片输入") from exc

        try:
            _, payload = image_ref.split(",", 1)
        except ValueError as exc:
            raise ValueError("无效的 data URL 图片输入") from exc
        try:
            image_bytes = base64.b64decode(payload)
        except Exception as exc:
            raise ValueError("无法解码 data URL 图片内容") from exc
        # BUG-07: Image.open() 未关闭，高并发下积累内存压力
        with Image.open(io.BytesIO(image_bytes)) as img:
            return img.convert("RGB")

    @classmethod
    def _normalize_vlm_image_input(cls, image_ref: str):
        image_ref = (image_ref or "").strip()
        if not image_ref:
            raise ValueError("图片输入为空")
        if image_ref.startswith("data:"):
            return cls._decode_data_url_image(image_ref)
        if image_ref.startswith("file://"):
            return image_ref[len("file://"):]
        return image_ref

    def _build_vlm_messages_and_images(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str],
    ) -> tuple[list[dict[str, str]], list[Any]]:
        vlm_messages: list[dict[str, str]] = []
        image_inputs: list[Any] = []
        if system:
            vlm_messages.append({"role": "system", "content": system})
        for message in messages:
            role = str(message.get("role", "user"))
            content = message.get("content", "")
            if isinstance(content, str):
                vlm_messages.append({"role": role, "content": content})
                continue
            if not isinstance(content, list):
                raise TypeError("消息 content 格式不支持")
            text_parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "text":
                    text = str(part.get("text", "")).strip()
                    if text:
                        text_parts.append(text)
                    continue
                if part_type == "image_url":
                    image_url = part.get("image_url") or {}
                    image_ref = str(image_url.get("url", "")).strip()
                    image_inputs.append(self._normalize_vlm_image_input(image_ref))
                    continue
                raise ValueError(f"不支持的消息内容类型：{part_type}")
            vlm_messages.append({"role": role, "content": "\n".join(text_parts).strip()})
        if not image_inputs:
            raise ValueError("未找到图片输入")
        return vlm_messages, image_inputs

    def _ensure_vlm_loaded(self) -> None:
        if self._vlm_model is not None and self._vlm_processor is not None and self._vlm_config is not None:
            return
        if not _MLX_VLM_AVAILABLE:
            raise ImportError("mlx-vlm 未安装，无法使用本地视觉模型")
        with self._vlm_lock:
            if self._vlm_model is not None and self._vlm_processor is not None and self._vlm_config is not None:
                return
            load_target = self._loader.resolve_target()
            self._vlm_model, self._vlm_processor = vlm_load(load_target)
            self._vlm_config = vlm_load_config(load_target)

    def _prepare_vlm_prompt(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str],
    ) -> tuple[str, list[Any]]:
        self._ensure_vlm_loaded()
        vlm_messages, image_inputs = self._build_vlm_messages_and_images(messages, system)
        prompt = vlm_apply_chat_template(
            self._vlm_processor,
            self._vlm_config,
            vlm_messages,
            add_generation_prompt=True,
            enable_thinking=False,
            num_images=len(image_inputs),
        )
        return prompt, image_inputs

    def _generate_vlm_text(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str],
        *,
        max_tokens: int,
        temperature: float,
        top_p: float,
        repetition_penalty: float,
    ) -> str:
        prompt, image_inputs = self._prepare_vlm_prompt(messages, system)
        result = vlm_generate(
            self._vlm_model,
            self._vlm_processor,
            prompt,
            image=image_inputs,
            verbose=False,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
        )
        return str(getattr(result, "text", result or "")).strip()

    def _stream_vlm_responses(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str],
        *,
        max_tokens: int,
        temperature: float,
        top_p: float,
        repetition_penalty: float,
    ):
        prompt, image_inputs = self._prepare_vlm_prompt(messages, system)
        return vlm_stream_generate(
            self._vlm_model,
            self._vlm_processor,
            prompt,
            image=image_inputs,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
        )

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
            top_p=top_p,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            repetition_penalty=repetition_penalty,
            system_text=system_str,
            user_text=user_text,
        )
        await self._prefill_queue.put(slot)
        self._not_empty.set()

        # 从 token_queue 流式消费：None = 结束，Exception = 错误
        # finally 在协程被 Cancel（客户端断开）时标记 done，通知调度器跳过后续生成
        try:
            while True:
                item = await slot.token_queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            slot.done = True

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
        if not self._messages_include_images(messages):
            async for token in super().generate_messages_stream(
                messages,
                system,
                max_tokens,
                temperature,
                top_p,
                top_k=top_k,
                min_p=min_p,
                presence_penalty=presence_penalty,
                repetition_penalty=repetition_penalty,
            ):
                yield token
            return

        if not self.is_ready:
            raise RuntimeError("LocalProvider not loaded. Call load() first.")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        # BUG-06: 客户端断开时 daemon 线程无法被取消，持续占用 GPU
        # stop_event 让消费方协程通知后台线程提前退出
        stop_event = threading.Event()

        def _run():
            try:
                for response in self._stream_vlm_responses(
                    messages,
                    system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    repetition_penalty=repetition_penalty,
                ):
                    if stop_event.is_set():
                        break
                    text = str(getattr(response, "text", response or ""))
                    if text:
                        loop.call_soon_threadsafe(queue.put_nowait, text)
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=_run, daemon=True).start()

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            stop_event.set()

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
        if not self._messages_include_images(messages):
            return await super().generate_messages(
                messages,
                system,
                max_tokens,
                temperature,
                top_p,
                top_k=top_k,
                min_p=min_p,
                presence_penalty=presence_penalty,
                repetition_penalty=repetition_penalty,
            )

        if not self.is_ready:
            raise RuntimeError("LocalProvider not loaded. Call load() first.")

        return await asyncio.to_thread(
            self._generate_vlm_text,
            messages,
            system,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
        )
