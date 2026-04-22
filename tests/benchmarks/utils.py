import os
import psutil
import gc
import logging

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

def print_benchmark_table(results: list[dict]):
    """将测试结果打印为漂亮的 Markdown 表格。"""
    if not results:
        return
    
    headers = ["Model", "Mode", "TTFT (ms)", "TPOT (ms)", "Peak Metal (MB)", "Status"]
    rows = []
    for r in results:
        rows.append([
            r.get("model", "N/A").split("/")[-1],
            r.get("mode", "N/A"),
            f"{r.get('ttft_ms', 0):.1f}",
            f"{r.get('tpot_ms', 0):.1f}",
            f"{r.get('mem_metal_mb', 0):.0f}",
            "✅" if not r.get("error") else "❌"
        ])
    
    # Simple table formatting
    col_widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    
    def fmt_row(data):
        return "| " + " | ".join(str(val).ljust(width) for val, width in zip(data, col_widths)) + " |"

    print("\n" + fmt_row(headers))
    print("| " + " | ".join("-" * w for w in col_widths) + " |")
    for row in rows:
        print(fmt_row(row))
    print("\n")
