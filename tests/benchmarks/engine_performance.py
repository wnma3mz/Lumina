import time
import argparse
import json
from pathlib import Path
import numpy as np
from tests.benchmarks.utils import setup_benchmark_logging, get_mem_stats, clear_mlx_cache

logger = setup_benchmark_logging()

def run_performance_test(model_id, mode, max_tokens=32):
    import mlx.core as mx
    import mlx.utils as mx_utils
    from mlx_lm import load as mlx_load

    logger.info(f"--- Performance Test: {model_id} | Mode: {mode} ---")
    clear_mlx_cache()
    
    # 1. Load Model
    lazy_flag = (mode != "baseline")
    try:
        model, tokenizer = mlx_load(model_id, lazy=lazy_flag)
    except Exception as e:
        logger.error(f"Failed to load: {e}")
        return {"error": str(e)}
    
    if mode == "baseline":
        logger.info("Eager loading all parameters...")
        mx.eval(model.parameters())
    elif mode == "hybrid":
        logger.info("Hybrid loading: offloading L2 components (Towers/Embed)...")
        all_params = mx_utils.tree_flatten(model.parameters())
        to_eval = [p for name, p in all_params if not any(k in name.lower() for k in ["visual", "vision", "audio", "embed_tokens"])]
        mx.eval(to_eval)
        
    initial_rss, initial_metal = get_mem_stats()
    
    # 2. Precise TTFT/TPOT Measurement
    prompt = "Explain why local LLMs are secure."
    tokens = mx.array(tokenizer.encode(prompt))[None]
    embed_layer = getattr(model, "embed_tokens", getattr(getattr(model, "model", None), "embed_tokens", None))
    
    ttft = 0
    tpot_list = []
    curr_tokens = tokens
    
    start_gen = time.perf_counter()
    prev_time = start_gen
    
    try:
        for i in range(max_tokens):
            if mode != "baseline" and embed_layer and i == 0:
                with mx.stream(mx.cpu):
                    x = embed_layer(curr_tokens)
                    mx.eval(x)
                logits = model(x)
            else:
                logits = model(curr_tokens)
            
            mx.eval(logits)
            
            curr_time = time.perf_counter()
            delta = curr_time - prev_time
            if i == 0: ttft = delta
            else: tpot_list.append(delta)
            
            curr_tokens = mx.array([[100]]) # Mock token
            prev_time = curr_time
            
        final_rss, final_metal = get_mem_stats()
        avg_tpot = np.mean(tpot_list) if tpot_list else 0
        
        result = {
            "model": model_id,
            "mode": mode,
            "ttft_ms": round(ttft * 1000, 2),
            "tpot_ms": round(avg_tpot * 1000, 2),
            "mem_metal_mb": round(final_metal, 2),
            "load_time_saved": (mode != "baseline")
        }
        logger.info(f"DONE: TTFT={result['ttft_ms']}ms, TPOT={result['tpot_ms']}ms, Metal={result['mem_metal_mb']}MB")
        return result
    except Exception as e:
        logger.error(f"Inference error: {e}")
        return {"error": str(e)}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", choices=["baseline", "optimized", "hybrid"], default="hybrid")
    args = parser.parse_args()
    
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    
    res = run_performance_test(args.model, args.mode)
    print(f"\nMETRICS_JSON:{json.dumps(res)}")
