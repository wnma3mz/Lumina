# Lumina 配置系统说明

本文面向维护者，说明 Lumina 当前的配置真源、读写路径、热更新边界，以及新增配置字段时应修改哪些位置。

## 1. 配置真源

运行时唯一真源是 `lumina.config.get_config()` 返回的 `Config` 单例。

- CLI、FastAPI、菜单栏逻辑都应从这里读取当前配置
- 不要为新功能再引入长期存在的模块级配置 singleton
- 某些子模块会保留运行时镜像或局部缓存，但最终应与 `get_config()` 同步

## 2. 配置文件路径

`lumina/config_runtime.py` 统一了配置路径解析：

- 优先使用显式传入路径
- 其次使用 `set_active_config_path()` 指定的活动路径
- 再次使用 `~/.lumina/config.json`
- 只读回退时，才会使用包内 `lumina/config.json`

相关函数：

- `resolve_config_path()`
- `read_config_data()`
- `read_mutable_config_data()`
- `write_config_atomic()`

## 3. `/v1/config` 的处理分层

`PATCH /v1/config` 不再在 router 内手写 merge / 写盘 / 同步副作用，而是拆成两层：

### `ConfigStore` (`lumina/config_runtime.py`)

负责：

- 合并 patch 到当前可写配置
- 规范化持久化结构
- 原子写回磁盘
- 构建新的运行时 `Config`
- 判断 `restart_required`

关键入口：

- `ConfigStore.apply_patch()`
- `patch_requires_restart()`
- `replace_runtime_config()`
- `serialize_runtime_config()`

这里的意思是：运行时配置更新现在只有这一条正式路径。旧的按 section 局部 merge 的 runtime update 逻辑已经移除，不应再新增第二套“临时同步函数”。

其中：

- `Config.from_data()` / `normalize_config_data()` 是冷启动与 PATCH 共用的唯一 normalize 入口
- `ConfigStore.apply_patch()` 合并 patch 后会直接走这条入口，再 `model_validate()`
- `serialize_runtime_config()` 只序列化真实 runtime section（`provider/system/digest/document/vision/audio`），不再依赖额外 `include` 黑魔法去兜 computed property；`ui` 继续通过 `system.ui` 暴露
- 写盘仍保留旧契约：`ui` 可继续以顶层 legacy shape 持久化，但运行时只认 `system.ui`

### `ConfigApplier` (`lumina/config_apply.py`)

负责：

- digest 配置与 scheduler reload
- request history 模块同步
- ASR prompts 与 transcriber 模型同步
- LLM system prompts 同步
- OpenAI provider 连接参数热更新

注意：

- `ConfigApplier` 只处理“已经确认可安全热更新”的副作用
- 不能把 merge / validate / write 逻辑重新塞回 router
- 也不要重新引入旧式的 section-based runtime update helper

### Router PATCH body

`PATCH /v1/config` 的请求体不再直接复用完整 `Config` model，而是使用专用 `ConfigPatch`：

- 显式允许 `provider/system/digest/document/vision/audio/ui` 这些顶层 patch key
- `provider.backend` 这类 computed 字段会在写盘前剔除
- 路由层不再假装“完整配置重验一次”，只负责接收合法 patch，再交给 `ConfigStore`

## 4. 当前支持热更新的字段

以下字段修改后可在当前进程内生效：

- `digest.*`
- `provider.prompts.*`
- `digest.prompts.*`
- `document.prompts.*`
- `vision.prompts.*`
- `audio.prompts.*`
- `provider.sampling.*`
- `digest.sampling.*`
- `document.sampling.*`
- `vision.sampling.*`
- `audio.sampling.*`
- `system.request_history.*`
- `audio.whisper_model`
- `system.server.log_level`
- `provider.openai.base_url`
- `provider.openai.api_key`
- `provider.openai.model`

其中 `provider.openai.*` 只有在当前 backend 已经是 `openai` 时，才会同步到运行中的 provider 对象。

## 5. 仍需重启的字段

以下字段会写入配置并更新运行时单例，但**当前进程行为不会立刻切换**：

- `system.server.host`
- `system.server.port`
- `system.desktop.*`
- `provider.type`
- `provider.model_path`
- `provider.llama_cpp.*`
- `provider.offload_embedding`
- `provider.offload_vision`
- `provider.offload_audio`
- `provider.mlx_memory.*`

典型例子是 MLX offload：配置会保存成功，但已加载的 `LocalProvider` 和已 `mx.eval` 的权重布局不会在当前进程内重建。

## 6. `mlx_memory` 的持久化约定

项目当前对 offload 配置采用“读兼容、写规范”：

- 读取时兼容两种格式：
  - `provider.mlx_memory.offload_*`
  - `provider.offload_*`
- 写回时规范化为推荐格式：

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

此外，`provider.backend` 是 computed 字段，只用于运行时响应，不应写回配置文件。

## 7. 新增配置字段时的检查清单

新增字段时，优先按以下顺序检查：

1. `lumina/config.py`
   定义 schema、默认值，并把兼容迁移收进 `normalize_config_data()`
2. `lumina/config_runtime.py`
   处理 patch merge、规范化写盘、`restart_required` 判定
3. `lumina/config_apply.py`
   如果该字段支持热更新，在这里定义副作用
4. `lumina/api/templates/config_form.html`
   如果 Web UI 需要配置入口，补表单与保存逻辑
5. 测试
   至少覆盖：
   - `Config.load()`
   - `PATCH /v1/config`
   - `restart_required`
   - 若可热更新，补运行时行为测试

## 8. 常见误区

- 不要把 `patch.model_dump()` 的 computed 字段直接写回配置文件
- 不要把“保存成功”误写成“热更新成功”
- 不要只改 `config.json` 模板而不改 Pydantic schema
- 不要在 router 里手写某个 section 的局部 merge 逻辑
- 不要同时维护 `Config.load()` 和 PATCH 路径两套 normalize 规则

