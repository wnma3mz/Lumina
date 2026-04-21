import subprocess
import json
import sys
from pathlib import Path

# Add project root to sys.path to allow absolute imports of tests.*
root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from tests.benchmarks.utils import setup_benchmark_logging, print_benchmark_table  # noqa: E402

logger = setup_benchmark_logging()

def run_cmd(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
        return None
    
    # Extract JSON metrics from output
    for line in result.stdout.split('\n'):
        if line.startswith("METRICS_JSON:"):
            return json.loads(line.replace("METRICS_JSON:", ""))
    return None

def main():
    models = [
        "mlx-community/Qwen3.5-0.8B-4bit",
        "mlx-community/gemma-4-e2b-it-4bit"
    ]
    modes = ["baseline", "hybrid"] 
    
    all_results = []
    
    logger.info("🧪 Starting Automated Benchmark Matrix...")
    
    for model in models:
        for mode in modes:
            logger.info(f"Running: {model} in {mode} mode")
            cmd = [sys.executable, "tests/benchmarks/engine_performance.py", "--model", model, "--mode", mode]
            res = run_cmd(cmd)
            if res:
                all_results.append(res)
    
    logger.info("📊 FINAL SUMMARY REPORT")
    print_benchmark_table(all_results)

if __name__ == "__main__":
    main()
