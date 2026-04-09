"""
LocalProvider 调度回归测试。

这些测试不跑真实 MLX 模型，而是用可控的 synthetic provider 模拟
prefill / decode 成本。这样可以稳定验证两件事：

1. 当前交错调度确实能改善后到请求的 TTFT。
2. 后续若把 `_advance_batch()` 换成真正的 continuous batching，
   可以用同一套测试验证总耗时是否下降。
"""

from __future__ import annotations

import asyncio
import statistics
import time
from contextlib import suppress
from pathlib import Path

import pytest
import mlx.core as mx

import lumina.providers.local as local_mod
from lumina.providers.local import LocalProvider, _RequestSlot


MODEL_PATH = Path.home() / ".lumina" / "models" / "qwen3.5-0.8b-4bit"


class SyntheticInterleavedProvider(LocalProvider):
    """用 sleep 模拟 prefill / decode 成本，复用真实调度循环。"""

    def __init__(
        self,
        *,
        prefill_delay: float,
        decode_delay: float,
        max_new_prefill_per_iter: int = 4,
    ):
        super().__init__(
            model_path="synthetic",
            max_new_prefill_per_iter=max_new_prefill_per_iter,
        )
        self.prefill_delay = prefill_delay
        self.decode_delay = decode_delay
        self._model = object()  # 仅用于通过 is_ready 检查

    def _build_prompt_tokens(self, system: str, user_text: str):
        return user_text

    def _emit_next_token(self, slot) -> None:
        next_idx = slot.n_tokens + 1
        slot.n_tokens = next_idx
        self._put_token(slot, f"t{next_idx}")
        if slot.n_tokens >= slot.max_tokens:
            slot.done = True
            self._put_token(slot, None)

    def _do_prefill(self, slot) -> None:
        time.sleep(self.prefill_delay)
        self._emit_next_token(slot)

    def _advance_one(self, slot) -> None:
        if slot.done:
            return
        time.sleep(self.decode_delay)
        self._emit_next_token(slot)


class SyntheticBatchedDecodeProvider(SyntheticInterleavedProvider):
    """模拟 continuous batching：同一轮 decode 只付一次共享成本。"""

    def __init__(
        self,
        *,
        prefill_delay: float,
        decode_delay: float,
        batched_decode_delay: float,
        max_new_prefill_per_iter: int = 4,
    ):
        super().__init__(
            prefill_delay=prefill_delay,
            decode_delay=decode_delay,
            max_new_prefill_per_iter=max_new_prefill_per_iter,
        )
        self.batched_decode_delay = batched_decode_delay

    def _advance_batch(self, slots) -> None:
        active = [slot for slot in slots if not slot.done]
        if not active:
            return
        time.sleep(self.batched_decode_delay)
        for slot in active:
            self._emit_next_token(slot)


async def _stop_provider(provider: LocalProvider) -> None:
    if provider._worker_task is None:
        return
    provider._worker_task.cancel()
    with suppress(asyncio.CancelledError):
        await provider._worker_task
    provider._worker_task = None


async def _consume_stream(provider: LocalProvider, name: str, *, delay: float, max_tokens: int, t0: float) -> dict:
    await asyncio.sleep(delay)
    start = time.perf_counter()
    first_token_offset = None
    tokens = []

    async for token in provider.generate_stream(name, system=None, max_tokens=max_tokens, temperature=0.0):
        now = time.perf_counter()
        tokens.append(token)
        if first_token_offset is None:
            first_token_offset = now - t0

    end = time.perf_counter()
    return {
        "tokens": tokens,
        "start_offset": start - t0,
        "first_token_offset": first_token_offset,
        "end_offset": end - t0,
        "ttft": (first_token_offset - (start - t0)) if first_token_offset is not None else None,
    }


async def _run_workload(provider: LocalProvider, *, delays: list[float], max_tokens: int) -> tuple[float, dict[str, dict]]:
    t0 = time.perf_counter()
    tasks = [
        asyncio.create_task(
            _consume_stream(provider, f"req-{idx}", delay=delay, max_tokens=max_tokens, t0=t0)
        )
        for idx, delay in enumerate(delays)
    ]
    results_list = await asyncio.gather(*tasks)
    makespan = time.perf_counter() - t0
    await _stop_provider(provider)
    return makespan, {f"req-{idx}": result for idx, result in enumerate(results_list)}


async def _median_makespan(provider_factory, *, repeats: int, delays: list[float], max_tokens: int) -> tuple[float, dict[str, dict]]:
    samples = []
    last_results = None
    for _ in range(repeats):
        makespan, results = await _run_workload(provider_factory(), delays=delays, max_tokens=max_tokens)
        samples.append(makespan)
        last_results = results
    return statistics.median(samples), last_results


@pytest.mark.asyncio
async def test_interleaved_scheduler_gives_late_request_token_before_first_request_finishes():
    provider = SyntheticInterleavedProvider(
        prefill_delay=0.03,
        decode_delay=0.03,
        max_new_prefill_per_iter=2,
    )
    _, results = await _run_workload(provider, delays=[0.0, 0.015], max_tokens=5)

    assert results["req-1"]["tokens"] == ["t1", "t2", "t3", "t4", "t5"]
    assert results["req-1"]["first_token_offset"] < results["req-0"]["end_offset"]


@pytest.mark.asyncio
async def test_batched_decode_strategy_preserves_per_request_output():
    provider = SyntheticBatchedDecodeProvider(
        prefill_delay=0.01,
        decode_delay=0.01,
        batched_decode_delay=0.004,
        max_new_prefill_per_iter=3,
    )
    _, results = await _run_workload(provider, delays=[0.0, 0.003, 0.006], max_tokens=4)

    for idx in range(3):
        assert results[f"req-{idx}"]["tokens"] == ["t1", "t2", "t3", "t4"]


@pytest.mark.asyncio
async def test_batched_decode_strategy_reduces_makespan():
    delays = [0.0, 0.005, 0.01, 0.015]
    max_tokens = 6

    interleaved_makespan, interleaved_results = await _median_makespan(
        lambda: SyntheticInterleavedProvider(
            prefill_delay=0.02,
            decode_delay=0.02,
            max_new_prefill_per_iter=4,
        ),
        repeats=3,
        delays=delays,
        max_tokens=max_tokens,
    )
    batched_makespan, batched_results = await _median_makespan(
        lambda: SyntheticBatchedDecodeProvider(
            prefill_delay=0.02,
            decode_delay=0.02,
            batched_decode_delay=0.008,
            max_new_prefill_per_iter=4,
        ),
        repeats=3,
        delays=delays,
        max_tokens=max_tokens,
    )

    assert batched_makespan < interleaved_makespan * 0.75
    assert batched_results["req-3"]["tokens"] == interleaved_results["req-3"]["tokens"]


def test_prefill_batch_uses_batched_model_call(monkeypatch):
    class FakeTokenizer:
        eos_token_id = 999999

        def decode(self, token_ids):
            return "".join(f"<{tok}>" for tok in token_ids)

    class FakeBatchCache:
        def __init__(self):
            self.finalized = False

        @property
        def state(self):
            return mx.array([0])

        def finalize(self):
            self.finalized = True

        def extract(self, idx):
            return f"cache-{idx}"

    class FakeModel:
        def __init__(self):
            self.batch_sizes = []

        def __call__(self, inputs, cache=None):
            self.batch_sizes.append(tuple(inputs.shape))
            batch, seq = inputs.shape
            return mx.zeros((batch, seq, 4))

    provider = LocalProvider(model_path="synthetic")
    provider._model = FakeModel()
    provider._tokenizer = FakeTokenizer()
    provider._loop = asyncio.new_event_loop()

    monkeypatch.setattr(local_mod, "_make_cache", lambda model, left_padding, max_kv_size: [FakeBatchCache()])

    slots = [
        _RequestSlot(request_id="a", prompt_tokens=mx.array([11, 12, 13]), max_tokens=4, temperature=0.0),
        _RequestSlot(request_id="b", prompt_tokens=mx.array([21, 22]), max_tokens=4, temperature=0.0),
    ]
    try:
        active = provider._prefill_batch(slots)
    finally:
        provider._loop.close()

    assert len(active) == 2
    assert any(shape[0] == 2 for shape in provider._model.batch_sizes)
    assert slots[0].prompt_cache == ["cache-0"]
    assert slots[1].prompt_cache == ["cache-1"]


class LegacySchedulerProvider(LocalProvider):
    def _use_builtin_batch_engine(self) -> bool:
        return False


class NoSystemCacheProvider(LocalProvider):
    def _get_or_create_system_prompt_cache(self, system_text: str):
        return None


class NoSystemCacheBatchProvider(NoSystemCacheProvider):
    def _use_builtin_batch_engine(self) -> bool:
        return True


class FakeLoadedModel:
    def parameters(self):
        return []


class FakeLoadedTokenizer:
    eos_token_id = 0

    def encode(self, text):
        return [11, 12, 13]

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        return "warmup"


async def _collect_text(
    provider: LocalProvider,
    prompt: str,
    *,
    max_tokens: int = 16,
    system: str | None = None,
) -> str:
    parts = []
    async for token in provider.generate_stream(prompt, system=system, max_tokens=max_tokens, temperature=0.0):
        parts.append(token)
    return "".join(parts)


@pytest.mark.asyncio
async def test_batch_engine_matches_legacy_scheduler_outputs():
    if not MODEL_PATH.exists():
        pytest.skip("local model not available")

    batch_provider = NoSystemCacheBatchProvider(str(MODEL_PATH))
    batch_provider.load()

    legacy_provider = LegacySchedulerProvider(str(MODEL_PATH))
    legacy_provider._model = batch_provider._model
    legacy_provider._tokenizer = batch_provider._tokenizer

    prompts = [
        "请用一句话解释 continuous batching。",
        "Why does TTFT matter for short prompts?",
        "列出两个并发调优时常见的误区。",
    ]

    try:
        batch_results = [await _collect_text(batch_provider, prompt) for prompt in prompts]
        legacy_results = [await _collect_text(legacy_provider, prompt) for prompt in prompts]
    finally:
        await _stop_provider(batch_provider)
        await _stop_provider(legacy_provider)

    assert batch_results == legacy_results


def test_load_runs_warmup_by_default(monkeypatch):
    provider = LocalProvider(model_path="synthetic")
    warmup_calls = []

    monkeypatch.setattr(local_mod, "load", lambda model_path: (FakeLoadedModel(), FakeLoadedTokenizer()))
    monkeypatch.setattr(provider, "_init_batch_engine", lambda: None)
    monkeypatch.setattr(provider, "_run_warmup", lambda: warmup_calls.append("warmup"))

    provider.load()

    assert warmup_calls == ["warmup"]


def test_load_skips_warmup_when_disabled(monkeypatch):
    provider = LocalProvider(model_path="synthetic", enable_warmup=False)
    warmup_calls = []

    monkeypatch.setattr(local_mod, "load", lambda model_path: (FakeLoadedModel(), FakeLoadedTokenizer()))
    monkeypatch.setattr(provider, "_init_batch_engine", lambda: None)
    monkeypatch.setattr(provider, "_run_warmup", lambda: warmup_calls.append("warmup"))

    provider.load()

    assert warmup_calls == []


def test_load_keeps_provider_ready_when_warmup_fails(monkeypatch):
    provider = LocalProvider(model_path="synthetic")

    monkeypatch.setattr(local_mod, "load", lambda model_path: (FakeLoadedModel(), FakeLoadedTokenizer()))
    monkeypatch.setattr(provider, "_init_batch_engine", lambda: None)
    monkeypatch.setattr(provider, "_run_warmup", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    provider.load()

    assert provider.is_ready


def test_system_prompt_cache_reuses_prefix_cache(monkeypatch):
    class FakeCacheLayer:
        def __init__(self, state):
            self._state = state

        @property
        def state(self):
            return self._state

        @property
        def meta_state(self):
            return ()

        @classmethod
        def from_state(cls, state, meta_state):
            return cls(state)

    provider = LocalProvider(model_path="synthetic")
    provider._model = object()
    provider._tokenizer = object()

    prefill_calls = []

    monkeypatch.setattr(provider, "_derive_system_prefix_tokens", lambda system_text: [1, 2, 3])
    monkeypatch.setattr(local_mod.mlx_cache, "make_prompt_cache", lambda model: [FakeCacheLayer((mx.array([1, 2, 3]),))])
    monkeypatch.setattr(
        provider,
        "_prefill_full_prompt_cache",
        lambda prompt_tokens, prompt_cache: prefill_calls.append(list(prompt_tokens)),
    )

    first = provider._get_or_create_system_prompt_cache("same system")
    second = provider._get_or_create_system_prompt_cache("same system")

    assert first is second
    assert prefill_calls == [[1, 2, 3]]


def test_prepare_batch_generator_prompt_falls_back_when_prefix_mismatches():
    class FakeCacheLayer:
        def __init__(self, state):
            self._state = state

        @property
        def state(self):
            return self._state

        @property
        def meta_state(self):
            return ()

        @classmethod
        def from_state(cls, state, meta_state):
            return cls(state)

    provider = LocalProvider(model_path="synthetic")
    provider._model = object()
    provider._tokenizer = object()
    provider._system_prompt_cache["sys"] = local_mod._SystemPromptCacheEntry(
        system_text="sys",
        prefix_tokens=[9, 9],
        prompt_cache=[FakeCacheLayer((mx.array([1]),))],
    )

    slot = _RequestSlot(
        request_id="slot",
        prompt_tokens=mx.array([1, 2, 3, 4]),
        max_tokens=8,
        temperature=0.0,
        system_text="sys",
        user_text="hello",
    )
    prompt_tokens, prompt_cache = provider._prepare_batch_generator_prompt(slot)

    assert prompt_tokens == [1, 2, 3, 4]
    assert prompt_cache is None


def test_prepare_batch_generator_prompt_reconstructs_full_prompt_when_cache_hits():
    class FakeCacheLayer:
        def __init__(self, state):
            self._state = state

        @property
        def state(self):
            return self._state

        @property
        def meta_state(self):
            return ()

        @classmethod
        def from_state(cls, state, meta_state):
            return cls(state)

    provider = LocalProvider(model_path="synthetic")
    provider._model = object()
    provider._tokenizer = object()
    provider._system_prompt_cache["sys"] = local_mod._SystemPromptCacheEntry(
        system_text="sys",
        prefix_tokens=[1, 2],
        prompt_cache=[FakeCacheLayer((mx.array([7, 8]),))],
    )

    slot = _RequestSlot(
        request_id="slot",
        prompt_tokens=mx.array([1, 2, 3, 4, 5]),
        max_tokens=8,
        temperature=0.0,
        system_text="sys",
        user_text="hello",
    )
    suffix_tokens, prompt_cache = provider._prepare_batch_generator_prompt(slot)

    assert prompt_cache is not None
    assert provider._system_prompt_cache["sys"].prefix_tokens + suffix_tokens == [1, 2, 3, 4, 5]
