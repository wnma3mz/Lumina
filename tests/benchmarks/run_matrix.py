import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Add project root to sys.path to allow absolute imports of tests.*
root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from tests.benchmarks.utils import setup_benchmark_logging, print_http_matrix_table  # noqa: E402

logger = setup_benchmark_logging()


DEFAULT_MODELS = [
    "mlx-community/Qwen3.5-0.8B-4bit",
    "mlx-community/gemma-4-e2b-it-4bit",
]
DEFAULT_MODES = ["baseline", "hybrid"]
_LOCAL_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run_cmd(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
    if result.returncode != 0:
        logger.error(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
        return None, result.stderr

    for line in result.stdout.split("\n"):
        if line.startswith("METRICS_JSON:"):
            return json.loads(line.replace("METRICS_JSON:", "")), None
    return None, result.stdout


def _wait_for_server(base_url: str, proc: subprocess.Popen, timeout_s: int = 600) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Server exited early with code {proc.returncode}")
        try:
            with _LOCAL_NO_PROXY.open(f"{base_url}/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except urllib.error.URLError:
            time.sleep(1)
        except Exception:
            time.sleep(1)
    raise TimeoutError(f"Timed out waiting for {base_url}/health")


def _write_benchmark_config(model: str, mode: str, port: int) -> Path:
    template_path = root / "lumina" / "config.json"
    data = json.loads(template_path.read_text(encoding="utf-8"))
    offload = mode == "hybrid"

    data["provider"]["type"] = "local"
    data["provider"]["model_path"] = model
    data["provider"]["mlx_memory"] = {
        "offload_embedding": offload,
        "offload_vision": offload,
        "offload_audio": offload,
    }
    data["system"]["server"]["host"] = "127.0.0.1"
    data["system"]["server"]["port"] = port
    data["system"]["server"]["log_level"] = "INFO"
    data["system"]["desktop"]["menubar_enabled"] = False
    data["system"]["request_history"]["enabled"] = False
    data["digest"]["enabled"] = False
    data["audio"]["enabled"] = False
    data["audio"]["ptt"]["enabled"] = False

    fd, path_str = tempfile.mkstemp(prefix=f"lumina-bench-{mode}-", suffix=".json")
    path = Path(path_str)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return path


def _stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _bench_one(
    model: str,
    mode: str,
    *,
    port: int,
    rounds: int,
    skip_vision: bool,
    skip_concurrent: bool,
    startup_timeout: int,
) -> dict:
    config_path = _write_benchmark_config(model, mode, port)
    log_file = tempfile.NamedTemporaryFile(prefix=f"lumina-bench-{port}-", suffix=".log", delete=False)
    log_path = Path(log_file.name)
    log_file.close()
    base_url = f"http://127.0.0.1:{port}"
    proc = None
    try:
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "lumina/main.py",
                    "server",
                    "--config",
                    str(config_path),
                    "--no-menubar",
                    "--port",
                    str(port),
                    "--log-level",
                    "INFO",
                ],
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
        _wait_for_server(base_url, proc, timeout_s=startup_timeout)
        cmd = [
            sys.executable,
            "tests/benchmarks/http_bench.py",
            "--url",
            base_url,
            "--rounds",
            str(rounds),
        ]
        if skip_vision:
            cmd.append("--skip-vision")
        if skip_concurrent:
            cmd.append("--skip-concurrent")
        metrics, error_text = run_cmd(cmd)
        if metrics is None:
            return {"model": model, "mode": mode, "error": error_text or "benchmark output missing"}

        text = metrics.get("text", {}) or {}
        vision = metrics.get("vision", {}) or {}
        warm = vision.get("warm", {}) if isinstance(vision, dict) else {}
        cold = vision.get("cold", {}) if isinstance(vision, dict) else {}
        concurrent = metrics.get("concurrent", {}) or {}
        return {
            "model": model,
            "mode": mode,
            "text_ttft_ms": text.get("ttft_ms"),
            "text_tpot_ms": text.get("tpot_ms"),
            "vision_cold_ttft_ms": cold.get("ttft_ms") if isinstance(cold, dict) and "error" not in cold else None,
            "vision_warm_ttft_ms": warm.get("ttft_ms") if isinstance(warm, dict) and "error" not in warm else None,
            "concurrent_tok_s": concurrent.get("tokens_per_s") if isinstance(concurrent, dict) and "error" not in concurrent else None,
        }
    except Exception as exc:
        log_excerpt = ""
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if lines:
                log_excerpt = "\n".join(lines[-20:])
        return {
            "model": model,
            "mode": mode,
            "error": f"{type(exc).__name__}: {exc}\n{log_excerpt}".strip(),
        }
    finally:
        if proc is not None:
            _stop_server(proc)
        config_path.unlink(missing_ok=True)
        log_path.unlink(missing_ok=True)


def _pick_port(base_port: int, offset: int) -> int:
    port = base_port + offset
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"Benchmark port {port} is already in use")
    return port


def main():
    parser = argparse.ArgumentParser(description="Lumina HTTP benchmark matrix")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--modes", nargs="+", default=DEFAULT_MODES, choices=DEFAULT_MODES)
    parser.add_argument("--base-port", type=int, default=31900)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--startup-timeout", type=int, default=600)
    parser.add_argument("--skip-vision", action="store_true")
    parser.add_argument("--skip-concurrent", action="store_true")
    args = parser.parse_args()

    all_results = []

    logger.info("🧪 Starting Automated HTTP Benchmark Matrix...")

    index = 0
    for model in args.models:
        for mode in args.modes:
            port = _pick_port(args.base_port, index)
            index += 1
            logger.info(f"Running: {model} in {mode} mode on port {port}")
            res = _bench_one(
                model,
                mode,
                port=port,
                rounds=args.rounds,
                skip_vision=args.skip_vision,
                skip_concurrent=args.skip_concurrent,
                startup_timeout=args.startup_timeout,
            )
            if res:
                all_results.append(res)

    logger.info("📊 FINAL SUMMARY REPORT")
    print_http_matrix_table(all_results)
    print(f"METRICS_JSON:{json.dumps(all_results)}")

if __name__ == "__main__":
    main()
