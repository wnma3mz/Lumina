"""
Lumina HTTP-level 推理基准测试

测试路径：HTTP /v1/chat/completions (streaming)
与真实用户路径完全一致，覆盖：
  - 纯文本推理 (text-only)
  - 图片+文本推理 (vision)

用法：
  # 确保 Lumina 服务已启动 (uv run lumina server)
  uv run python tests/benchmarks/http_bench.py
  uv run python tests/benchmarks/http_bench.py --url http://127.0.0.1:31821 --rounds 5
  uv run python tests/benchmarks/http_bench.py --skip-vision   # 仅文本
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import sys
import time
from pathlib import Path
from typing import Optional

root = Path(__file__).resolve().parents[2]
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

try:
    import aiohttp
except ImportError:
    print("ERROR: aiohttp 未安装。运行: uv add aiohttp --dev")
    sys.exit(1)

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


# ── 工具函数 ───────────────────────────────────────────────────────────────────

def _make_test_image_data_url(size: int = 64, color=(100, 149, 237)) -> Optional[str]:
    if not _PIL_AVAILABLE:
        return None
    img = Image.new("RGB", (size, size), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


async def _stream_completion(
    session: "aiohttp.ClientSession",
    base_url: str,
    messages: list,
    max_tokens: int = 32,
) -> dict:
    """发起一次 SSE 流式请求，返回 TTFT / TPOT / tokens / text。"""
    payload = {
        "model": "local",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": True,
        "temperature": 0.0,
    }
    t0 = time.perf_counter()
    ttft_ms: Optional[float] = None
    token_intervals: list[float] = []
    text = ""
    prev = t0

    try:
        async with session.post(
            f"{base_url}/v1/chat/completions",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                return {"error": f"HTTP {resp.status}: {body[:200]}"}
            async for raw_line in resp.content:
                line = raw_line.decode().strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                try:
                    chunk = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if not delta:
                    continue
                now = time.perf_counter()
                if ttft_ms is None:
                    ttft_ms = (now - t0) * 1000
                else:
                    token_intervals.append((now - prev) * 1000)
                text += delta
                prev = now
    except Exception as e:
        return {"error": str(e)}

    tpot_ms = sum(token_intervals) / len(token_intervals) if token_intervals else 0.0
    n_tokens = len(token_intervals) + (1 if ttft_ms is not None else 0)
    return {
        "ttft_ms": round(ttft_ms or 0, 1),
        "tpot_ms": round(tpot_ms, 1),
        "n_tokens": n_tokens,
        "text": text,
    }


# ── 单场景测试 ─────────────────────────────────────────────────────────────────

async def bench_text(base_url: str, rounds: int, max_tokens: int = 32) -> dict:
    messages = [{"role": "user", "content": "Explain why local LLMs are secure."}]
    results = []
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        # 预热一次，不计入统计
        await _stream_completion(session, base_url, messages, max_tokens)
        for _ in range(rounds):
            r = await _stream_completion(session, base_url, messages, max_tokens)
            if "error" not in r:
                results.append(r)
    if not results:
        return {"error": "all rounds failed"}
    return {
        "ttft_ms": round(sum(r["ttft_ms"] for r in results) / len(results), 1),
        "tpot_ms": round(sum(r["tpot_ms"] for r in results) / len(results), 1),
        "ttft_min_ms": round(min(r["ttft_ms"] for r in results), 1),
        "ttft_max_ms": round(max(r["ttft_ms"] for r in results), 1),
        "n_tokens": results[0]["n_tokens"],
        "rounds": len(results),
        "sample_text": results[0]["text"][:60],
    }


async def bench_vision(base_url: str, rounds: int, max_tokens: int = 16) -> dict:
    data_url = _make_test_image_data_url()
    if data_url is None:
        return {"error": "Pillow 未安装，跳过图片测试"}
    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": "What color is this image? One word."},
        ],
    }]
    results = []
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        # 预热（Vision Encoder 换入）
        await _stream_completion(session, base_url, messages, max_tokens)
        for _ in range(rounds):
            r = await _stream_completion(session, base_url, messages, max_tokens)
            if "error" not in r:
                results.append(r)
    if not results:
        return {"error": "all rounds failed"}
    return {
        "ttft_ms": round(sum(r["ttft_ms"] for r in results) / len(results), 1),
        "tpot_ms": round(sum(r["tpot_ms"] for r in results) / len(results), 1),
        "ttft_min_ms": round(min(r["ttft_ms"] for r in results), 1),
        "ttft_max_ms": round(max(r["ttft_ms"] for r in results), 1),
        "rounds": len(results),
        "sample_text": results[0]["text"][:40],
    }


async def bench_concurrent(base_url: str, concurrency: int, max_tokens: int = 64) -> dict:
    """并发请求吞吐量测试。"""
    messages = [{"role": "user", "content": "Write a short paragraph about AI privacy."}]
    connector = aiohttp.TCPConnector(ssl=False, limit=concurrency + 2)
    async with aiohttp.ClientSession(connector=connector) as session:
        t0 = time.perf_counter()
        tasks = [
            _stream_completion(session, base_url, messages, max_tokens)
            for _ in range(concurrency)
        ]
        results = await asyncio.gather(*tasks)
        total_s = time.perf_counter() - t0

    ok = [r for r in results if "error" not in r]
    if not ok:
        return {"error": "all concurrent requests failed"}
    total_tokens = sum(r["n_tokens"] for r in ok)
    return {
        "concurrency": concurrency,
        "ok_requests": len(ok),
        "total_tokens": total_tokens,
        "total_s": round(total_s, 2),
        "tokens_per_s": round(total_tokens / total_s, 1),
        "avg_ttft_ms": round(sum(r["ttft_ms"] for r in ok) / len(ok), 1),
    }


# ── 打印 ───────────────────────────────────────────────────────────────────────

def _print_section(title: str):
    print(f"\n{'─' * 56}")
    print(f"  {title}")
    print(f"{'─' * 56}")


def _print_result(label: str, r: dict):
    if "error" in r:
        print(f"  {label}: ERROR — {r['error']}")
        return
    if "tokens_per_s" in r:
        print(
            f"  {label}: {r['tokens_per_s']} tok/s  "
            f"avg_TTFT={r['avg_ttft_ms']}ms  "
            f"({r['ok_requests']}/{r['concurrency']} ok, {r['total_s']}s)"
        )
    else:
        print(
            f"  {label}: TTFT={r['ttft_ms']}ms "
            f"[{r.get('ttft_min_ms','?')}-{r.get('ttft_max_ms','?')}]  "
            f"TPOT={r['tpot_ms']}ms  "
            f"({r.get('rounds', '?')} rounds)"
        )
        if r.get("sample_text"):
            print(f"    → {repr(r['sample_text'])}")


def _as_metrics_json(text_r, vision_r, concur_r):
    return {
        "text": text_r,
        "vision": vision_r,
        "concurrent": concur_r,
    }


# ── main ───────────────────────────────────────────────────────────────────────

async def main(base_url: str, rounds: int, skip_vision: bool, skip_concurrent: bool):
    # 健康检查
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as s:
            async with s.get(f"{base_url}/health", timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200:
                    print(f"ERROR: /health 返回 {r.status}，请先启动服务")
                    return
                health = await r.json()
                if not health.get("llm_loaded"):
                    print("ERROR: LLM 尚未加载完成，请稍后重试")
                    return
    except Exception as e:
        print(f"ERROR: 无法连接 {base_url} — {e}")
        return

    print(f"\nLumina Benchmark  base_url={base_url}  rounds={rounds}")

    _print_section("纯文本推理 (text-only)")
    text_r = await bench_text(base_url, rounds)
    _print_result("hybrid mode", text_r)

    vision_r: dict = {"skipped": True}
    if not skip_vision:
        _print_section("图片+文本推理 (vision, offload cold→warm)")
        # 冷启动（Vision Encoder 换入）
        cold_r = await bench_vision(base_url, rounds=1)
        _print_result("首次(冷)", cold_r)
        # 热测
        warm_r = await bench_vision(base_url, rounds)
        _print_result("热跑均值", warm_r)
        vision_r = {"cold": cold_r, "warm": warm_r}

    concur_r: dict = {"skipped": True}
    if not skip_concurrent:
        _print_section("并发吞吐 (concurrency=4)")
        concur_r = await bench_concurrent(base_url, concurrency=4)
        _print_result("4 并发", concur_r)

    print(f"\nMETRICS_JSON:{json.dumps(_as_metrics_json(text_r, vision_r, concur_r))}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lumina HTTP benchmark")
    parser.add_argument("--url", default="http://127.0.0.1:31821", help="Lumina 服务地址")
    parser.add_argument("--rounds", type=int, default=3, help="热跑轮数（预热不计入）")
    parser.add_argument("--skip-vision", action="store_true", help="跳过图片测试")
    parser.add_argument("--skip-concurrent", action="store_true", help="跳过并发测试")
    args = parser.parse_args()
    asyncio.run(main(args.url, args.rounds, args.skip_vision, args.skip_concurrent))
