"""
基准脚本：比较不同 prefill 接入宽度对并发请求的影响。

运行：
    uv run python scripts/benchmark_local_provider.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

from lumina.providers.local import LocalProvider


DEFAULT_MODEL_PATH = Path.home() / ".lumina" / "models" / "qwen3.5-0.8b-4bit"


class SingleDecodeProvider(LocalProvider):
    """禁用 batch decode，只保留逐请求单步推进。"""

    def _advance_batch(self, slots):
        for slot in slots:
            if not slot.done:
                self._advance_one(slot)


class NoReuseBatchProvider(LocalProvider):
    """保留 batch decode，但每步都 materialize，禁用 merged cache 复用。"""

    def _advance_batch(self, slots):
        super()._advance_batch(slots)
        slot_by_id = {
            slot.request_id: slot
            for slot in slots
            if not slot.done and slot.prompt_cache is not None and slot.next_input_token is not None
        }
        self._materialize_continuous_batch(slot_by_id)


class LegacyPutProvider(LocalProvider):
    """在 loop 内也强制走线程安全投递，模拟旧写法。"""

    def _put_token_local(self, slot, value) -> None:
        self._put_token(slot, value)


class SerialPrefillProvider(LocalProvider):
    """禁用 prefill batching，恢复逐请求 prefill + 首 token。"""

    def _prefill_batch(self, slots):
        newly_active = []
        for slot in slots:
            self._do_prefill(slot)
            if not slot.done:
                newly_active.append(slot)
        return newly_active


class LegacySchedulerProvider(LocalProvider):
    """禁用 BatchGenerator engine，使用旧自定义 scheduler。"""

    def _use_builtin_batch_engine(self) -> bool:
        return False


class SharedExecutorBatchProvider(LocalProvider):
    """BatchGenerator 仍使用默认线程池，而不是专用线程。"""

    def _use_dedicated_batch_executor(self) -> bool:
        return False


class NoSystemCacheProvider(LocalProvider):
    """禁用 system prompt prefix cache。"""

    def _use_builtin_batch_engine(self) -> bool:
        return True

    def _get_or_create_system_prompt_cache(self, system_text: str):
        return None


class NoWarmupProvider(LocalProvider):
    """禁用 load 后的 shader warmup。"""

    def __init__(self, *args, **kwargs):
        kwargs["enable_warmup"] = False
        super().__init__(*args, **kwargs)

    def _use_builtin_batch_engine(self) -> bool:
        return True


async def _stop_provider(provider: LocalProvider) -> None:
    if provider._worker_task is None:
        return
    provider._worker_task.cancel()
    with suppress(asyncio.CancelledError):
        await provider._worker_task
    provider._worker_task = None


async def _consume(
    provider: LocalProvider,
    prompt: str,
    *,
    max_tokens: int,
    delay: float,
    t0: float,
    system: str | None = None,
) -> dict:
    await asyncio.sleep(delay)
    start = time.perf_counter()
    first_token_offset = None
    parts = []

    async for _token in provider.generate_stream(prompt, system=system, max_tokens=max_tokens, temperature=0.2):
        parts.append(_token)
        now = time.perf_counter()
        if first_token_offset is None:
            first_token_offset = now - t0

    end = time.perf_counter()
    text = "".join(parts)
    return {
        "ttft": None if first_token_offset is None else first_token_offset - (start - t0),
        "latency": end - start,
        "char_count": len(text),
    }


async def _run_provider_once(
    provider: LocalProvider,
    prompts: list[str],
    *,
    max_tokens: int,
    stagger: float,
    system: str | None = None,
) -> dict:
    t0 = time.perf_counter()
    tasks = [
        asyncio.create_task(
            _consume(provider, prompt, max_tokens=max_tokens, delay=idx * stagger, t0=t0)
            if system is None
            else _consume(provider, prompt, max_tokens=max_tokens, delay=idx * stagger, t0=t0, system=system)
        )
        for idx, prompt in enumerate(prompts)
    ]
    per_request = await asyncio.gather(*tasks)
    makespan = time.perf_counter() - t0
    await _stop_provider(provider)

    total_chars = sum(item["char_count"] for item in per_request)
    ttfts = [item["ttft"] for item in per_request if item["ttft"] is not None]
    return {
        "makespan": makespan,
        "avg_ttft": statistics.mean(ttfts),
        "p95_ttft": max(ttfts),
        "chars_per_s": total_chars / makespan,
    }


def _reuse_loaded_artifacts(provider: LocalProvider, base_provider: LocalProvider) -> None:
    provider._model = base_provider._model
    provider._tokenizer = base_provider._tokenizer
    provider._init_batch_engine()


async def benchmark_provider(
    model_path: Path,
    repeats: int,
    max_tokens: int,
    stagger: float,
    prefill_widths: list[int],
    compare_decode_modes: bool,
) -> dict:
    provider_cls = LocalProvider if compare_decode_modes else LocalProvider
    base_provider = provider_cls(str(model_path), max_new_prefill_per_iter=max(prefill_widths))
    base_provider.load()

    prompts = [
        "请用三点总结 continuous batching 的核心收益。",
        "Why does token streaming latency matter for short requests?",
        "请简要解释 TTFT 和吞吐量之间的关系。",
        "List two practical risks when increasing concurrency blindly.",
    ]

    if compare_decode_modes:
        samples_by_key = {"single": [], "batched": []}
    else:
        samples_by_key = {width: [] for width in prefill_widths}

    for _ in range(repeats):
        if compare_decode_modes:
            for key, cls in [("single", SingleDecodeProvider), ("batched", LocalProvider)]:
                provider = cls(str(model_path), max_new_prefill_per_iter=max(prefill_widths))
                _reuse_loaded_artifacts(provider, base_provider)
                samples_by_key[key].append(
                    await _run_provider_once(provider, prompts, max_tokens=max_tokens, stagger=stagger)
                )
        else:
            for width in prefill_widths:
                provider = LocalProvider(str(model_path), max_new_prefill_per_iter=width)
                _reuse_loaded_artifacts(provider, base_provider)
                samples_by_key[width].append(
                    await _run_provider_once(provider, prompts, max_tokens=max_tokens, stagger=stagger)
                )

    def summarize(samples: list[dict]) -> dict:
        return {
            "makespan": statistics.median(item["makespan"] for item in samples),
            "avg_ttft": statistics.median(item["avg_ttft"] for item in samples),
            "p95_ttft": statistics.median(item["p95_ttft"] for item in samples),
            "chars_per_s": statistics.median(item["chars_per_s"] for item in samples),
        }

    return {key: summarize(samples) for key, samples in samples_by_key.items()}


def _print_provider_result(name: str, result: dict) -> None:
    print(f"{name}:")
    print(f"  makespan   : {result['makespan']:.3f}s")
    print(f"  avg TTFT   : {result['avg_ttft']:.3f}s")
    print(f"  p95 TTFT   : {result['p95_ttft']:.3f}s")
    print(f"  throughput : {result['chars_per_s']:.2f} char/s")


def _run_cold_start_worker(args, *, disable_warmup: bool) -> dict:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--cold-start-worker",
        "--model-path",
        str(args.model_path),
        "--provider-max-tokens",
        str(args.provider_max_tokens),
    ]
    if disable_warmup:
        command.append("--disable-warmup")
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1])


async def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark LocalProvider prefill widths.")
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--provider-repeats", type=int, default=2)
    parser.add_argument("--provider-max-tokens", type=int, default=48)
    parser.add_argument("--provider-stagger", type=float, default=0.03)
    parser.add_argument("--baseline-width", type=int, default=2)
    parser.add_argument("--candidate-width", type=int, default=4)
    parser.add_argument("--compare-decode-modes", action="store_true")
    parser.add_argument("--compare-cache-reuse", action="store_true")
    parser.add_argument("--compare-put-mode", action="store_true")
    parser.add_argument("--compare-prefill-batch", action="store_true")
    parser.add_argument("--compare-engine-mode", action="store_true")
    parser.add_argument("--compare-executor-mode", action="store_true")
    parser.add_argument("--compare-system-cache", action="store_true")
    parser.add_argument("--compare-warmup", action="store_true")
    parser.add_argument("--cold-start-worker", action="store_true")
    parser.add_argument("--disable-warmup", action="store_true")
    args = parser.parse_args()

    shared_system = "You are a concise assistant focused on systems and performance."
    cold_start_prompt = "请简要解释为什么模型首个请求通常比后续请求更慢。"

    if args.cold_start_worker:
        provider_cls = NoWarmupProvider if args.disable_warmup else LocalProvider
        provider = provider_cls(str(args.model_path))
        load_started_at = time.perf_counter()
        provider.load()
        load_latency = time.perf_counter() - load_started_at
        result = await _run_provider_once(
            provider,
            [cold_start_prompt],
            max_tokens=args.provider_max_tokens,
            stagger=0.0,
            system=shared_system,
        )
        print(json.dumps({"load_latency": load_latency, **result}))
        return

    print("== Provider End-to-End Benchmark ==")
    if args.compare_warmup:
        samples = {"warmup=off": [], "warmup=on": []}
        for _ in range(args.provider_repeats):
            samples["warmup=off"].append(_run_cold_start_worker(args, disable_warmup=True))
            samples["warmup=on"].append(_run_cold_start_worker(args, disable_warmup=False))
        result = {
            key: {
                "load_latency": statistics.median(item["load_latency"] for item in value),
                "makespan": statistics.median(item["makespan"] for item in value),
                "avg_ttft": statistics.median(item["avg_ttft"] for item in value),
                "p95_ttft": statistics.median(item["p95_ttft"] for item in value),
                "chars_per_s": statistics.median(item["chars_per_s"] for item in value),
            }
            for key, value in samples.items()
        }
        _print_provider_result("warmup=off", result["warmup=off"])
        print(f"  load time  : {result['warmup=off']['load_latency']:.3f}s")
        _print_provider_result("warmup=on", result["warmup=on"])
        print(f"  load time  : {result['warmup=on']['load_latency']:.3f}s")
        makespan_gain = result["warmup=off"]["makespan"] / result["warmup=on"]["makespan"]
        ttft_gain = result["warmup=off"]["avg_ttft"] / result["warmup=on"]["avg_ttft"]
        throughput_gain = result["warmup=on"]["chars_per_s"] / result["warmup=off"]["chars_per_s"]
    elif args.compare_system_cache:
        base_provider = LocalProvider(str(args.model_path), max_new_prefill_per_iter=max(args.baseline_width, args.candidate_width))
        base_provider.load()
        prompts = [
            "请用三点总结 continuous batching 的核心收益。",
            "Why does token streaming latency matter for short requests?",
            "请简要解释 TTFT 和吞吐量之间的关系。",
            "List two practical risks when increasing concurrency blindly.",
        ]
        samples = {"system-cache=off": [], "system-cache=on": []}
        for _ in range(args.provider_repeats):
            for key, cls in [("system-cache=off", NoSystemCacheProvider), ("system-cache=on", LocalProvider)]:
                provider = cls(str(args.model_path), max_new_prefill_per_iter=max(args.baseline_width, args.candidate_width))
                _reuse_loaded_artifacts(provider, base_provider)
                samples[key].append(
                    await _run_provider_once(
                        provider,
                        prompts,
                        max_tokens=args.provider_max_tokens,
                        stagger=args.provider_stagger,
                        system=shared_system,
                    )
                )
        result = {
            key: {
                "makespan": statistics.median(item["makespan"] for item in value),
                "avg_ttft": statistics.median(item["avg_ttft"] for item in value),
                "p95_ttft": statistics.median(item["p95_ttft"] for item in value),
                "chars_per_s": statistics.median(item["chars_per_s"] for item in value),
            }
            for key, value in samples.items()
        }
        _print_provider_result("system-cache=off", result["system-cache=off"])
        _print_provider_result("system-cache=on", result["system-cache=on"])
        makespan_gain = result["system-cache=off"]["makespan"] / result["system-cache=on"]["makespan"]
        ttft_gain = result["system-cache=off"]["avg_ttft"] / result["system-cache=on"]["avg_ttft"]
        throughput_gain = result["system-cache=on"]["chars_per_s"] / result["system-cache=off"]["chars_per_s"]
    elif args.compare_executor_mode:
        base_provider = LocalProvider(str(args.model_path), max_new_prefill_per_iter=max(args.baseline_width, args.candidate_width))
        base_provider.load()
        prompts = [
            "请用三点总结 continuous batching 的核心收益。",
            "Why does token streaming latency matter for short requests?",
            "请简要解释 TTFT 和吞吐量之间的关系。",
            "List two practical risks when increasing concurrency blindly.",
        ]
        samples = {"executor=shared": [], "executor=dedicated": []}
        for _ in range(args.provider_repeats):
            for key, cls in [("executor=shared", SharedExecutorBatchProvider), ("executor=dedicated", LocalProvider)]:
                provider = cls(str(args.model_path), max_new_prefill_per_iter=max(args.baseline_width, args.candidate_width))
                _reuse_loaded_artifacts(provider, base_provider)
                samples[key].append(
                    await _run_provider_once(provider, prompts, max_tokens=args.provider_max_tokens, stagger=args.provider_stagger)
                )
        result = {
            key: {
                "makespan": statistics.median(item["makespan"] for item in value),
                "avg_ttft": statistics.median(item["avg_ttft"] for item in value),
                "p95_ttft": statistics.median(item["p95_ttft"] for item in value),
                "chars_per_s": statistics.median(item["chars_per_s"] for item in value),
            }
            for key, value in samples.items()
        }
        _print_provider_result("executor=shared", result["executor=shared"])
        _print_provider_result("executor=dedicated", result["executor=dedicated"])
        makespan_gain = result["executor=shared"]["makespan"] / result["executor=dedicated"]["makespan"]
        ttft_gain = result["executor=shared"]["avg_ttft"] / result["executor=dedicated"]["avg_ttft"]
        throughput_gain = result["executor=dedicated"]["chars_per_s"] / result["executor=shared"]["chars_per_s"]
    elif args.compare_engine_mode:
        base_provider = LocalProvider(str(args.model_path), max_new_prefill_per_iter=max(args.baseline_width, args.candidate_width))
        base_provider.load()
        prompts = [
            "请用三点总结 continuous batching 的核心收益。",
            "Why does token streaming latency matter for short requests?",
            "请简要解释 TTFT 和吞吐量之间的关系。",
            "List two practical risks when increasing concurrency blindly.",
        ]
        samples = {"engine=legacy": [], "engine=batch_generator": []}
        for _ in range(args.provider_repeats):
            for key, cls in [("engine=legacy", LegacySchedulerProvider), ("engine=batch_generator", LocalProvider)]:
                provider = cls(str(args.model_path), max_new_prefill_per_iter=max(args.baseline_width, args.candidate_width))
                _reuse_loaded_artifacts(provider, base_provider)
                samples[key].append(
                    await _run_provider_once(provider, prompts, max_tokens=args.provider_max_tokens, stagger=args.provider_stagger)
                )
        result = {
            key: {
                "makespan": statistics.median(item["makespan"] for item in value),
                "avg_ttft": statistics.median(item["avg_ttft"] for item in value),
                "p95_ttft": statistics.median(item["p95_ttft"] for item in value),
                "chars_per_s": statistics.median(item["chars_per_s"] for item in value),
            }
            for key, value in samples.items()
        }
        _print_provider_result("engine=legacy", result["engine=legacy"])
        _print_provider_result("engine=batch_generator", result["engine=batch_generator"])
        makespan_gain = result["engine=legacy"]["makespan"] / result["engine=batch_generator"]["makespan"]
        ttft_gain = result["engine=legacy"]["avg_ttft"] / result["engine=batch_generator"]["avg_ttft"]
        throughput_gain = result["engine=batch_generator"]["chars_per_s"] / result["engine=legacy"]["chars_per_s"]
    elif args.compare_prefill_batch:
        base_provider = LocalProvider(str(args.model_path), max_new_prefill_per_iter=max(args.baseline_width, args.candidate_width))
        base_provider.load()
        prompts = [
            "请用三点总结 continuous batching 的核心收益。",
            "Why does token streaming latency matter for short requests?",
            "请简要解释 TTFT 和吞吐量之间的关系。",
            "List two practical risks when increasing concurrency blindly.",
        ]
        samples = {"prefill=serial": [], "prefill=batched": []}
        for _ in range(args.provider_repeats):
            for key, cls in [("prefill=serial", SerialPrefillProvider), ("prefill=batched", LocalProvider)]:
                provider = cls(str(args.model_path), max_new_prefill_per_iter=max(args.baseline_width, args.candidate_width))
                _reuse_loaded_artifacts(provider, base_provider)
                samples[key].append(
                    await _run_provider_once(provider, prompts, max_tokens=args.provider_max_tokens, stagger=args.provider_stagger)
                )
        result = {
            key: {
                "makespan": statistics.median(item["makespan"] for item in value),
                "avg_ttft": statistics.median(item["avg_ttft"] for item in value),
                "p95_ttft": statistics.median(item["p95_ttft"] for item in value),
                "chars_per_s": statistics.median(item["chars_per_s"] for item in value),
            }
            for key, value in samples.items()
        }
        _print_provider_result("prefill=serial", result["prefill=serial"])
        _print_provider_result("prefill=batched", result["prefill=batched"])
        makespan_gain = result["prefill=serial"]["makespan"] / result["prefill=batched"]["makespan"]
        ttft_gain = result["prefill=serial"]["avg_ttft"] / result["prefill=batched"]["avg_ttft"]
        throughput_gain = result["prefill=batched"]["chars_per_s"] / result["prefill=serial"]["chars_per_s"]
    elif args.compare_put_mode:
        base_provider = LocalProvider(str(args.model_path), max_new_prefill_per_iter=max(args.baseline_width, args.candidate_width))
        base_provider.load()
        prompts = [
            "请用三点总结 continuous batching 的核心收益。",
            "Why does token streaming latency matter for short requests?",
            "请简要解释 TTFT 和吞吐量之间的关系。",
            "List two practical risks when increasing concurrency blindly.",
        ]
        samples = {"put=legacy": [], "put=fast": []}
        for _ in range(args.provider_repeats):
            for key, cls in [("put=legacy", LegacyPutProvider), ("put=fast", LocalProvider)]:
                provider = cls(str(args.model_path), max_new_prefill_per_iter=max(args.baseline_width, args.candidate_width))
                _reuse_loaded_artifacts(provider, base_provider)
                samples[key].append(
                    await _run_provider_once(provider, prompts, max_tokens=args.provider_max_tokens, stagger=args.provider_stagger)
                )
        result = {
            key: {
                "makespan": statistics.median(item["makespan"] for item in value),
                "avg_ttft": statistics.median(item["avg_ttft"] for item in value),
                "p95_ttft": statistics.median(item["p95_ttft"] for item in value),
                "chars_per_s": statistics.median(item["chars_per_s"] for item in value),
            }
            for key, value in samples.items()
        }
        _print_provider_result("put=legacy", result["put=legacy"])
        _print_provider_result("put=fast", result["put=fast"])
        makespan_gain = result["put=legacy"]["makespan"] / result["put=fast"]["makespan"]
        ttft_gain = result["put=legacy"]["avg_ttft"] / result["put=fast"]["avg_ttft"]
        throughput_gain = result["put=fast"]["chars_per_s"] / result["put=legacy"]["chars_per_s"]
    elif args.compare_cache_reuse:
        base_provider = LocalProvider(str(args.model_path), max_new_prefill_per_iter=max(args.baseline_width, args.candidate_width))
        base_provider.load()
        prompts = [
            "请用三点总结 continuous batching 的核心收益。",
            "Why does token streaming latency matter for short requests?",
            "请简要解释 TTFT 和吞吐量之间的关系。",
            "List two practical risks when increasing concurrency blindly.",
        ]
        samples = {"reuse=off": [], "reuse=on": []}
        for _ in range(args.provider_repeats):
            for key, cls in [("reuse=off", NoReuseBatchProvider), ("reuse=on", LocalProvider)]:
                provider = cls(str(args.model_path), max_new_prefill_per_iter=max(args.baseline_width, args.candidate_width))
                _reuse_loaded_artifacts(provider, base_provider)
                samples[key].append(
                    await _run_provider_once(provider, prompts, max_tokens=args.provider_max_tokens, stagger=args.provider_stagger)
                )
        result = {
            key: {
                "makespan": statistics.median(item["makespan"] for item in value),
                "avg_ttft": statistics.median(item["avg_ttft"] for item in value),
                "p95_ttft": statistics.median(item["p95_ttft"] for item in value),
                "chars_per_s": statistics.median(item["chars_per_s"] for item in value),
            }
            for key, value in samples.items()
        }
        _print_provider_result("reuse=off", result["reuse=off"])
        _print_provider_result("reuse=on", result["reuse=on"])
        makespan_gain = result["reuse=off"]["makespan"] / result["reuse=on"]["makespan"]
        ttft_gain = result["reuse=off"]["avg_ttft"] / result["reuse=on"]["avg_ttft"]
        throughput_gain = result["reuse=on"]["chars_per_s"] / result["reuse=off"]["chars_per_s"]
    else:
        result = await benchmark_provider(
            args.model_path,
            repeats=args.provider_repeats,
            max_tokens=args.provider_max_tokens,
            stagger=args.provider_stagger,
            prefill_widths=[args.baseline_width, args.candidate_width],
            compare_decode_modes=args.compare_decode_modes,
        )
    if args.compare_decode_modes:
        _print_provider_result("decode=single", result["single"])
        _print_provider_result("decode=batched", result["batched"])
        makespan_gain = result["single"]["makespan"] / result["batched"]["makespan"]
        ttft_gain = result["single"]["avg_ttft"] / result["batched"]["avg_ttft"]
        throughput_gain = result["batched"]["chars_per_s"] / result["single"]["chars_per_s"]
    elif not args.compare_cache_reuse and not args.compare_put_mode and not args.compare_prefill_batch and not args.compare_engine_mode and not args.compare_executor_mode and not args.compare_system_cache and not args.compare_warmup:
        _print_provider_result(f"prefill={args.baseline_width}", result[args.baseline_width])
        _print_provider_result(f"prefill={args.candidate_width}", result[args.candidate_width])
        makespan_gain = result[args.baseline_width]["makespan"] / result[args.candidate_width]["makespan"]
        ttft_gain = result[args.baseline_width]["avg_ttft"] / result[args.candidate_width]["avg_ttft"]
        throughput_gain = result[args.candidate_width]["chars_per_s"] / result[args.baseline_width]["chars_per_s"]

    print("\n== Relative Improvement ==")
    print(f"makespan speedup : {makespan_gain:.2f}x")
    print(f"avg TTFT speedup : {ttft_gain:.2f}x")
    print(f"throughput gain  : {throughput_gain:.2f}x")


if __name__ == "__main__":
    asyncio.run(main())
