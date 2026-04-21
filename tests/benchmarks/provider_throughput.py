import time
import argparse
import json
import asyncio
from pathlib import Path
from tests.benchmarks.utils import setup_benchmark_logging, get_mem_stats, clear_mlx_cache

logger = setup_benchmark_logging()

async def run_throughput_test(model_id, mode, concurrency=4):
    from lumina.providers.local import LocalProvider
    
    logger.info(f"--- Throughput Test: {model_id} | Mode: {mode} | Concurrency: {concurrency} ---")
    clear_mlx_cache()
    
    # 1. Initialize Provider with specific mode
    lazy = (mode == "optimized")
    offload = (mode != "baseline")
    
    provider = LocalProvider(
        model_path=model_id,
        lazy_load=lazy,
        offload_embedding=offload,
        offload_vision=offload,
        offload_audio=offload
    )
    
    start_load = time.perf_counter()
    provider.load()
    load_time = time.perf_counter() - start_load
    
    rss, metal = get_mem_stats()
    logger.info(f"Model Loaded in {load_time:.2f}s. Metal Mem: {metal:.2f}MB")

    # 2. Concurrent Requests
    prompt = "Write a 200-word essay about the future of local AI."
    
    async def task(tid):
        t_start = time.perf_counter()
        char_count = 0
        async for chunk in provider.generate_stream(prompt, max_tokens=150):
            if chunk.get('text'):
                char_count += len(chunk['text'])
        t_end = time.perf_counter()
        return {
            "id": tid,
            "duration": t_end - t_start,
            "chars": char_count,
            "cps": char_count / (t_end - t_start)
        }

    logger.info(f"Starting {concurrency} concurrent requests...")
    start_all = time.perf_counter()
    results = await asyncio.gather(*(task(i) for i in range(concurrency)))
    total_time = time.perf_counter() - start_all
    
    total_chars = sum(r['chars'] for r in results)
    avg_cps = total_chars / total_time
    
    final_rss, final_metal = get_mem_stats()
    
    report = {
        "model": model_id,
        "mode": mode,
        "concurrency": concurrency,
        "total_time_s": round(total_time, 2),
        "total_chars": total_chars,
        "avg_cps": round(avg_cps, 2),
        "peak_metal_mb": round(final_metal, 2)
    }
    
    logger.info(f"DONE: Total Time={report['total_time_s']}s, Avg Speed={report['avg_cps']} chars/s")
    return report

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", choices=["baseline", "optimized", "hybrid"], default="hybrid")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    
    # Add project root to path for imports
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    
    asyncio.run(run_throughput_test(args.model, args.mode, args.concurrency))
