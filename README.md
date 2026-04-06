# Lumina

你的电脑里有一个私人 AI 助手，不联网，不收费，不上传任何数据。

目前支持三件事：**翻译**、**总结**、**语音转文字**。未来可以扩展更多。

---

## 安装

打开终端，在项目目录下运行：

```bash
bash scripts/install_full.sh
```

安装完成后，运行以下命令启动服务：

```bash
~/.lumina/lumina server
```

看到下面的提示，说明已经就绪：

```
  Lumina Full 已就绪
  服务地址：http://127.0.0.1:31821
```

> 后续可以把 `~/.lumina` 加入 PATH，直接用 `lumina server` 启动。详见安装完成后的提示。

---

## 有什么用

### 翻译 PDF 论文 / 文档

在 Finder 中选中 PDF 文件，右键 → **快速操作** → **用 Lumina 翻译 PDF**。

翻译完成后，同目录会出现两个文件：

- `文件名-mono.pdf` — 纯中文版
- `文件名-dual.pdf` — 中英双语对照版

系统右上角会弹出通知，点击即可跳到结果文件。

---

### 总结 PDF 内容

选中 PDF → 右键 → **快速操作** → **用 Lumina 总结 PDF**。

几秒后在同目录生成 `文件名-summary.txt`，是一段中文摘要。

---

### 手机 PWA（无需安装 App）

在手机 Safari 中访问 Lumina，可上传 PDF 或粘贴链接，直接翻译或总结。

用以下方式启动，服务会自动打印出手机可访问的局域网地址：

```bash
LUMINA_HOST=0.0.0.0 lumina server
```

启动后终端会显示：

```
  本机访问：http://127.0.0.1:31821
  局域网访问：http://192.168.x.x:31821   ← 手机打开这个
  添加到主屏幕即可像 App 一样使用
```

手机和 Mac 需在同一 Wi-Fi 下。

---

### 接入浏览器翻译插件（完全本地、免费）

沉浸式翻译、OpenAI Translator 等插件支持接入自定义 API。

把插件的 API 地址填为：

```
http://127.0.0.1:31821/v1
```

模型名填 `lumina`，API Key 随便填（比如 `lumina`）。

这样网页翻译**完全在本地跑**，不联网、不消耗 API 额度、内容不上传到任何服务器。

---

### 语音转文字

对着麦克风说话，Lumina 把内容转成文字。适合配合 Raycast、Alfred 等启动器做快捷键录音。

---

## 首次使用前

每次开机后，需要先启动服务：

```bash
lumina server
```

如果想开机自动启动，运行一次：

```bash
launchctl load ~/Library/LaunchAgents/com.lumina.server.plist
```

---

## 安装右键菜单

翻译和总结的右键菜单需要单独安装一次：

```bash
bash scripts/install_quick_action.sh
```

---

## 技术细节

<details>
<summary>展开查看：架构、配置、HTTP 接口文档</summary>

### 版本说明

| 版本 | 说明 |
|------|------|
| **Full**（默认） | 内置本地模型（Qwen3.5-0.8B-4bit，约 622MB），无需联网，直接使用 |
| **Lite** | 不含模型，把请求转发到你自己的外部 OpenAI 兼容 API |

安装脚本：`bash scripts/install_full.sh` / `bash scripts/install_lite.sh`

---

### 架构

```
┌──────────────────────────────────────────────────┐
│               HTTP Client                         │
│  (浏览器插件 / lumina pdf / lumina summarize)      │
└──────────────────┬───────────────────────────────┘
                   │ OpenAI 兼容 API
┌──────────────────▼───────────────────────────────┐
│               FastAPI Server                      │
│  /v1/chat/completions  /v1/translate              │
│  /v1/summarize  /v1/audio/transcriptions          │
└──────────────────┬───────────────────────────────┘
                   │
        ┌──────────▼──────────┐
        │   LocalProvider     │  ←→  OpenAIProvider（Lite 版）
        │  mlx-lm 本地推理    │      任意 OpenAI 兼容 API
        └─────────────────────┘
```

Provider 切换：设置环境变量 `LUMINA_PROVIDER_TYPE=openai`，或修改 `~/.lumina/config.json`。

---

### 配置文件

位于 `~/.lumina/config.json`，环境变量优先级更高。

```json
{
  "provider": { "type": "local", "model_path": null },
  "whisper_model": "mlx-community/whisper-tiny-mlx-4bit",
  "host": "127.0.0.1",
  "port": 31821,
  "log_level": "INFO",
  "system_prompts": {
    "translate_to_zh": "...",
    "translate_to_en": "...",
    "summarize": "...",
    "chat": "..."
  }
}
```

| 环境变量 | 说明 | 默认 |
|----------|------|------|
| `LUMINA_PROVIDER_TYPE` | `local` 或 `openai` | `local` |
| `LUMINA_MODEL_PATH` | 本地模型路径 | 内置 models/ |
| `LUMINA_OPENAI_BASE_URL` | 外部 API 地址 | — |
| `LUMINA_OPENAI_API_KEY` | API 密钥 | `lumina` |
| `LUMINA_OPENAI_MODEL` | 模型名称 | `lumina` |
| `LUMINA_HOST` | 监听地址 | `127.0.0.1` |
| `LUMINA_PORT` | 监听端口 | `31821` |

---

### HTTP 接口

**翻译**
```bash
POST /v1/translate
{"text": "The quick brown fox", "target_language": "zh"}
```

**摘要**
```bash
POST /v1/summarize
{"text": "Long article..."}
```

**Chat（OpenAI 兼容）**
```bash
POST /v1/chat/completions
{"model": "lumina", "messages": [{"role": "user", "content": "你好"}]}
```

**语音转文字（上传文件）**
```bash
curl -X POST http://127.0.0.1:31821/v1/audio/transcriptions \
  -F "file=@audio.wav" -F "language=zh"
```

**语音录制（按键触发）**
```bash
# 开始录音
curl -X POST http://127.0.0.1:31821/v1/audio/record/start
# 停止并获取文字
curl -X POST http://127.0.0.1:31821/v1/audio/record/stop \
  -d '{"session_id":"abc123","language":"zh"}'
```

---

### 命令行

```bash
lumina server                                      # 启动服务
lumina pdf paper.pdf                               # 翻译本地 PDF
lumina pdf https://arxiv.org/pdf/2104.09864        # 翻译 URL（自动下载）
lumina pdf ./papers/ -o ./translated               # 翻译整个目录
lumina summarize paper.pdf                         # 总结本地 PDF
lumina summarize https://arxiv.org/pdf/2104.09864  # 总结 URL
lumina summarize paper.pdf --stdout                # 总结并打印到终端
```

---

### 目录结构

```
lumina/
├── lumina/
│   ├── config.json          # 默认配置
│   ├── config.py            # 配置加载（env > ~/.lumina/config.json > 内置）
│   ├── main.py              # CLI 入口
│   ├── pdf_translate.py     # PDF 翻译（pdf2zh）
│   ├── pdf_summarize.py     # PDF 摘要（pymupdf + /v1/summarize）
│   ├── providers/           # LocalProvider / OpenAIProvider
│   ├── engine/llm.py        # 任务路由层
│   ├── asr/                 # 录音 + Whisper 转写
│   └── api/server.py        # FastAPI 路由
├── models/
│   └── qwen3.5-0.8b-4bit/  # 内置模型（622MB）
├── scripts/
│   ├── install_full.sh
│   ├── install_lite.sh
│   ├── build_full.sh        # 打包 Lumina.app
│   ├── build_lite.sh        # 打包 Lumina Lite.app
│   └── install_quick_action.sh
└── pyproject.toml
```

</details>
