# Lumina

本地运行的 AI 工具箱。不联网，不收费，不上传。

[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-black)](https://github.com/wnma3mz/Lumina)
[![License](https://img.shields.io/github/license/wnma3mz/Lumina)](LICENSE)

<div align="center">
  <img src="assets/showcase.jpg" width="850" style="border-radius: 20px; box-shadow: 0 20px 50px rgba(0,0,0,0.1);">
</div>

## 能做什么

### 📋 活动回顾

自动采集你的 Shell 命令、Git 提交、浏览记录、备忘录、日历，定期生成简报，帮你找回上下文、追踪进展。

### 🌐 兼容浏览器翻译插件

把任意 OpenAI 兼容插件（沉浸式翻译、OpenAI Translator 等）的 API 地址填为 `http://127.0.0.1:31821/v1/chat/completions`，立即获得本地模型驱动的网页翻译。

### 📄 PDF 翻译 · 总结

本地文件翻译或总结，结果直接保存到同目录。支持单文件、整个目录批量处理，以及 URL、arXiv 链接直接下载翻译。

### 📷 图片理解

支持图片文件和链接、整个目录批量处理，可做 OCR 提取，也可以生成简洁 Caption。

### 👾 Lumina Buddy

一个居住在 Web 终端顶部的 ASCII 字符伙伴。它是装饰，也是你当日数字生活的「镜像」。

---

## 快速开始

### 命令行安装

```bash
git clone https://github.com/wnma3mz/Lumina.git
cd Lumina
uv sync                        # 安装依赖（需要 uv）
uv run lumina server           # 启动服务
```

### 平台安装脚本

- macOS：`uv sync && uv run lumina server`
- Linux：`bash scripts/install_linux.sh`
- Windows：`powershell -ExecutionPolicy Bypass -File scripts/install_windows.ps1`


<details>
<summary>展开查看：平台入口集成、平台验证手册、支持矩阵</summary>

### 平台入口集成

- Linux 文件管理器入口：`bash scripts/install_linux_desktop_entry.sh`
- Windows 右键 `Send to`：`powershell -ExecutionPolicy Bypass -File scripts/install_windows_sendto.ps1`
- 通用文件动作脚本：`uv run python scripts/lumina_file_action.py <translate|summarize|polish> <files...>`

### 平台验证手册

- Windows / Linux 手动验收清单：[docs/windows-linux-validation.md](docs/windows-linux-validation.md)

### 支持矩阵

| 能力 | macOS | Windows | Linux |
|---|---|---|---|
| Web UI / HTTP API | ✅ | ✅ | ✅ |
| 本地模型默认后端 | MLX | llama.cpp | llama.cpp |
| ASR 默认后端 | mlx-whisper | faster-whisper | faster-whisper |
| PTT 录音转写粘贴 | ✅ | ✅ | ✅（依赖 `xdotool` / `ydotool` 等桌面工具时体验更完整） |
| 结果弹窗 | NSPanel | pywebview | pywebview |
| 系统通知 | ✅ | ✅ | ✅（依赖 `notify-send`） |
| Finder Quick Action / 系统服务 | ✅ | 入口形态不同 | 入口形态不同 |
| 日报 Apple 专属数据源（Notes / Calendar / Safari） | ✅ | — | — |

</details>

---

## 使用方式

### PDF 翻译 / 总结

macOS：选中 PDF → 右键 → **快速操作** → 翻译 / 总结  
Windows / Linux：可直接使用 Web UI 或命令行完成同样的翻译 / 总结流程

输出文件：
- `文件名-mono.pdf` — 纯中文版
- `文件名-dual.pdf` — 中英双语对照版
- `文件名-summary.txt` — 中文摘要

**命令行：**

```bash
lumina pdf paper.pdf                                # 翻译本地 PDF
lumina pdf https://arxiv.org/pdf/2104.09864        # 翻译 URL
lumina pdf ./papers/ -o ./translated               # 翻译整个目录
```

---

### 活动回顾

服务启动后自动运行。访问 `http://127.0.0.1:31821` 查看网页界面，或：

日报每小时自动更新，默认每天 20:00 推送系统通知。采集范围：

| 数据来源 | 说明 |
|---|---|
| Shell 历史 | zsh / bash / fish / PowerShell 历史 |
| Git 提交 | 所有扫描目录内的 git log |
| 浏览器历史 | Chrome / Edge / Brave / Chromium / Firefox / Safari（按平台可用） |
| 备忘录 | Notes.app 最近修改条目（macOS） |
| Markdown 笔记 | 扫描目录内 .md 文件 |
| 日历 | 今日及近期日程（macOS） |
| AI 对话 | Cursor IDE / Claude 等对话记录 |

---

## 开发者文档

[架构设计亮点 →](docs/architecture.md)（日报采集、推理引擎、配置热更新、Web UI）

<details>
<summary>展开查看：HTTP 接口、版本说明</summary>

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

# Chat + 图片输入（支持 image_url / data URL）
POST /v1/chat/completions
{
  "model": "lumina",
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "请描述这张图"},
        {"type": "image_url", "image_url": {"url": "https://example.com/demo.png"}}
      ]
    }
  ]
}

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
| **Full**（默认） | 首次启动按平台自动下载本地模型：macOS 下载 MLX 模型，Windows / Linux 下载 GGUF 模型，无需联网推理 |
| **Lite** | 不含模型，把请求转发到你自己的外部 OpenAI 兼容 API |

</details>
