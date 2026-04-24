# Lumina — 开发上下文

## 项目定位

本地 LLM HTTP 服务，运行于 Apple Silicon Mac。核心功能：
- PDF 翻译 / 总结（调用 pdf2zh，后端指向本地服务）
- 文本润色
- 语音转文字（mlx-whisper）
- 每日活动摘要（日报）
- Finder 右键快速操作（Quick Action）

## 技术栈

| 层 | 实现 |
|---|---|
| LLM 推理 | mlx-lm（Apple Silicon GPU） |
| HTTP 服务 | FastAPI + uvicorn，端口 `31821` |
| 菜单栏 | rumps（.app bundle 模式） |
| 打包 | PyInstaller，spec 在 `scripts/lumina_full.spec` |
| 包管理 | uv，`uv run lumina` 启动开发模式 |

## 目录结构

```
lumina/
  main.py              # CLI 入口：argparse 骨架 + main()，不含业务逻辑
  config.py / config.json  # 配置加载，端口默认 31821
  cli/                 # 子命令实现（v0.6 从 main.py 拆出）
    server.py          # cmd_server / cmd_stop / cmd_restart，含菜单栏 App
    pdf.py             # cmd_pdf / cmd_summarize / cmd_watch
    text.py            # cmd_polish / cmd_popup
    setup.py           # ensure_model / lite_setup_wizard
    utils.py           # 公共工具：日志、config 路径、PID、Banner 等
  api/
    server.py          # FastAPI app 创建，路由注册；提供 GET /manifest.json（PWA）
    sse.py             # SSE 流式辅助：stream_llm()
    routers/           # 路由模块（document/vision/audio/digest/chat/config/fragments/game）
      fragments.py     # HTMX HTML 片段路由（Jinja2 渲染，返回可直接插入 DOM 的片段）
      game.py          # 语言效果游戏：POST /v1/game/score（LLM 打分，1-5 分）
    templates/         # Jinja2 模板（GET / 由此渲染，无静态 HTML）
      index.html       # 主页面（HTMX + 纯 CSS tab 切换，内联 htmx.min.js，PWA meta）
      panels/          # 各 tab 面板的初始 HTML（digest/document/lab/settings/audio/game）
      config_form.html # 设置表单片段（/fragments/config 返回）
      digest_content.html  # 日报时间轴片段（/fragments/digest 返回）
      digest_sources.html  # 数据来源图标行片段
      pdf_progress.html / pdf_result.html / pdf_error.html  # PDF 任务状态片段
      report_content.html  # 日报/周报/月报内容片段
    static/
      style.css        # Tailwind 编译产物（直接提交）；从 input.css 编译，禁止手动修改
      input.css        # Tailwind 源文件（唯一样式源）；在 lumina/ 目录下编译
      logo.svg         # 应用图标
      index.html       # 独立备份（含内联 HTMX），仅供离线测试，GET / 不再 serve 此文件
  providers/
    __init__.py        # 懒加载：LocalProvider / OpenAIProvider 按需 import（见下节）
    local.py           # mlx-lm/mlx-vlm 本地推理，含 Continuous Batching 和 VLM 兼容层
    mlx_loader.py      # Layer 0：模型路径解析 + 加载（自动识别 VLM/LM）+ BatchGenerator 初始化
    mlx_prompt.py      # Layer 1：chat_template 渲染 + tokenize + system prefix 提取
    openai.py          # OpenAI 兼容远程接口
  services/
    document/          # 文档处理服务（PDF/文本、watcher、pdf_cache）
    audio/             # 音频处理服务（转写、ptt 录音等）
    digest/            # 日报生成服务与采集器（核心逻辑及各种数据源快照）
    vision/            # 视觉处理服务
  engine/              # 核心引擎层（llm/sampling/请求上下文/请求历史）
  platform_support/    # 平台特定实现（popup 浮窗、platform_utils）
scripts/
  build.sh             # macOS 平台打包脚本（含 Full / Lite 模式）
  install.sh           # macOS / Linux 快速安装与环境配置脚本
  install.ps1          # Windows 环境安装与右键菜单配置脚本
  lumina_full.spec     # PyInstaller spec（固定路径，保证缓存命中）
  install_quick_action.sh  # 安装 Finder 右键 workflow
```

## 运行方式

```bash
uv run lumina server          # 开发模式启动服务
uv run lumina server --ptt    # 同时启动 PTT 热键守护（长按 F5 录音）
bash scripts/build.sh         # 打包为 Lumina.app
```

命令行模式与 .app bundle 模式行为**应一致**，包括每小时定时生成日报。

## 关键设计决策

### Providers 懒加载（`providers/__init__.py`）

`LocalProvider`（依赖 `mlx`）和 `OpenAIProvider` 通过模块级 `__getattr__` 懒加载，不在顶层 import：

```python
def __getattr__(name: str):
    if name == "LocalProvider":
        from .local import LocalProvider
        return LocalProvider
    ...
```

**原因**：`mlx` 在非 macOS 平台（如 Linux CI）import 时直接报错。懒加载后 `from lumina.providers import BaseProvider` 在任何平台均安全，`LocalProvider` 只在实际使用时才被加载。**不要改回顶层 import。**

### Continuous Batching（`providers/local.py`）
- `_RequestSlot`：每个请求独立的 `asyncio.Queue` 传 token，无共享状态
- 调度器：Phase 1 prefill 新请求，Phase 2 只推进 **prefill 前已存在** 的 slot（快照 `existing_decode`），防止首 token 被覆盖
- EOS 检测：mlx-lm `generate_step` 不自动停，手动检测 token id 248046（`<|im_end|>`）

### VLM 兼容层（`providers/local.py`）

mlx_vlm 与 mlx_lm 在以下三处存在接口差异，均已在 `LocalProvider` 中统一处理：

1. **`_extract_logits(output)`**：mlx_lm 直接返回 `mx.array`；mlx_vlm 的 `LanguageModel.__call__` 返回 `LanguageModelOutput(logits=...)`，需要 `getattr(output, "logits", output)` 解包。
2. **`_make_prompt_cache()`**：VLM 模型顶层无 `make_cache()`，直接传给 `mlx_cache.make_prompt_cache` 会生成全 `KVCache` 结构，Mamba-Hybrid 架构（如 Qwen3.5）需要混合 `ArraysCache`+`KVCache`。必须用 `model.language_model.make_cache()` 获得正确结构。
3. **`_eval_cache_state(prompt_cache)`**：`ArraysCache.state` 返回含 `None` 的列表，`mx.eval(None)` 会报错，需要 flatten + filter None 后再 eval。

### Digest 采集模式（纯全量快照）

每个 collector 每次都取 `time.time() - cfg.history_hours * 3600` 作为截止时间，没有增量/全量之分。**没有 cursor 机制。**

每次运行都是"现在回头看过去 N 小时"的完整快照，各来源最多返回 top N 条按时间倒序的记录。

**各来源截止时间计算：**

| Collector | cutoff 用法 | 数据源字段 |
|---|---|---|
| collect_shell_history | `cutoff = time.time() - history_hours * 3600` | zsh `: ts:0;cmd` 前缀 |
| collect_git_logs | `--since=datetime.fromtimestamp(cutoff)` | `git log --format=%ct` |
| collect_clipboard | 无状态 | — |
| collect_browser_history | 各浏览器 epoch 转换 | Chrome µs；Firefox µs；Safari CoreData epoch |
| collect_notes_app | `cutoff_core = cutoff - 978307200` | `ZMODIFICATIONDATE1` |
| collect_markdown_notes | `mtime > cutoff` + md5 内容去重 | `st_mtime` |
| collect_ai_queries | `ts <= cutoff` 跳过 | 各子来源 Unix 秒 |

**Markdown 去重：** `cursor_store.py` 仅保留 `md5_of_file` + `load_md_hashes`/`save_md_hashes`，用于过滤 mtime 改变但内容未变的文件（iCloud 同步、编辑器扫描等）。

### Digest collector 元数据（`api/ui_meta.py`）

- 运行时 collector key 的唯一来源是 `services/digest/collectors/__init__.py` 自动发现出的 `COLLECTORS`
- `api/ui_meta.py` 里的 `COLLECTOR_DEFS` 只是**显式覆盖表**，不再是完整真源
- 新增或插件 collector 时：
  - 有显式元数据就用显式元数据
  - 没有就自动生成默认 `label` / `icon` / `filter_key`
  - 时间轴颜色默认回落到中性样式
- 不要再写一份独立的“前端 collector 列表”去和运行时发现结果并行维护

### Digest 并发安全（`services/digest/core.py`）

- **锁机制**：`asyncio.Lock`（`_digest_lock`），懒初始化（`_get_digest_lock()`）。`maybe_generate_digest` / `maybe_generate_changelog` 在 acquire 前先检查 `lock.locked()`，已锁则直接跳过（非阻塞等待）。
- **不要用文件锁**：之前用过 `.lock` 文件 + `exists()`/`touch()`，但文件锁在单进程 asyncio 中无法防止并发重入（两个协程可能在 `exists()` 返回 False 后、`touch()` 执行前同时通过检查）。asyncio.Lock 是真正原子的。
- **executor 超时后不阻塞**：`_collect_all()` 里 `ThreadPoolExecutor` 在 `finally` 中调用 `shutdown(wait=False)`，允许主协程在 30s 超时后立即返回，慢 collector 线程在后台继续完成后自然退出。**不要改成 `wait=True`**，否则整个 digest 生成会被单个慢 collector 卡住。
- **`digest.enabled` 默认值**：`DigestConfig.enabled = False`（dataclass），`configure()` 中 key 缺失时也默认 `False`。**不要把 `configure()` 里的默认值改成 `True`**，否则与 dataclass 语义不一致，导致「配置文件没有 digest 段时误启用日报」。

### 日报定时生成与冷却
- `.app` 模式：`rumps.timer(3600)` 在 `_run_with_menubar()` 中触发
- 命令行模式：`_start_digest_timer(llm)` 用 `threading.Timer` 循环，行为一致
- 前端：HTMX `hx-trigger="every 5m"` 轮询 `/fragments/digest`，后端每次重新渲染时间轴内容（无 JS 比对逻辑）
- **启动冷却**：`maybe_generate_digest()` 在生成前先检查上次生成时间（从 digest.md mtime 恢复），若距今不足 `refresh_hours`（默认 1h）则跳过，防止每次重启都重复采集
- **采集顺序随机化**：`_collect_all()` 每次执行前 `random.shuffle(active)` 打乱 collector 顺序，确保各来源在 LLM token 上下文中均匀分布

### MLX 内存分层（`providers/mlx_loader.py`）

加载策略只有一种：**L1 Eager + L2 Offload（Hybrid）**，不再有 `lazy_load` 全量 offload 模式。

- **L1（Backbone）**：`language_model.model.layers.*` 或 `model.layers.*`（非 vision/audio）始终 `mx.eval` 锁入 Metal 显存。
- **L2（辅助组件）**：由三个开关控制，默认全部 `true`：
  - `offload_embedding`：卸载 `embed_tokens`
  - `offload_vision`：卸载 `visual / vision_tower / merger / projector / projection`
  - `offload_audio`：卸载 `audio_tower / audio_projector`
- **配置格式**：`config.json` 中用 `provider.mlx_memory` 嵌套块（推荐），也可直接写在 `provider` 顶层（向后兼容）。`ProviderConfig._unpack_mlx_memory` 在解析时自动展平。

### 配置 Web UI（`/v1/config`）
- **GET `/v1/config`**：返回完整运行时配置（来自 `get_config()` singleton）
- **运行时唯一真源**：内存中只认 `get_config()` 返回的 `Config` singleton。不要再为新功能引入第二套长期配置 singleton。
- **normalize 真源**：`Config.from_data()` / `normalize_config_data()` 是冷启动和 `PATCH /v1/config` 共用的唯一配置规范化入口。不要再在 `Config.load()` 和 PATCH 路径各写一套迁移逻辑。
- **PATCH `/v1/config`` 实现分层**：
  - `ConfigStore`（`config_runtime.py`）负责 merge / validate / 原子写盘 / 更新运行时 `Config`
  - `ConfigApplier`（`config_apply.py`）负责热更新副作用（digest、request_history、ASR、LLM prompts 等）
  - router 使用专用 `ConfigPatch` body，只接收 patch 允许的顶层 section；`provider.backend` 这类 computed 字段不写盘
  - 新增字段时优先改 `config.py`、`config_runtime.py`、`config_apply.py`，不要把同步逻辑重新塞回 router
- **当前支持热更新的高频字段**：
  - `digest.*`、各域 `prompts.*`、各域 `sampling.*`
  - `system.request_history.*`
  - `audio.whisper_model`
  - `system.server.log_level`
  - `provider.openai.base_url` / `api_key` / `model`（仅当前 backend 已是 openai 时）
  - `ptt.*` 仍依赖 config 文件 mtime watcher 自动重载
- **仍需重启的字段**：`system.server.host/port`、`system.desktop.*`、`provider.type`、`provider.model_path`、`provider.llama_cpp.*`、`provider.offload_*` / `provider.mlx_memory.*`
- **并发安全**：写操作通过模块级 `asyncio.Lock` 保护；临时文件 + `rename()` 原子写入

### Messages 解析单源（`providers/message_parts.py`）

- `messages` 的 part 遍历、provider 文本降级、history 文本记录、VLM 图片拆分都走 `providers/message_parts.py`
- `BaseProvider` / `LLMEngine` / `LocalVlmAdapter` 不应再各自维护一份 `role/content/parts` 遍历逻辑
- 新增消息 part 类型时，先改 `message_parts.py`，再按需要补具体 provider 能力分支

### PyInstaller + multiprocessing
- `babeldoc`（pdf2zh 依赖）用 `multiprocessing.Process` 做字体子集化
- macOS 默认 `spawn` 模式：子进程用 `sys.executable` 重新启动，走到 argparse 报错 `invalid choice: 'from multiprocessing...'`
- 修复：`if __name__ == "__main__"` 里在 `main()` 之前调用 `multiprocessing.set_start_method("fork")`
- `fork` 只能在主进程设置，放在入口最早执行，子进程不会再执行此处
- `main()` 内保留 `freeze_support()`（PyInstaller 打包后有效，开发模式 no-op）

### PyInstaller 构建速度
- `collect_all` 扫描 mtime 导致 Analysis 缓存几乎不命中，1m45s 是实际下限
- 排除 `torch`（节省 ~356MB 和 ~8s）；不能排除 `unittest`（scipy 依赖它）

### macOS 图标
- SVG rect: `x=20 y=20 w=216 h=216 rx=46`（符合 macOS HIG 安全边距）
- PyInstaller 的 sips 处理会丢失透明通道，build 脚本最后用源文件覆盖 bundle 里的 icns

## 测试资源

- `tests/fixtures/2010_Bottou_SGD.pdf` — 10 页英文论文，用于 PDF 翻译/总结功能的快速测试（约 40s 完成）

## 代码质量

**每次改完代码必须 lint**，用 ruff 检查所有改动过的文件，0 error 才能提交：

```bash
uv run --with ruff ruff check <改动的文件...>
# 可自动修复的先跑：
uv run --with ruff ruff check --fix <改动的文件...>
```

Provider 相关回归测试已按主题拆分，不再往单个超大文件里堆：

- `tests/providers/test_local_provider_scheduling.py`
- `tests/providers/test_local_provider_loading.py`
- `tests/providers/test_local_provider_vlm_messages.py`
- `tests/providers/test_mlx_model_loader.py`
- `tests/providers/test_mlx_batch_scheduler.py`
- `tests/providers/test_system_prompt_cache.py`
- 共享 helper 在 `tests/providers/local_provider_test_helpers.py`

## Git 操作规范

- **禁止在未经用户明确许可的情况下 push 到远端**（包括 `git push`、`gh release`）
- commit 可以随时做，push / release 必须等用户说「可以 push」或「发包」

## 前端开发准则

### 技术栈约定

- **`style.css` 是编译产物，禁止手动修改**：`style.css` 由 Tailwind CLI 从 `input.css` 编译生成，手动修改会在下次编译时被覆盖。所有样式改动在 `input.css` 里进行，然后重新编译提交。
- **`input.css` 是唯一样式源**：所有样式修改、新增原子类或自定义组件，必须在 `lumina/api/static/input.css` 中进行。
- **编译命令**（**必须在 `lumina/` 子目录下运行**，`tailwind.config.js` 在那里，不是项目根目录）：
  ```bash
  cd lumina
  npm install --save-dev tailwindcss@3 @tailwindcss/typography   # 首次或环境重建时
  ./node_modules/.bin/tailwindcss -i api/static/input.css -o api/static/style.css
  cd ..
  git add lumina/api/static/style.css lumina/api/static/input.css
  ```
  `lumina/node_modules/` 和 `lumina/package-lock.json` 已在 `.gitignore`，不提交。`lumina/package.json` 和 `lumina/tailwind.config.js` 已提交到 git。
- **`@apply` 只能用在 `@layer` 块内**：`@layer components` / `@layer utilities` 之外的纯 CSS 选择器块不能使用带状态前缀的 `@apply`（如 `@apply hover:text-zinc-900` 或 `@apply dark:hover:text-white`）——Tailwind JIT 不会展开这些前缀。正确做法：直接写 CSS 属性值（`color: rgb(24 24 27)`），dark hover 用 `:hover:is(.dark *)` 复合选择器。
- **HTMX 服务端渲染**：局部刷新通过后端 Jinja2 模板返回 HTML 片段实现（`/fragments/*` 路由），片段内的样式依赖全局 `style.css`，不需要额外引入 CSS。
- **PWA 支持**：`GET /manifest.json` 由 `server.py` 内联返回；`index.html` 包含 `<link rel="manifest">`、`apple-mobile-web-app-capable`、`theme-color` 等 meta，支持添加到主屏幕。

### 设计语言（Bento Card 风格）

v0.8.0 起采用 **Bento Card** 设计风格，替代原有毛玻璃（glassmorphism）风格：

- **`bento-card` 类**：圆角卡片，白色背景 / 深色模式自动切换，细边框 + 轻阴影。
- **颜色体系**：zinc（中性灰）+ indigo（主色调）+ 各来源色（emerald=git, blue=browser, amber=clipboard, purple=notes...）。
- **深色模式**：通过 `dark:` Tailwind variant + `<html class="dark">` 切换（JS 读写 `localStorage.theme`，跟随系统偏好）。
- **不要引入外部图标库**：优先用 emoji；SVG 图标只用于 logo.svg。

### 布局

- Flex / Grid 布局优先，响应式使用 Tailwind `md:` 断点。
- 设置页采用 `grid-cols-4`（左侧 `md:col-span-1` 导航，右侧 `md:col-span-3` 内容）。

### 交互反馈

按钮用 Tailwind `hover:opacity-80 active:scale-95 transition-all` 组合，或在 `input.css` 的 `@layer components` 里定义语义组件类（编译后体现在 `style.css`）。

### z-index 层级规范

| 层 | 值 | 用途 |
|---|---|---|
| 内容 | 默认 | 正常文档流 |
| 保存栏 | 100 | `#save-bar`（底部固定栏） |
| 宠物挂件 | 150 | `lumina-buddy`（ASCII 宠物） |
| 宠物气泡 | 200 | `buddy-speech`（对话框） |
| Sheet | 200 | `pdf-route-sheet`（底部滑出面板） |
| 模态框 | 300 | `compare-modal`（对比浮层） |
| 拖拽遮罩 | 400 | `#drag-overlay` |

### HTMX 使用规范

- `hx-swap="outerHTML"` 会替换元素本身，导致 `id` 丢失，不可用于需要后续操作的容器。**需要保留 id 时一律用 `hx-swap="innerHTML"`**。
- 后端 HTMX 端点必须返回 **HTML**（`HTMLResponse` 或 `TemplateResponse`），不能返回 JSON——HTMX 不解析 JSON，会直接把原始文本插入 DOM。
- 需要在 HTMX 请求后触发 JS 逻辑，用 `hx-on::after-request` 而非 `hx-on::load`（前者在响应完成后执行，后者在元素首次插入时执行）。
- `hx-trigger="revealed"` 用于懒加载：元素首次出现在视口（或 CSS `:checked` 让它变为 display:flex）时自动触发请求，适合设置面板等非首屏内容。

### CSS 兄弟选择器（radio tab 切换）

本项目 tab 切换用 `<input type="radio"> + CSS :checked ~` 实现，**零 JS**：

```html
<!-- radio 必须与 .tabs、<main>、#save-bar 处于同一父元素下 -->
<div id="app">
  <input type="radio" id="tab-X" hidden>   <!-- 兄弟节点 -->
  <div class="tabs">...</div>              <!-- 兄弟节点 -->
  <main>...</main>                         <!-- 兄弟节点 -->
  <div id="save-bar">...</div>             <!-- 兄弟节点 -->
</div>
```

`~` 只能选同级后续兄弟。把 radio 放在父容器外（如 `<body>` 直接子节点），而目标元素在内部 `<div>` 里，选择器永远失效。

## 已知问题 / 注意事项

- **网络代理**：所有外部下载失败时用 `HTTP_PROXY=http://127.0.0.1:7890`
- **mlx 路径**：`libmlx.dylib` 必须在 `mlx/lib/`，`mlx.metallib` 必须同时放在 `mlx/` 和 `Contents/Frameworks/`
- **Quick Action 错误「服务输入出现问题」**：Automator 调用 `lumina pdf` 时 multiprocessing spawn 子进程重走 CLI 导致的，已通过 `set_start_method("fork")` 修复，需重新打包才能生效
- **前端模板入口**：`lumina/api/templates/index.html`（Jinja2），面板拆在 `templates/panels/*.html`，后端 HTML 片段在 `templates/*.html`。直接编辑模板文件，刷新浏览器即生效（FastAPI 每次请求都重新渲染，无需重启）。`lumina/api/static/index.html` 保留作为带内联 HTMX 的独立备份，仅供测试和离线使用，`GET /` 不再 serve 它。
- **style.css 修改方式**：改 `input.css` → 在 `lumina/` 目录下重新编译 → 提交 `style.css`。新增 tab 时在 `input.css` 里补齐六组选择器（panel hide、:checked show、nav-active 高亮、not-checked 非活跃、:hover、:hover:is(.dark *)），然后编译。
- **打包前必须清理 pyc 缓存**：PyInstaller 优先使用 `__pycache__/*.pyc` 而非源码，修改 `.py` 后若 pyc 未更新则改动不会打入包内。每次打包前运行：
  ```bash
  find lumina -name "*.pyc" -delete && find lumina -name "__pycache__" -type d -exec rm -rf {} +
  ```
  或在 `build.sh` 开头加上这两行（已加）。

- **静态文件单一来源**：启动时 `sync_static()`（`cli/utils.py`）将 bundle/源码内的 `static/` 同步到 `~/.lumina/static/`；FastAPI 的 `_static_dir()` 优先 serve `~/.lumina/static/`，fallback 到 bundle 路径。CLI 和 `.app` 两种启动方式都使用同一份最新文件，无需手动 cp。`.app` 里有三份 `index.html` 的历史问题已通过此机制消除。

- **Web UI 默认 tab**：日报 → 翻译 → 总结 → 设置（支持 URL hash 直达，如 `/#settings`）

- **备忘录（Notes.app）在 .app 包中无法读取**：根本原因是 macOS TCC 沙盒限制——打包后的 `.app` 没有 `com.apple.Notes` 权利和 Full Disk Access，无法访问 `~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite`（`shutil.copy2` 和直接 `sqlite3.connect` 均返回 `Permission denied`）。
  - `uv run lumina server` 开发模式下可正常读取（终端继承用户权限）
  - `.app` 模式静默返回空字符串，不影响其他采集器
  - 如需在 .app 中支持 Notes，需对 `.app` 进行代码签名并添加 `NSNotesUsageDescription` 权利——目前不计划实现

## 版本

当前：`v0.8.6`（`pyproject.toml` 和 `scripts/lumina_full.spec` 中的 `CFBundleShortVersionString`）
