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
    routers/           # 路由模块（pdf/chat/config/digest/audio/text/fragments）
      fragments.py     # HTMX HTML 片段路由（Jinja2 渲染，返回可直接插入 DOM 的片段）
    templates/         # Jinja2 模板（GET / 由此渲染，无静态 HTML）
      index.html       # 主页面（HTMX + 纯 CSS tab 切换，内联 htmx.min.js，PWA meta）
      panels/          # 各 tab 面板的初始 HTML（digest/document/lab/settings）
      config_form.html # 设置表单片段（/fragments/config 返回）
      digest_content.html  # 日报时间轴片段（/fragments/digest 返回）
      digest_sources.html  # 数据来源图标行片段
      pdf_progress.html / pdf_result.html / pdf_error.html  # PDF 任务状态片段
      report_content.html  # 日报/周报/月报内容片段
    static/
      style.css        # Tailwind 编译产物（直接提交，无运行时构建）；含 bento-card 设计系统
      logo.svg         # 应用图标
      index.html       # 独立备份（含内联 HTMX），仅供离线测试，GET / 不再 serve 此文件
  providers/
    __init__.py        # 懒加载：LocalProvider / OpenAIProvider 按需 import（见下节）
    local.py           # mlx-lm 本地推理，含 Continuous Batching
    mlx_loader.py      # Layer 0：模型路径解析 + 加载 + BatchGenerator 初始化
    mlx_prompt.py      # Layer 1：chat_template 渲染 + tokenize + system prefix 提取
    openai.py          # OpenAI 兼容远程接口
  services/
    pdf.py             # PDF 业务服务层：PdfJobManager / fetch_pdf_url / stream_pdf_summary
  engine/llm.py        # 上层封装，提供 stream / chat 接口
  digest/
    core.py            # 日报生成逻辑（含冷却检查）
    collectors/        # 活动数据采集插件目录（shell/git/browser/notes/markdown/ai 等）
  asr/                 # 语音转文字（mlx-whisper）
  pdf_translate.py     # lumina pdf 子命令实现
  pdf_summarize.py     # lumina summarize 子命令实现
  text_polish.py       # lumina polish 子命令实现
  ptt.py               # Push-to-Talk 守护进程
  watcher.py           # 目录监听自动翻译
scripts/
  build_full.sh        # PyInstaller 打包脚本
  lumina_full.spec     # PyInstaller spec（固定路径，保证缓存命中）
  install_quick_action.sh  # 安装 Finder 右键 workflow
```

## 运行方式

```bash
uv run lumina server          # 开发模式启动服务
uv run lumina server --ptt    # 同时启动 PTT 热键守护（长按 F5 录音）
bash scripts/build_full.sh    # 打包为 Lumina.app
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

### Digest 并发安全（`digest/core.py`）

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

### 配置 Web UI（`/v1/config`）
- **GET `/v1/config`**：返回完整运行时配置（来自 `get_config()` singleton）
- **PATCH `/v1/config`**：部分更新，原子写回 `~/.lumina/config.json`，并热重载部分字段：
  - `digest.*` → 调用 `configure()` 重新初始化 DigestConfig singleton（立即生效）
  - `system_prompts.*` → 原地 mutate `app.state.llm._system_prompts` dict（立即生效）
  - `ptt.*` → 仅写 config.json，依赖 PTT 文件 mtime watcher 自动重载
  - `provider.*`、`whisper_model`、`host`、`port`、`log_level` → 写 config.json，响应附带 `"restart_required": true`，前端提示重启
- **并发安全**：写操作通过模块级 `asyncio.Lock` 保护；临时文件 + `rename()` 原子写入

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

## Git 操作规范

- **禁止在未经用户明确许可的情况下 push 到远端**（包括 `git push`、`gh release`）
- commit 可以随时做，push / release 必须等用户说「可以 push」或「发包」

## 前端开发准则

### 技术栈约定

- **禁止直接手动修改 `style.css`**：`style.css` 是 Tailwind CLI 自动编译的产物。任何手动修改都会在下次构建时被覆盖。
- **样式源头是 `input.css`**：所有的样式修改、新增原子类或自定义组件，必须在 `lumina/api/static/input.css` 中进行。
- **无运行时构建步骤**：`node_modules/`、`package.json`、`tailwind.config.js` 在 `.gitignore` 中，不提交。`input.css`（Tailwind 源文件）已提交到 `lumina/api/static/input.css`，不需要在本机执行 npm/tailwind 命令即可运行项目。需要更新 `style.css` 时，在本地执行 `npx tailwindcss -i lumina/api/static/input.css -o lumina/api/static/style.css`，然后提交生成的 `style.css`。
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

按钮用 Tailwind `hover:opacity-80 active:scale-95 transition-all` 组合，或在 `style.css` 里定义语义组件类。

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
- **style.css 修改方式**：若只改颜色/自定义组件，可直接编辑 `lumina/api/static/style.css`。若需新增 Tailwind utility class，在本地用 `npx tailwindcss -i lumina/api/static/input.css -o lumina/api/static/style.css` 重新编译，再提交 `style.css`（和必要时的 `input.css`）。
- **打包前必须清理 pyc 缓存**：PyInstaller 优先使用 `__pycache__/*.pyc` 而非源码，修改 `.py` 后若 pyc 未更新则改动不会打入包内。每次打包前运行：
  ```bash
  find lumina -name "*.pyc" -delete && find lumina -name "__pycache__" -type d -exec rm -rf {} +
  ```
  或在 `build_full.sh` 开头加上这两行（已加）。

- **静态文件单一来源**：启动时 `sync_static()`（`cli/utils.py`）将 bundle/源码内的 `static/` 同步到 `~/.lumina/static/`；FastAPI 的 `_static_dir()` 优先 serve `~/.lumina/static/`，fallback 到 bundle 路径。CLI 和 `.app` 两种启动方式都使用同一份最新文件，无需手动 cp。`.app` 里有三份 `index.html` 的历史问题已通过此机制消除。

- **Web UI 默认 tab**：日报 → 翻译 → 总结 → 设置（支持 URL hash 直达，如 `/#settings`）

- **备忘录（Notes.app）在 .app 包中无法读取**：根本原因是 macOS TCC 沙盒限制——打包后的 `.app` 没有 `com.apple.Notes` 权利和 Full Disk Access，无法访问 `~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite`（`shutil.copy2` 和直接 `sqlite3.connect` 均返回 `Permission denied`）。
  - `uv run lumina server` 开发模式下可正常读取（终端继承用户权限）
  - `.app` 模式静默返回空字符串，不影响其他采集器
  - 如需在 .app 中支持 Notes，需对 `.app` 进行代码签名并添加 `NSNotesUsageDescription` 权利——目前不计划实现

## 版本

当前：`v0.8.3`（`pyproject.toml` 和 `scripts/lumina_full.spec` 中的 `CFBundleShortVersionString`）
