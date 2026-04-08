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
  main.py              # CLI 入口，所有子命令在此注册
  config.py / config.json  # 配置加载，端口默认 31821
  api/
    server.py          # FastAPI 路由
    static/index.html  # 单页 Web UI（无构建步骤，直接编辑）
  providers/
    local.py           # mlx-lm 本地推理，含 Continuous Batching
    openai.py          # OpenAI 兼容远程接口
  engine/llm.py        # 上层封装，提供 stream / chat 接口
  digest/
    core.py            # 日报生成逻辑
    collectors.py      # 活动数据采集（shell history, git, 剪贴板等）
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

### Continuous Batching（`providers/local.py`）
- `_RequestSlot`：每个请求独立的 `asyncio.Queue` 传 token，无共享状态
- 调度器：Phase 1 prefill 新请求，Phase 2 只推进 **prefill 前已存在** 的 slot（快照 `existing_decode`），防止首 token 被覆盖
- EOS 检测：mlx-lm `generate_step` 不自动停，手动检测 token id 248046（`<|im_end|>`）

### 日报定时生成
- `.app` 模式：`rumps.timer(3600)` 在 `_run_with_menubar()` 中触发
- 命令行模式：`_start_digest_timer(llm)` 用 `threading.Timer` 循环，行为一致
- 前端：5 分钟 `setInterval` 轮询 `/v1/digest`，比对 `generated_at` 只在内容变化时重渲染

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

## 已知问题 / 注意事项

- **网络代理**：所有外部下载失败时用 `HTTP_PROXY=http://127.0.0.1:7890`
- **mlx 路径**：`libmlx.dylib` 必须在 `mlx/lib/`，`mlx.metallib` 必须同时放在 `mlx/` 和 `Contents/Frameworks/`
- **Quick Action 错误「服务输入出现问题」**：Automator 调用 `lumina pdf` 时 multiprocessing spawn 子进程重走 CLI 导致的，已通过 `set_start_method("fork")` 修复，需重新打包才能生效
- **前端无构建步骤**：`lumina/api/static/index.html` 直接编辑，改完立即生效（开发模式下刷新页面即可）
- **Web UI 默认 tab**：日报 → 翻译 → 总结

## 版本

当前：`v0.2.0`（`pyproject.toml` 和 `scripts/lumina_full.spec` 中的 `CFBundleShortVersionString`）
