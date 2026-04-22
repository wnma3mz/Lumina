from __future__ import annotations

import asyncio

import pytest

import lumina.providers.local as local_mod

from tests.providers.local_provider_test_helpers import (
    LegacySchedulerProvider,
    MODEL_PATH,
    NoSystemCacheBatchProvider,
    SyntheticBatchedDecodeProvider,
    SyntheticInterleavedProvider,
    _collect_text,
    _make_slot,
    _median_makespan,
    _run_workload,
    _stop_provider,
    mx,
)
from lumina.providers.local import LocalProvider, _RequestSlot


@pytest.mark.anyio
async def test_interleaved_scheduler_gives_late_request_token_before_first_request_finishes():
    provider = SyntheticInterleavedProvider(
        prefill_delay=0.03,
        decode_delay=0.03,
        max_new_prefill_per_iter=2,
    )
    _, results = await _run_workload(provider, delays=[0.0, 0.015], max_tokens=5)

    assert results["req-1"]["tokens"] == ["t1", "t2", "t3", "t4", "t5"]
    assert results["req-1"]["first_token_offset"] < results["req-0"]["end_offset"]


@pytest.mark.anyio
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


@pytest.mark.anyio
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


@pytest.mark.anyio
async def test_batch_engine_matches_legacy_scheduler_outputs():
    if not MODEL_PATH.exists():
        pytest.skip("local model not available")

    batch_provider = NoSystemCacheBatchProvider(str(MODEL_PATH))
    batch_provider.load()

    legacy_provider = LegacySchedulerProvider(str(MODEL_PATH))
    legacy_provider._model = batch_provider._model
    legacy_provider._tokenizer = batch_provider._tokenizer
    legacy_provider._prompt_builder = batch_provider._prompt_builder

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


def test_finish_reason_length_emits_none_terminator():
    slot = _make_slot()

    finish_reason = "length"
    if finish_reason is not None:
        slot.done = True
        slot.token_queue.put_nowait(None)

    sentinel = slot.token_queue.get_nowait()
    assert sentinel is None
    assert slot.done is True


def test_finish_reason_stop_also_emits_none_terminator():
    slot = _make_slot()

    finish_reason = "stop"
    if finish_reason is not None:
        slot.done = True
        slot.token_queue.put_nowait(None)

    sentinel = slot.token_queue.get_nowait()
    assert sentinel is None
    assert slot.done is True
