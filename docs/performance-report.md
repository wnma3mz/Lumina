# Lumina 性能基准测试报告

本报告记录 Lumina 推理引擎在 Apple Silicon 上的性能表现，重点验证 **L1/L2 显存分层技术 (Hybrid Offloading)** 的实际效果。

---

## 1. 测试方法

### 测试路径

所有指标均通过 **HTTP `/v1/chat/completions` (SSE 流式)** 采集，与真实用户请求路径完全一致：

```
HTTP 客户端 → FastAPI → LocalProvider → mlx_vlm (VLM) / mlx_lm (LM) → Metal GPU
```

旧版 `engine_performance.py` 直接调用 `mlx_lm.load` + 裸 `model()` 推理，绕开了 VLM 分发、chat template、KV cache 管理等关键路径，数据仅供参考。新脚本 `http_bench.py` 替代了它。

### 测试脚本

```bash
# 启动服务（另开终端）
uv run lumina server

# 运行基准测试
uv run python tests/benchmarks/http_bench.py
uv run python tests/benchmarks/http_bench.py --rounds 5 --skip-vision
```

### 指标定义

| 指标 | 说明 |
| :--- | :--- |
| **TTFT** | Time to First Token，从发送请求到收到第一个 token 的延迟（ms）。含 prefill、VLM 图像编码等全部开销。 |
| **TPOT** | Time Per Output Token，decode 阶段每个 token 的平均耗时（ms）。直接反映 GPU 吞吐。 |
| **tok/s** | 并发场景下，所有请求的总 token 数除以总耗时，衡量系统整体吞吐。 |

### 测试环境

- **硬件**: Apple Silicon Mac (M-series)
- **软件**: Lumina v0.8.5, mlx_vlm 0.4.4, mlx_lm 0.31.x, macOS 14+
- **加载模式**: Hybrid（默认）— `offload_embedding=true`, `offload_vision=true`, `offload_audio=true`
- **测试方式**: 每项热跑 4 轮取均值，首轮预热不计入统计

---

## 2. 测试结果：Qwen3.5-0.8B-4bit（当前默认模型）

### 2.1 纯文本推理（text-only）

| 模式 | TTFT 均值 | TTFT 区间 | TPOT | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| Hybrid（默认） | **43ms** | 36–52ms | **7.7ms** | offload_embedding/vision/audio=true |

TTFT 区间反映同一 system prompt 下的 KV cache 命中情况（hit 时更低）。TPOT 7.7ms ≈ **130 tok/s** decode 速度，对 0.8B 4-bit 模型属正常水平。

### 2.2 图片+文本推理（vision）

Vision Encoder 在 offload_vision=true 时留在磁盘，首次调用需换入内存：

| 轮次 | TTFT | 说明 |
| :--- | :--- | :--- |
| 冷启动（首次） | **122ms** | Vision Encoder 从磁盘 mmap 换入 Metal |
| 热跑均值 | **119ms** | Vision Encoder 已在 Metal cache，接近全量加载水平 |

VLM 推理路径修复说明（v0.8.5+）：
- **正确的 cache 结构**：Qwen3.5 是 Mamba-Hybrid 架构，需要混合 `ArraysCache`/`KVCache`。此前误用 `mlx_lm.make_prompt_cache` 生成的全 `KVCache` 结构，导致 `'KVCache' object is not subscriptable`。现在通过 `model.language_model.make_cache()` 获得正确结构。
- **`LanguageModelOutput` 解包**：mlx_vlm 的 `LanguageModel.__call__` 返回 `LanguageModelOutput(logits=...)` 而非裸 array，所有推理调用点已通过 `_extract_logits()` 统一解包。

### 2.3 并发吞吐（concurrency=4）

| 并发数 | 总 tok/s | 平均 TTFT | 完成率 |
| :--- | :--- | :--- | :--- |
| 4 | **275 tok/s** | 73ms | 4/4 |

4 路并发下总吞吐 275 tok/s，平均 TTFT 73ms（vs 单路 43ms），并发开销约 70%——符合单 GPU 串行 decode 的预期（batch size=1 的物理限制）。

---

## 3. 显存分层效果

### Hybrid vs Baseline 对比（Qwen3.5-0.8B）

旧 `engine_performance.py`（非 HTTP 路径，仅供参考）：

| 模式 | TTFT | TPOT | Metal 显存 |
| :--- | :--- | :--- | :--- |
| Baseline（全量加载） | ~65ms | ~7.9ms | 405MB |
| Hybrid（offload） | ~57ms | ~7.5ms | 405MB |

Qwen3.5-0.8B 极小（0.8B 4-bit ≈ 450MB），Embedding 层占比低，Hybrid 模式下显存节省不明显。**对 2B+ 模型效果更显著**（见下节）。

### 预期效果（按模型规模）

| 模型规模 | offload_embedding | offload_vision | 预期显存节省 | TTFT 增量 |
| :--- | :--- | :--- | :--- | :--- |
| 0.8B（如 Qwen3.5） | 可忽略 | 可忽略 | < 50MB | < 10ms |
| 2B（如 Gemma 4 2B） | ~200MB | ~500MB | ~700MB | 100–300ms（冷） |
| 7B+ | ~500MB | ~1GB+ | 1–2GB | 200–500ms（冷） |

---

## 4. 已知问题与局限

**vision TPOT 测试不准确**：当前视觉测试用 prompt "What color is this image?" 只回复一个词，decode 步数为 0–1，无法统计有效 TPOT。如需测量 VLM decode 速度，需换用需要长回复的 prompt（待改进）。

**Metal 显存计数**：`mx.get_active_memory()` 返回当前活跃显存，不含 Metal driver 保留区域。真实峰值可通过 Instruments / `sudo powermetrics` 观察。

---

## 5. 如何复现

```bash
# 确保服务运行中
uv run lumina server

# 完整基准（另开终端）
uv run python tests/benchmarks/http_bench.py --rounds 4

# 仅文本，跳过视觉和并发
uv run python tests/benchmarks/http_bench.py --skip-vision --skip-concurrent

# 指定非默认地址
uv run python tests/benchmarks/http_bench.py --url http://127.0.0.1:31821 --rounds 5
```

---

*最后更新: 2026-04-21 | 测试模型: Qwen3.5-0.8B-4bit (mlx_vlm hybrid mode)*
