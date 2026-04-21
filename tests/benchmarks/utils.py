import time
import os
import psutil
import gc
import logging
from pathlib import Path

def setup_benchmark_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S'
    )
    return logging.getLogger("benchmark")

def get_mem_stats():
    import mlx.core as mx
    # Support both old and new MLX memory APIs
    rss = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    try:
        metal = mx.get_active_memory() / (1024 * 1024)
    except AttributeError:
        metal = mx.metal.get_active_memory() / (1024 * 1024)
    return rss, metal

def clear_mlx_cache():
    import mlx.core as mx
    gc.collect()
    try:
        mx.clear_cache()
    except AttributeError:
        mx.metal.clear_cache()
