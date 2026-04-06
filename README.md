# Lumina

你的电脑里有一个私人 AI 助手，不联网，不收费，不上传任何数据。

**翻译 PDF · 总结论文 · 语音转文字 · 接入浏览器插件**

---

## 下载安装

**[→ 点击前往 Releases 下载最新版](https://github.com/wnma3mz/Lumina/releases/latest)**

1. 下载 `Lumina.zip`
2. 解压，把 `Lumina.app` 拖到「应用程序」文件夹
3. 双击启动

首次启动会自动下载模型（约 622MB），下载期间右上角会有进度通知。下载完成后弹出「Lumina 已就绪」通知，即可使用。

> **首次打开提示"无法验证开发者"？**
> 在「应用程序」里右键点击 `Lumina.app` → 选「打开」→ 再点「打开」，之后就可以直接双击了。

---

## 有什么用

### 翻译 PDF 论文

在 Finder 中选中 PDF → 右键 → **快速操作** → **用 Lumina 翻译 PDF**

完成后同目录出现两个文件：
- `文件名-mono.pdf` — 纯中文版
- `文件名-dual.pdf` — 中英双语对照版

---

### 总结 PDF 内容

选中 PDF → 右键 → **快速操作** → **用 Lumina 总结 PDF**

同目录生成 `文件名-summary.txt`，是一段中文摘要。

---

### 接入浏览器翻译插件

沉浸式翻译、OpenAI Translator 等插件，把 API 地址填为：

```
http://127.0.0.1:31821/v1
```

模型名填 `lumina`，API Key 随便填。网页翻译**完全本地运行**，不联网、不消耗额度。

---

### 手机使用（PWA）

手机 Safari 访问 `http://Mac局域网IP:31821`，可上传 PDF 或粘贴链接翻译、总结。添加到主屏幕后像 App 一样使用。

Mac 局域网 IP 在 Lumina 启动后的通知里会显示（需开放局域网访问，见下方技术细节）。

---

### 语音转文字

对着麦克风说话，Lumina 把内容转成文字。适合配合 Raycast、Alfred 等做快捷键录音。

---

## 安装右键菜单

右键翻译 / 总结 / 润色功能需单独安装一次。打开终端运行：

```bash
bash /Applications/Lumina.app/Contents/MacOS/scripts/install_quick_action.sh
```

---

## 退出 / 重启

点击菜单栏的 **Lumina** 图标，选择「退出 Lumina」或「重启服务」。

---

## 技术细节

<details>
<summary>展开查看：架构、配置、HTTP 接口、命令行</summary>

### 版本说明

| 版本 | 说明 |
|------|------|
| **Full**（默认） | 首次启动自动下载本地模型（Qwen3.5-0.8B-4bit，约 622MB），无需联网推理 |
| **Lite** | 不含模型，把请求转发到你自己的外部 OpenAI 兼容 API |

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

---

### 配置文件

位于 `~/.lumina/config.json`，环境变量优先级更高。

| 环境变量 | 说明 | 默认 |
|----------|------|------|
| `LUMINA_PROVIDER_TYPE` | `local` 或 `openai` | `local` |
| `LUMINA_MODEL_PATH` | 本地模型路径 | `~/.lumina/models/qwen3.5-0.8b-4bit` |
| `LUMINA_OPENAI_BASE_URL` | 外部 API 地址 | — |
| `LUMINA_OPENAI_API_KEY` | API 密钥 | `lumina` |
| `LUMINA_OPENAI_MODEL` | 模型名称 | `lumina` |
| `LUMINA_HOST` | 监听地址 | `127.0.0.1` |
| `LUMINA_PORT` | 监听端口 | `31821` |

开放局域网访问（供手机 PWA 使用）：

```bash
LUMINA_HOST=0.0.0.0 lumina server
```

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

**语音转文字**
```bash
curl -X POST http://127.0.0.1:31821/v1/audio/transcriptions \
  -F "file=@audio.wav" -F "language=zh"
```

---

### 命令行（开发者 / 从源码安装）

```bash
lumina server                                      # 启动服务
lumina stop                                        # 停止服务
lumina restart                                     # 重启服务
lumina pdf paper.pdf                               # 翻译本地 PDF
lumina pdf https://arxiv.org/pdf/2104.09864        # 翻译 URL（自动下载）
lumina pdf ./papers/ -o ./translated               # 翻译整个目录
lumina summarize paper.pdf                         # 总结本地 PDF
lumina summarize https://arxiv.org/pdf/2104.09864  # 总结 URL
lumina summarize paper.pdf --stdout                # 总结并打印到终端
```

从源码安装：

```bash
git clone https://github.com/wnma3mz/Lumina.git
cd Lumina
bash scripts/install_full.sh
```

</details>
