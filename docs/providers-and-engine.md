# Lumina Providers、Engine 与 MLX Offload

本文概述 Lumina 的推理主链路，重点说明 Provider 选择、MLX 加载、continuous batching，以及 offload 配置的真实生效边界。

## 1. 推理链路总览

从请求进入到模型执行的大致路径：

```text
HTTP / CLI
  -> LLMEngine
  -> Provider (Local / OpenAI / LlamaCpp)
  -> LocalProvider 时进入 MLX 加载与推理链路
```

常见文件：

- `lumina/cli/server.py`
- `lumina/engine/llm.py`
- `lumina/providers/__init__.py`
- `lumina/providers/local.py`
- `lumina/providers/mlx_loader.py`
- `lumina/engine/request_history.py`
- `lumina/api/sse.py`

## 2. Provider 选择与懒加载

Provider 工厂在 `lumina/cli/server.py:build_provider()`。

根据 `cfg.provider.type`，会选择：

- `OpenAIProvider`
- `LlamaCppProvider`
- `LocalProvider`

`lumina/providers/__init__.py` 使用懒加载暴露 `LocalProvider` / `OpenAIProvider`，原因是：

- `mlx` 在非 macOS 平台不可安全顶层 import
- CI / Linux 环境只需要导入基类时，不能被 `mlx` 依赖炸掉

因此不要把 `LocalProvider` 改回顶层静态 import。

## 3. `LocalProvider` 与 batching

`LocalProvider` 负责：

- 调用 `MlxModelLoader` 加载模型
- 处理 chat template 与 tokenizer
- 管理 prompt cache
- 执行 continuous batching

batching 设计要点：

- 每个请求有自己的 `_RequestSlot`
- prefill 与 decode 分阶段推进
- decode 只推进 prefill 前已存在的 slot，避免首 token 被覆盖
- EOS 需要手动检测

这部分逻辑主要在：

- `lumina/providers/local.py`
- `lumina/providers/scheduler.py`
- `lumina/engine/scheduler.py`

## 4. `mlx_loader` 的加载分层

`lumina/providers/mlx_loader.py` 的职责是：

- 解析模型路径
- 区分 `mlx_lm` 与 `mlx_vlm`
- 初始化 `BatchGenerator`
- 根据 offload 策略决定哪些参数先 `mx.eval`

当前设计是单一模式：

- **L1**：语言 backbone 层始终 eager-load
- **L2**：embedding / vision / audio 组件可选择不预加载

`should_eval()` 的语义是：

- 命中 backbone 层，必须 `mx.eval`
- 命中 offload 关键字，不 `mx.eval`
- 其他参数（如 norm、lm_head）默认 `mx.eval`

## 5. Offload 配置如何传递

配置入口在 `ProviderConfig`：

- `offload_embedding`
- `offload_vision`
- `offload_audio`

当前支持两种配置写法：

```json
{
  "provider": {
    "mlx_memory": {
      "offload_embedding": true,
      "offload_vision": true,
      "offload_audio": true
    }
  }
}
```

或旧格式：

```json
{
  "provider": {
    "offload_embedding": true,
    "offload_vision": true,
    "offload_audio": true
  }
}
```

读取时通过 `ProviderConfig._unpack_mlx_memory()` 做兼容展开。

启动时的实际传递链路：

```text
Config
  -> build_provider(cfg)
  -> LocalProvider(..., offload_*)
  -> MlxModelLoader.load(offload_*)
```

## 6. Offload 的真实生效边界

这是最容易被误解的地方。

### 会立即变化的东西

- `PATCH /v1/config` 后，配置文件会更新
- `get_config()` 返回的运行时单例会更新
- `GET /v1/config` 会看到新值

### 不会立即变化的东西

已加载进当前进程的 `LocalProvider` 不会因为 patch 自动重建，因此：

- 已经构造好的 provider 仍持有启动时的 offload 参数
- 已经 `mx.eval` 过的权重布局不会重新分层
- 当前进程的 MLX 行为要等重启后才切换

因此 `offload_*` / `mlx_memory.*` 属于**保存成功但需重启生效**的配置。

## 7. VLM 兼容层要点

`mlx_vlm` 与 `mlx_lm` 在几个地方并不兼容，LocalProvider 已做过桥接：

- logits 结构不同
- `make_cache()` 调用入口不同
- cache state 中可能包含 `None`

这也是为什么不要直接在别处绕过 `LocalProvider` 自己拼调用链。

## 8. 当前维护约定

### 对 offload 配置

- 读取时兼容旧格式
- 写回时规范化到 `provider.mlx_memory.*`
- `provider.backend` 之类 computed 字段不写回磁盘

### 对 provider patch 日志

- 真正做了运行时同步，才记为 hot-reload
- 仅保存但未在当前进程生效时，不应误报为 hot-reloaded

## 9. 推荐补测与排查方向

如果未来继续改这块，建议优先覆盖：

1. `provider.mlx_memory.*` 嵌套 patch
2. `restart_required` 与运行时行为是否一致
3. VLM 检测是否走对 `mlx_vlm`
4. 新模型架构下 offload 关键字是否仍能命中 vision / audio 组件

排查时优先看：

- `lumina/config.py`
- `lumina/config_runtime.py`
- `lumina/config_apply.py`
- `lumina/providers/mlx_loader.py`
- `lumina/providers/local.py`

