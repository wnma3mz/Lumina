# Lumina 架构设计亮点

Lumina 的核心主张是：AI 工作流不需要上传到云端。所有推理、采集、渲染都在本地完成。这份文档记录几个值得关注的设计决策，说明"本地优先"的要求是如何具体落地的。

---

## 一、日报采集：全量快照 + 并发 + 随机化

### 一句话

每次生成日报，都是"现在回头看过去 N 小时"的完整快照，不依赖增量 cursor，不存任何同步状态。

### 为什么这样设计

增量采集的直觉是省资源——只取新增的部分。但在本地场景里，这会引入状态管理：上次采集到哪了？cursor 丢了怎么办？重启后要不要补采？

全量快照的逻辑更简单：每个 collector 拿到同一个截止时间戳 `time.time() - history_hours * 3600`，各自独立往回扫，按时间倒序取 top N 条。没有全局状态，没有 cursor 对账，重启后自然补齐。

唯一的"状态"是 Markdown 文件的内容 md5，用于过滤 iCloud 同步、编辑器后台扫描导致的 mtime 变化但内容未变的文件——这是真正需要去重的场景，而不是采集状态管理。

### 技术细节

**并发采集，带超时控制**

所有 collector 在 `ThreadPoolExecutor` 里并发运行，整体 30 秒超时。超时后主协程立即返回，慢 collector 在后台自然结束（`shutdown(wait=False)`），不阻塞日报生成。

```python
# services/digest/core.py
with ThreadPoolExecutor(max_workers=len(active)) as pool:
    futures = {pool.submit(c.collect, cutoff): c for c in active}
    ...
# finally: shutdown(wait=False)  — 超时不等待
```

**采集顺序随机化**

每次执行前 `random.shuffle(active)`，打乱 collector 顺序。目的是让各数据源在 LLM 上下文 token 中均匀分布——如果顺序固定，最末尾的来源每次都可能被截断。

**并发安全**

生成任务由 `asyncio.Lock` 保护，`maybe_generate_digest()` 在 acquire 前先检查 `lock.locked()`，已锁则直接跳过（非阻塞），不排队等待。这避免了多次触发（定时器 + 前端轮询 + 手动刷新同时到来）时的重复生成。

插件开发指南见 [digest-collector-plugin.md](digest-collector-plugin.md)。

**数据源覆盖**

| 来源 | 平台 | 截止时间字段 |
|---|---|---|
| Shell 历史（zsh/bash/fish/PowerShell） | 全平台 | zsh `: ts:0;cmd` 前缀 |
| Git 提交 | 全平台 | `--since=datetime` |
| 浏览器历史（Chrome/Edge/Firefox/Safari） | 全平台 | Chrome µs epoch；Firefox µs；Safari CoreData epoch |
| 备忘录（Notes.app） | macOS | `ZMODIFICATIONDATE1`（CoreData epoch） |
| Markdown 笔记 | 全平台 | `st_mtime` + md5 内容去重 |
| 日历（Calendar.app） | macOS | EventKit |
| AI 对话（Cursor/Claude 等） | 全平台 | 各子来源 Unix 秒 |

---

## 二、本地推理引擎：Continuous Batching + L1/L2 内存分层

### 一句话

单进程处理多个并发请求，不排队；同时把多模态模型的"非核心"权重从 GPU 显存里卸载出去，让 16GB Mac 也能跑 2B 多模态模型。

### Continuous Batching

朴素的实现是请求排队：上一个请求跑完，再跑下一个。这对吞吐量很差。

Lumina 的实现是 prefill 与 decode 分阶段：

- **Phase 1**：对所有新到的请求做 prefill（处理输入、填充 KV cache）
- **Phase 2**：只推进 prefill **之前**已存在的 slot 做 decode（生成 token）

Phase 2 的"快照"是关键——它防止一个刚 prefill 完的请求在同一轮 decode 里立刻抢占其他请求的首 token 位置。

每个请求有自己独立的 `_RequestSlot`，用 `asyncio.Queue` 传 token，无共享状态。EOS 手动检测（mlx-lm 的 `generate_step` 不自动停，检测 token id `248046`，即 `<|im_end|>`）。

### L1/L2 内存分层

多模态模型（含 vision encoder、audio encoder、embedding 表）在全量加载时会比纯文本模型多占 1–3GB 显存。对于 16GB 机器，这是不可忽视的压力。

分层策略：

| 层 | 内容 | 策略 |
|---|---|---|
| **L1（GPU 核心）** | Transformer backbone layers | `mx.eval` 锁入 Metal 显存，保证 decode 吞吐 |
| **L2（统一内存/磁盘映射）** | Embedding 表、Vision Tower、Projector、Audio Encoder | 不 eager eval，利用 mmap 按需换入 |

实测（Apple M4 / 16GB，Gemma-4-E2B-IT-4bit）：

| 指标 | 影响 |
|---|---|
| TTFT（首字延迟） | 增加约 0–60ms（取决于 SSD 速度和是否冷启动） |
| TPOT（后续 token 吞吐） | **无影响** |
| Metal 显存节省 | 300MB–2.5GB（视模型规模） |

**单模型原则**：不论是文本请求还是图片请求，都绑定到启动时加载的同一套模型对象。不存在"文本用 LM、图片另起一套 VLM"的分叉——这保证了 offload 配置对图片请求路径同样成立。

**VLM 兼容层**：`mlx_vlm` 与 `mlx_lm` 在三处接口不兼容（logits 结构、`make_cache()` 入口、`ArraysCache.state` 含 `None`），均在 `LocalProvider` 中统一桥接，上层调用无需感知。

详细机制见 [inference-engine.md](inference-engine.md)。

### 相关文件

- `lumina/providers/local.py` — 生命周期、调度、公共接口
- `lumina/providers/mlx/loader.py` — 模型加载 + offload 分层
- `lumina/providers/mlx/vlm.py` — VLM 图片输入规范化与推理
- `lumina/providers/mlx/offload.py` — CPU embedding offload 前向分支

---

## 三、配置热更新：ConfigStore + ConfigApplier 两层分离

### 一句话

改一个配置字段，系统能区分"立刻在当前进程生效"和"下次重启才生效"——前者不要求重启，后者明确告知用户。

### 为什么需要两层

朴素实现是：收到 PATCH 请求 → 写文件 → 重载配置。问题是"重载"这个词掩盖了两件不同的事：

1. **持久化**：把新值写回 `~/.lumina/config.json`
2. **副作用**：让当前运行的模块立即感知变化（比如 digest scheduler 换了触发时间、ASR 换了模型、日志级别变了）

如果把这两件事混在一起写，每新增一个"可热更新字段"都要在 router 里手写一段同步逻辑，很快就会失控。

分层之后：

```
PATCH /v1/config
  └─ ConfigStore.apply_patch()       # merge + validate + 原子写盘 + 更新运行时单例
       └─ ConfigApplier.apply()      # 热更新副作用（digest/ASR/LLM prompts/OpenAI 连接参数）
```

Router 只负责接收合法 patch，不再手写任何 section 级别的同步逻辑。

### 当前边界

**不需要重启的字段：**
`digest.*`、各域 `prompts.*`、各域 `sampling.*`、`system.request_history.*`、`audio.whisper_model`、`system.server.log_level`、`provider.openai.*`

**需要重启的字段：**
`system.server.host/port`、`provider.type`、`provider.model_path`、`provider.mlx_memory.*`（已加载的模型权重布局不能在运行时重建）

`PATCH /v1/config` 的响应体会包含 `restart_required: true/false`，前端据此决定是否提示重启。

### 配置规范化单源

`Config.from_data()` / `normalize_config_data()` 是冷启动和 PATCH 共用的同一条 normalize 入口，不存在两套迁移逻辑漂移的问题。新增字段时，只需改 `config.py`、`config_runtime.py`、`config_apply.py` 三个文件。

---

## 四、Web UI：HTMX + Jinja2，无前端构建链

### 一句话

Web UI 没有 React/Vue，没有 node_modules，没有构建步骤。后端直接渲染 HTML，局部刷新靠 HTMX，样式靠 Tailwind 编译产物直接提交。

### 为什么不用 SPA 框架

本地工具的 Web UI 诉求和互联网产品不同：

- 状态大部分在服务端（配置、日报内容、PDF 任务进度）
- 局部刷新场景有限（digest 内容、PDF 进度、设置表单）
- 不需要离线状态管理，不需要路由库

用 HTMX 的优势是：服务端直接掌握所有状态和渲染逻辑，前端只负责"点击触发请求，把响应 HTML 插入 DOM"。

### 关键约定

**`/fragments/*` 端点返回 HTML，不返回 JSON**

HTMX 不会自动解析 JSON 成 DOM，`/fragments/*` 路由必须返回 `HTMLResponse` 或 `TemplateResponse`。

**Tab 切换零 JS**

首页 Tab 切换用 `<input type="radio"> + CSS :checked ~` 实现：

```html
<div id="app">
  <input type="radio" id="tab-digest" hidden>
  <input type="radio" id="tab-docs" hidden>
  <div class="tabs">...</div>
  <main>...</main>
</div>
```

Radio 必须与 `.tabs`、`<main>` 处于同一父节点，`~` 兄弟选择器才能生效。

**轮询自动停止**

PDF 任务进度通过 `hx-trigger="every 2s"` 轮询。任务完成后，后端返回不含轮询触发器的 HTML，HTMX 自动停轮询——不需要前端写停止逻辑。

**静态资源单一来源**

启动时 `sync_static()` 将源码内的 `static/` 同步到 `~/.lumina/static/`，FastAPI 优先 serve 后者。CLI 模式和 `.app bundle` 模式使用同一份最新文件，不会因打包时静态资源版本不一致而出现"代码更新了但页面没变"的问题。

**PWA 支持**

`/manifest.json` 由 `server.py` 内联返回，首页包含 `apple-mobile-web-app-capable`、`theme-color` 等 meta，支持添加到主屏幕作为 PWA 使用。

### 相关文件

- `lumina/api/server.py` — FastAPI 装配，`GET /` 渲染入口
- `lumina/api/routers/fragments.py` — HTMX 片段路由
- `lumina/api/templates/index.html` — 主页面（含 Tab 骨架）
- `lumina/api/templates/panels/` — 各 Tab 初始 HTML
- `lumina/api/static/style.css` — Tailwind 编译产物（直接提交）
- `lumina/api/static/input.css` — Tailwind 源文件（需改样式时编辑这里）
