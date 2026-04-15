# Lumina

你的 Mac 上运行的私人 AI 工具箱。不联网，不收费，不上传任何数据。

[![Platform](https://img.shields.io/badge/platform-Apple%20Silicon-black)](https://github.com/wnma3mz/Lumina)
[![License](https://img.shields.io/github/license/wnma3mz/Lumina)](LICENSE)

---

## 能做什么

### 📄 PDF 翻译 · 总结

在 Finder 右键选中 PDF，一键翻译或总结，结果直接保存到同目录。支持单文件、整个目录批量处理，以及 URL、arXiv 链接直接下载翻译。

### 📋 每日活动日报

自动采集你的 Shell 命令、Git 提交、浏览记录、备忘录、日历，每小时生成一份「今天做了什么」的简报，帮你找回上下文、追踪进展。

### 🌐 兼容浏览器翻译插件

把任意 OpenAI 兼容插件（沉浸式翻译、OpenAI Translator 等）的 API 地址填为 `http://127.0.0.1:31821/v1`，立即获得本地模型驱动的网页翻译。

---

## 快速开始

### 命令行安装

```bash
git clone https://github.com/wnma3mz/Lumina.git
cd Lumina
uv sync                        # 安装依赖（需要 uv）
uv run lumina server           # 启动服务
```

---

## 使用方式

### PDF 翻译 / 总结

选中 PDF → 右键 → **快速操作** → 翻译 / 总结

输出文件：
- `文件名-mono.pdf` — 纯中文版
- `文件名-dual.pdf` — 中英双语对照版
- `文件名-summary.txt` — 中文摘要

**命令行：**

```bash
lumina pdf paper.pdf                                # 翻译本地 PDF
lumina pdf https://arxiv.org/pdf/2104.09864        # 翻译 URL
lumina pdf ./papers/ -o ./translated               # 翻译整个目录
lumina summarize paper.pdf                         # 总结
lumina summarize paper.pdf --stdout                # 总结并打印到终端
```

---

### 每日活动日报

服务启动后自动运行。访问 `http://127.0.0.1:31821` 查看网页界面，或：

```bash
curl http://127.0.0.1:31821/v1/digest             # 查看当前日报
curl -X POST http://127.0.0.1:31821/v1/digest/refresh  # 立即重新生成
curl http://127.0.0.1:31821/v1/digest/export      # 下载完整历史（.md 文件）
```

日报每小时自动更新，默认每天 20:00 推送 macOS 通知。采集范围：

| 数据来源 | 说明 |
|---|---|
| Shell 历史 | zsh 命令记录 |
| Git 提交 | 所有扫描目录内的 git log |
| 浏览器历史 | Chrome / Firefox 最近访问 |
| 备忘录 | Notes.app 最近修改条目 |
| Markdown 笔记 | 扫描目录内 .md 文件 |
| 日历 | 今日及近期日程 |
| AI 对话 | Cursor IDE / Claude 等对话记录 |

---

### 浏览器插件接入

将插件的 API 地址设为：

```
http://127.0.0.1:31821/v1
```

模型名填 `lumina`，API Key 随便填。

手机 PWA 访问：在 Safari 打开 `http://Mac局域网IP:31821`，添加到主屏幕。

---

## 配置

配置文件位于 `~/.lumina/config.json`，不存在时使用默认值。

```json
{
  "provider": {
    "type": "local",
    "model_path": null
  },
  "digest": {
    "scan_dirs": [],
    "history_hours": 24,
    "refresh_hours": 1,
    "notify_time": "20:00"
  }
}
```

| 字段 | 说明 | 默认值 |
|---|---|---|
| `provider.type` | `local`（本地模型）或 `openai`（远程接口） | `local` |
| `provider.model_path` | 本地模型路径，`null` 时自动下载 | `null` |
| `provider.openai.base_url` | 远程 API 地址（type=openai 时必填） | — |
| `digest.scan_dirs` | 日报扫描目录，空数组时扫描 Documents / Desktop 等默认目录 | `[]` |
| `digest.history_hours` | 采集时间窗口（小时） | `24` |
| `digest.refresh_hours` | 日报更新间隔（小时） | `1` |
| `digest.notify_time` | 每日通知时间，空字符串禁用 | `"20:00"` |

---

## 开发者文档

<details>
<summary>展开查看：架构、接口、打包</summary>

### 技术栈

| 层 | 实现 |
|---|---|
| LLM 推理 | mlx-lm（Apple Silicon GPU） |
| HTTP 服务 | FastAPI + uvicorn，端口 `31821` |
| 菜单栏 | rumps（.app / `--menubar` 模式） |
| 打包 | PyInstaller，spec 在 `scripts/lumina_full.spec` |
| 包管理 | uv |

### 架构

```
┌──────────────────────────────────────────────────────┐
│       浏览器 / PWA（http://127.0.0.1:31821）          │
│       浏览器插件 / lumina pdf / lumina summarize      │
└──────────────────┬───────────────────────────────────┘
                   │
┌──────────────────▼───────────────────────────────────┐
│                  FastAPI Server                       │
│  GET  /          → Jinja2 模板渲染 Web UI（HTMX PWA）│
│  GET  /fragments/* → HTMX HTML 片段（局部刷新）      │
│  POST /v1/chat/completions  POST /v1/translate        │
│  POST /v1/pdf/*   GET /v1/digest  POST /v1/digest/refresh│
│  POST /v1/audio/transcriptions                        │
└──────────────────┬───────────────────────────────────┘
                   │
        ┌──────────▼──────────┐
        │   LocalProvider     │  ←→  OpenAIProvider（远程）
        │  mlx-lm 本地推理    │
        └─────────────────────┘
```

### HTTP 接口

```bash
# 翻译
POST /v1/translate
{"text": "The quick brown fox", "target_language": "zh"}

# 总结
POST /v1/summarize
{"text": "Long article..."}

# Chat（OpenAI 兼容）
POST /v1/chat/completions
{"model": "lumina", "messages": [{"role": "user", "content": "你好"}]}

# 语音转文字
POST /v1/audio/transcriptions
-F "file=@audio.wav" -F "language=zh"

# 日报
GET  /v1/digest
POST /v1/digest/refresh
GET  /v1/digest/export
```

### 版本说明

| 版本 | 说明 |
|------|------|
| **Full**（默认） | 首次启动自动下载本地模型（Qwen3.5-0.8B-4bit，约 622MB），无需联网推理 |
| **Lite** | 不含模型，把请求转发到你自己的外部 OpenAI 兼容 API |

### 打包

```bash
bash scripts/build_full.sh      # 构建 Lumina.app
bash scripts/install_quick_action.sh  # 安装 Finder Quick Action
```

### 目录结构

```
lumina/
  main.py              # CLI 入口
  config.py            # 配置加载
  api/
    server.py          # FastAPI 路由（含 PWA manifest、CORS）
    templates/         # Jinja2 模板（Web UI 主页 + HTMX 片段）
      index.html       # 主页面（PWA，内联 HTMX）
      panels/          # 各 tab 面板初始 HTML
    routers/
      fragments.py     # HTMX HTML 片段路由（/fragments/*）
    static/
      style.css        # 样式（含 bento-card 设计系统）
  providers/
    local.py           # mlx-lm 本地推理（Continuous Batching）
    openai.py          # OpenAI 兼容远程接口
  digest/
    core.py            # 日报生成调度
    collectors/        # 数据采集（shell / git / 浏览器 / 备忘录 / 日历 / AI）
  asr/                 # Whisper 语音转文字
  pdf_translate.py     # lumina pdf 实现
  pdf_summarize.py     # lumina summarize 实现
scripts/
  build_full.sh        # PyInstaller 打包
  install_quick_action.sh
```

</details>
