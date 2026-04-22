from __future__ import annotations

import asyncio
import statistics
import time
from contextlib import suppress
from pathlib import Path

import pytest

mx = pytest.importorskip("mlx.core", reason="mlx not available on this platform")

import lumina.providers.mlx_loader as mlx_loader_mod  # noqa: E402
from lumina.providers.local import LocalProvider, _RequestSlot  # noqa: E402

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
        self._model = object()

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


def _fake_loader_load(offload_embedding=True, offload_vision=True, offload_audio=True):
    return FakeLoadedModel(), FakeLoadedTokenizer(), None, None


def _make_slot() -> _RequestSlot:
    return _RequestSlot(
        request_id="test-uid",
        prompt_tokens=mx.array([], dtype=mx.uint32),
        max_tokens=10,
        temperature=0.0,
    )


def _make_scheduler():
    from lumina.providers.scheduler import MlxBatchScheduler

    return MlxBatchScheduler(
        model=object(),
        tokenizer=object(),
        batch_generator=None,
        batch_executor=None,
        prepare_prompt_fn=lambda slot: ([], None),
        emit_token_fn=lambda slot, tok: None,
    )


def _make_loader() -> mlx_loader_mod.MlxModelLoader:
    return mlx_loader_mod.MlxModelLoader(
        model_path="synthetic",
        max_new_prefill_per_iter=2,
        use_builtin_batch_engine_fn=lambda: False,
        use_dedicated_batch_executor_fn=lambda: False,
        eos_ids_fn=lambda: set(),
    )
