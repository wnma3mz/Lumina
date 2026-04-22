# MLX 内存分层与磁盘卸载 (L1/L2 Tiering)

本文件说明了 Lumina v0.8.5+ 引入的 MLX 内存优化技术。该技术允许在 16GB 及以下内存的 Mac 设备上更稳健地运行大型多模态模型。

## 1. 核心原理：L1/L2 分层加载

我们将模型的权重分为两个物理层级：

### **L1 (GPU 核心显存)** — "The Hot Path"
*   **组成**: Transformer Layers (Attention, MLP 块)。
*   **策略**: **锁死 (Eager-load)**。通过 `mx.eval` 强制将这些权重载入 Metal 显存。
*   **价值**: 保证 Token 生成的吞吐量 (TPOT) 无损，实现秒回响应。

### **L2 (统一内存/磁盘映射)** — "The Cold Path"
*   **组成**: Text Embedding 表、Vision Encoder (视觉塔)、Audio Encoder (音频塔)、**多模态投影层 (Projector/Merger)**。
*   **策略**: **延迟加载 (Lazy-load + CPU Offload)**。利用 `mmap` 留在磁盘，仅在需要时换入内存。
*   **价值**: 节省 300MB - 2GB+ 昂贵的 GPU 显存。对于大型多模态模型，投影层的卸载至关重要。

---

## 2. 配置指南

在 `~/.lumina/config.json` 的 `provider` 段落中配置（推荐使用 `mlx_memory` 嵌套块）：

```json
"provider": {
  "type": "local",
  "mlx_memory": {
    "offload_embedding": true,
    "offload_vision": true,
    "offload_audio": true
  }
}
```

也支持将字段直接写在 `provider` 顶层（向后兼容旧格式）：

```json
"provider": {
  "type": "local",
  "offload_embedding": true,
  "offload_vision": true,
  "offload_audio": true
}
```

修改后需重启服务方可生效（Web UI 设置页有对应开关）。

### 参数说明

| 参数 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `offload_embedding` | `true` | **推荐开启**。将 Embedding 层移出显存，节省 200MB–1GB，首字延迟增加约 200–500ms。 |
| `offload_vision` | `true` | **多模态必选**。视觉组件（Vision Tower + Projector）完全卸载，纯文本推理时不占显存。 |
| `offload_audio` | `true` | 音频组件卸载，纯文本推理时不占显存。 |

三个参数全部设为 `false` 时，所有权重锁入 Metal 显存，可获得最低延迟，但对内存压力最大。

---

## 3. 性能权衡 (Trade-offs)

开启 offload 后的典型表现（以 2B 模型为例）：

| 指标 | 影响 |
| :--- | :--- |
| TTFT（首字延迟） | 增加约 200ms–500ms（取决于 SSD 速度，冷启动时 Vision Encoder mmap 换入） |
| TPOT（Token 吞吐） | **无影响**。首 Token 生成后，后续性能与全显存模式一致 |
| Metal 显存节省 | 300MB–1GB（纯文本），1GB–2.5GB（多模态） |

对于 0.8B 这类极小模型，Embedding 层占比极小，offload 效果不明显（< 50MB）。**对 2B+ 模型效果显著。**

---

## 4. 当前实现说明

### 单模型原则

当前多模态请求不会再单独加载第二套 VLM 模型。

也就是说：

- 如果启动时加载的是 VLM，文本请求和图片请求共用同一套已加载模型
- 如果启动时加载的是纯文本模型，图片请求会直接报错，不会隐式走另一条 `vlm_load()` 路径

这样做的目的，是保证 `offload_vision` / `offload_audio` 对真实图片请求路径同样成立，而不是只对文本路径生效。

### 关于 `offload_embedding`

当前 `offload_embedding` 的工程语义是：

- 在加载阶段，`embed_tokens` 不会被 eager `mx.eval`
- 在 legacy prefill / system prompt cache 相关路径中，代码会显式使用 CPU stream 做 embedding 查找
- 在默认 BatchGenerator 路径中，主要依赖“embedding 权重未 eager eval”的分层结果，而不是完全独立的一套手写 CPU embedding 主循环

因此它确实生效，但不同调度路径的可观测方式并不完全相同。

---

## 5. 基准测试工具

性能指标通过 **HTTP `/v1/chat/completions` (SSE 流式)** 采集，与真实用户请求路径完全一致：

```
HTTP 客户端 → FastAPI → LocalProvider → mlx_vlm (VLM) / mlx_lm (LM) → Metal GPU
```

### 推荐脚本：`http_bench.py`

```bash
# 确保服务运行中
uv run lumina server

# 完整基准（文本 + 视觉 + 并发，另开终端）
uv run python tests/benchmarks/http_bench.py --rounds 4

# 仅文本
uv run python tests/benchmarks/http_bench.py --skip-vision --skip-concurrent

# 指定地址
uv run python tests/benchmarks/http_bench.py --url http://127.0.0.1:31821 --rounds 5
```

覆盖场景：纯文本推理（TTFT/TPOT，多轮均值）、Vision 冷/热启动、4 并发吞吐（tok/s）。

### 废弃脚本：`engine_performance.py`

旧版脚本直接调用 `mlx_lm.load` + 裸 `model()` 推理，绕开了 VLM 分发、chat template、KV cache 管理等关键路径，数据不具代表性，**已废弃**，保留仅供历史对比。

---

## 6. 最近验证结果

本轮围绕 offload / VLM 主路径做过完整回归：

```bash
uv run pytest -q
```

结果：

- `282 passed, 1 skipped`

重点验证项：

- 图片请求不再二次加载另一套 VLM 模型
- 图片请求与文本请求共享启动时已加载的同一模型对象
- 纯文本模型明确拒绝图片输入

---

## 7. 参考资料

- MLX Lazy Loading: `mlx.core.load` with safetensors.
- Memory Mapping: `mmap(2)` via macOS Page Cache.
- Implementation: `lumina/providers/mlx_loader.py`
- 性能测试报告: `docs/performance-report.md`
