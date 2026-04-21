"""
[已废弃] 直接调用 mlx_lm 的低层推理测试。

此脚本绕开了 LocalProvider、VLM 分发、chat template 等关键路径，
对 VLM 模型（如 Qwen3.5、Gemma 4）的测量结果不具代表性。

请使用 http_bench.py 替代：
  uv run python tests/benchmarks/http_bench.py

保留此文件仅用于历史对比，不作为正式基准数据来源。
"""
import argparse
import json
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:  # noqa: E402
    sys.path.insert(0, str(root))

import numpy as np  # noqa: E402
from tests.benchmarks.utils import setup_benchmark_logging, get_mem_stats, clear_mlx_cache  # noqa: E402

logger = setup_benchmark_logging()


def run_performance_test(model_id, mode, max_tokens=32):
    import mlx.core as mx
    import mlx.utils as mx_utils
    from mlx_lm import load as mlx_load

    logger.warning("engine_performance.py 已废弃，建议改用 http_bench.py")
    logger.info(f"--- Performance Test: {model_id} | Mode: {mode} ---")
    clear_mlx_cache()

    try:
        model, tokenizer = mlx_load(model_id, lazy=(mode != "baseline"))
    except Exception as e:
        logger.error(f"Failed to load: {e}")
        return {"error": str(e)}

    if mode == "baseline":
        logger.info("Eager loading all parameters...")
        mx.eval(model.parameters())
    elif mode == "hybrid":
        logger.info("Hybrid loading: offloading L2 components (Towers/Embed)...")
        all_params = mx_utils.tree_flatten(model.parameters())
        to_eval = [
            p for name, p in all_params
            if not any(k in name.lower() for k in ["visual", "vision", "audio", "embed_tokens"])
        ]
        mx.eval(to_eval)

    initial_rss, initial_metal = get_mem_stats()

    prompt = "Explain why local LLMs are secure."
    tokens = mx.array(tokenizer.encode(prompt))[None]

    ttft = 0
    tpot_list = []
    curr_tokens = tokens
    start_gen = time.perf_counter()
    prev_time = start_gen

    try:
        for i in range(max_tokens):
            out = model(curr_tokens)
            # mlx_vlm 返回 LanguageModelOutput，需要解包
            logits = getattr(out, "logits", out)
            mx.eval(logits)
            curr_time = time.perf_counter()
            delta = curr_time - prev_time
            if i == 0:
                ttft = delta
            else:
                tpot_list.append(delta)
            curr_tokens = mx.array([[100]])
            prev_time = curr_time

        final_rss, final_metal = get_mem_stats()
        avg_tpot = np.mean(tpot_list) if tpot_list else 0

        result = {
            "model": model_id,
            "mode": mode,
            "ttft_ms": round(ttft * 1000, 2),
            "tpot_ms": round(avg_tpot * 1000, 2),
            "mem_metal_mb": round(final_metal, 2),
        }
        logger.info(
            f"DONE: TTFT={result['ttft_ms']}ms, TPOT={result['tpot_ms']}ms, "
            f"Metal={result['mem_metal_mb']}MB"
        )
        return result
    except Exception as e:
        logger.error(f"Inference error: {e}")
        return {"error": str(e)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="[废弃] 低层推理测试，建议改用 http_bench.py")
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", choices=["baseline", "hybrid"], default="hybrid")
    args = parser.parse_args()

    res = run_performance_test(args.model, args.mode)
    print(f"\nMETRICS_JSON:{json.dumps(res)}")
