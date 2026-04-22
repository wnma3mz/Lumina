# Lumina 性能基准测试报告

本报告记录 Lumina 在 Apple Silicon 上的 HTTP 端到端推理表现，重点看三件事：文本首字延迟、视觉链路开销，以及并发吞吐。

---

## 1. 测试口径

所有指标都通过 **HTTP `/v1/chat/completions` (SSE 流式)** 采集，走真实用户路径：

```
HTTP 客户端 → FastAPI → LocalProvider → mlx_vlm (VLM) / mlx_lm (LM) → Metal GPU
```

旧版 `engine_performance.py` 已不再作为主报告口径；当前以 `http_bench.py` / `run_matrix.py` 的 HTTP 路径结果为准。

### 测试命令

```bash
# 启动服务（另开终端）
uv run lumina server

# 运行基准测试
uv run python tests/benchmarks/http_bench.py
uv run python tests/benchmarks/http_bench.py --rounds 5 --skip-vision
```

### 固定输入

这里不用公开数据集，而是统一使用脚本内固定构造的最小可复现实验输入。后续即使补充多套硬件环境，也尽量保持这一组输入不变，方便横向比较：

- **text-only**：单轮用户消息 `"Explain why local LLMs are secure."`
- **vision**：脚本运行时用 Pillow 生成一张 **64x64 纯色 RGB PNG**，颜色为 `(100, 149, 237)`，再以内联 `data:` URL 发给 `/v1/chat/completions`
- **vision 文本提示**：`"What color is this image? One word."`
- **concurrency=4**：4 个并发请求共享同一文本 prompt：`"Write a short paragraph about AI privacy."`

这组视觉测试更接近“最小图片理解回路”和 Vision Encoder 冷热启动开销，不是复杂图片理解 benchmark。

### 指标

| 指标 | 说明 |
| :--- | :--- |
| **TTFT** | Time to First Token，从发送请求到收到第一个 token 的延迟（ms）。含 prefill、VLM 图像编码等全部开销。 |
| **TPOT** | Time Per Output Token，decode 阶段每个 token 的平均耗时（ms）。直接反映 GPU 吞吐。 |
| **tok/s** | 并发场景下，所有请求的总 token 数除以总耗时，衡量系统整体吞吐。 |

### 测试环境

本报告允许记录多套硬件环境，但默认要求测试数据保持同一套脚本输入。

通用约束：

- **软件**: Lumina v0.8.5
- **加载模式**: 本节矩阵同时包含 `Baseline（全部不 offload）` 与 `Hybrid（offload_embedding/vision/audio=true）`
- **测试方式**: 每项热跑 4 轮取均值，首轮预热不计入统计

本轮已记录环境：

| 环境 | 硬件 | 系统 | 运行库 |
| :--- | :--- | :--- | :--- |
| Env A | Apple M3 Pro, 18GB 统一内存 | macOS 14.4 (`23E214`) | `mlx 0.31.1`, `mlx_lm 0.31.2`, `mlx_vlm 0.4.4` |
| Env B | Apple M4, 16GB 统一内存 | macOS 15.1.1 (`24B91`) | `mlx 0.31.1`, `mlx_lm 0.31.2`, `mlx_vlm 0.4.4` |

---

## 2. 测试结果

### 2.1 Env A (Apple M3 Pro / 18GB)

#### 2.1.1 核心对比矩阵

| 模型 | 模式 | text TTFT | text TPOT | vision 冷启动 TTFT | vision 热跑 TTFT | 4 并发 tok/s |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Qwen3.5-0.8B-4bit | Baseline | **37.2ms** | 8.4ms | **102.9ms** | **105.6ms** | **256.8** |
| Qwen3.5-0.8B-4bit | Hybrid | 35.7ms | **8.2ms** | 108.7ms | 206.6ms | 204.3 |
| Gemma-4-E2B-IT-4bit | Baseline | 46.2ms | 19.9ms | **873.6ms** | **875.9ms** | **138.5** |
| Gemma-4-E2B-IT-4bit | Hybrid | **36.9ms** | **16.6ms** | 934.1ms | 934.0ms | 132.2 |

### 2.2 Env B (Apple M4 / 16GB)

#### 2.2.1 核心对比矩阵

| 模型 | 模式 | text TTFT | text TPOT | vision 冷启动 TTFT | vision 热跑 TTFT | 4 并发 tok/s |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Qwen3.5-0.8B-4bit | Baseline | **36.1ms** | **7.2ms** | 124.5ms | 120.2ms | **262.8** |
| Qwen3.5-0.8B-4bit | Hybrid | 36.6ms | 7.3ms | **114.7ms** | **119.2ms** | 261.8 |
| Gemma-4-E2B-IT-4bit | Baseline | **36.8ms** | **17.6ms** | **1042.4ms** | **1038.7ms** | 127.4 |
| Gemma-4-E2B-IT-4bit | Hybrid | 38.5ms | 17.9ms | 1051.7ms | 1040.7ms | **130.8** |

结论：

- **M4 单核/GPU 性能强劲**: 在 Env B (M4) 上，Qwen 3.5 的 text TPOT 压到了 7.2ms，即使是内存带宽略低的环境，纯算力表现依然出色。
- **模式差异微小**: 在 16GB+ 设备上跑 0.8B/2B 模型，Hybrid 对速度的影响极小（正负 1-2ms 波动），但在极端高并发或显存吃紧时，Hybrid 提供的安全冗余更有价值。
- **Gemma 4 稳定性验证**: 本次实测确认 Gemma 4 在 M4 上运行非常稳健，且并发吞吐达到了 130 tok/s。

---

## 3. 显存分层效果

比较 `Hybrid` 相对 `Baseline（全部不 offload）` 的变化（负数表示 Hybrid 更快/更高）：

| 环境 | 模型 | text TTFT | text TPOT | vision 冷启动 TTFT | 4 并发 tok/s |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Env A** | Qwen3.5-0.8B | `-1.5ms` | `-0.2ms` | `+5.8ms` | `-52.5` |
| **Env A** | Gemma-4-E2B | `-9.3ms` | `-3.3ms` | `+60.5ms` | `-6.3` |
| **Env B** | Qwen3.5-0.8B | `+0.5ms` | `+0.1ms` | `-9.8ms` | `-1.0` |
| **Env B** | Gemma-4-E2B | `+1.7ms` | `+0.3ms` | `+9.3ms` | `+3.4` |


---

## 4. 局限

- **vision TPOT 不准确**：当前视觉测试只要求一个词，decode 步数太少，不适合测 VLM 持续生成速度。
- **Metal 显存不是完整峰值**：`mx.get_active_memory()` 只统计当前活跃显存，不含 Metal driver 保留区域。

---

## 5. 如何复现

```bash
# 确保服务运行中
uv run lumina server

# 单机 HTTP 基准
uv run python tests/benchmarks/http_bench.py --rounds 4

# 批量矩阵（自动拉起临时服务）
uv run python tests/benchmarks/run_matrix.py --rounds 4

# 仅文本
uv run python tests/benchmarks/http_bench.py --skip-vision --skip-concurrent
```

---

*最后更新: 2026-04-22 | Env A: Qwen3.5-0.8B-4bit / Gemma-4-E2B-IT-4bit（含 Gemma HTTP 实测与 `run_matrix.py` HTTP 主路径修复）*
