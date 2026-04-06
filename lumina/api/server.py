"""
Lumina HTTP 服务

提供 OpenAI 兼容接口 + 语音录制转写接口 + PWA 前端。
"""
import asyncio
import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from lumina.asr.recorder import AudioRecorder
from lumina.asr.transcriber import Transcriber
from lumina.engine.llm import LLMEngine
from lumina.api.protocol import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionStreamResponse,
    ChatCompletionChoice,
    ChatCompletionStreamChoice,
    ChatCompletionStreamDelta,
    ChatMessage,
    ModelCard,
    ModelList,
    RecordStartResponse,
    RecordStopRequest,
    PolishRequest,
    SummarizeRequest,
    TextResponse,
    TranscriptionResponse,
    TranslateRequest,
    UsageInfo,
    random_uuid,
)

# 全局录音 session：session_id -> (recorder, stop_event, task)
_record_sessions: dict[str, tuple[AudioRecorder, asyncio.Event, asyncio.Task]] = {}

# PDF 翻译 job 存储：job_id -> {"status": str, "mono": path, "dual": path, "dir": tmpdir, "ts": float}
_pdf_jobs: dict[str, dict] = {}

# ── PWA 前端 HTML ─────────────────────────────────────────────────────────────

_PWA_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Lumina">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1c1c1e" media="(prefers-color-scheme: dark)">
<link rel="manifest" href="/manifest.json">
<title>Lumina</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #f2f2f7;
    --card: #ffffff;
    --label: #3c3c43;
    --sub: #8e8e93;
    --accent: #007aff;
    --accent-dk: #0062cc;
    --danger: #ff3b30;
    --success: #34c759;
    --border: rgba(60,60,67,.12);
    --shadow: 0 2px 12px rgba(0,0,0,.08);
    --radius: 16px;
    --font: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1c1c1e; --card: #2c2c2e; --label: #ebebf5;
      --sub: #8e8e93; --border: rgba(84,84,88,.65);
    }
  }

  html, body { height: 100%; background: var(--bg); font-family: var(--font); color: var(--label); -webkit-text-size-adjust: 100%; }

  #app { display: flex; flex-direction: column; height: 100%; max-width: 560px; margin: 0 auto; }

  /* ── Header ── */
  header { padding: 56px 20px 0; }
  header h1 { font-size: 34px; font-weight: 700; letter-spacing: .4px; }
  header p  { margin-top: 4px; font-size: 15px; color: var(--sub); }

  /* ── Tab bar ── */
  .tabs { display: flex; gap: 8px; padding: 16px 20px 0; }
  .tab-btn {
    flex: 1; padding: 10px 0; border-radius: 12px; border: none; cursor: pointer;
    font-size: 15px; font-weight: 600; font-family: var(--font);
    background: var(--card); color: var(--sub); transition: all .18s;
  }
  .tab-btn.active { background: var(--accent); color: #fff; }

  /* ── Main content ── */
  main { flex: 1; overflow-y: auto; padding: 16px 20px; display: flex; flex-direction: column; gap: 14px; }

  .panel { display: none; flex-direction: column; gap: 14px; }
  .panel.active { display: flex; }

  /* ── Cards ── */
  .card { background: var(--card); border-radius: var(--radius); padding: 18px; box-shadow: var(--shadow); }
  .card label { display: block; font-size: 13px; font-weight: 600; color: var(--sub); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 10px; }

  /* ── Drop zone ── */
  .drop-zone {
    border: 2px dashed var(--border); border-radius: 12px; padding: 28px 20px;
    text-align: center; cursor: pointer; transition: border-color .18s, background .18s;
    position: relative;
  }
  .drop-zone.over { border-color: var(--accent); background: rgba(0,122,255,.06); }
  .drop-zone input[type=file] { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
  .drop-zone .icon { font-size: 36px; margin-bottom: 8px; }
  .drop-zone .hint { font-size: 15px; color: var(--sub); }
  .drop-zone .hint span { color: var(--accent); font-weight: 600; }
  .drop-zone .filename { margin-top: 8px; font-size: 14px; font-weight: 500; color: var(--label); word-break: break-all; }

  /* ── URL input ── */
  .url-row { display: flex; gap: 8px; align-items: center; }
  .url-row input {
    flex: 1; padding: 12px 14px; border-radius: 10px; border: 1.5px solid var(--border);
    font-size: 15px; font-family: var(--font); color: var(--label); background: var(--bg);
    outline: none; transition: border-color .18s;
  }
  .url-row input:focus { border-color: var(--accent); }
  .url-row input::placeholder { color: var(--sub); }

  /* ── Options row ── */
  .options { display: flex; gap: 8px; flex-wrap: wrap; }
  .opt-btn {
    padding: 8px 16px; border-radius: 20px; border: 1.5px solid var(--border);
    font-size: 14px; font-weight: 500; font-family: var(--font); cursor: pointer;
    background: var(--bg); color: var(--label); transition: all .15s;
  }
  .opt-btn.selected { border-color: var(--accent); background: rgba(0,122,255,.1); color: var(--accent); }

  /* ── Action button ── */
  .btn-primary {
    width: 100%; padding: 16px; border-radius: var(--radius); border: none; cursor: pointer;
    background: var(--accent); color: #fff; font-size: 17px; font-weight: 600;
    font-family: var(--font); transition: background .15s, opacity .15s;
  }
  .btn-primary:active { background: var(--accent-dk); }
  .btn-primary:disabled { opacity: .45; cursor: not-allowed; }

  /* ── Progress ── */
  .progress-wrap { display: none; flex-direction: column; gap: 8px; }
  .progress-wrap.visible { display: flex; }
  .progress-bar-track { height: 6px; border-radius: 3px; background: var(--border); overflow: hidden; }
  .progress-bar-fill { height: 100%; border-radius: 3px; background: var(--accent); transition: width .4s; width: 0%; }
  .progress-label { font-size: 13px; color: var(--sub); }

  /* ── Result area ── */
  .result-area { display: none; flex-direction: column; gap: 10px; }
  .result-area.visible { display: flex; }
  .result-text {
    background: var(--bg); border-radius: 12px; padding: 14px;
    font-size: 15px; line-height: 1.65; white-space: pre-wrap; word-break: break-word;
    max-height: 340px; overflow-y: auto;
  }
  .result-text.streaming::after { content: "▋"; animation: blink .7s step-start infinite; color: var(--accent); }
  @keyframes blink { 50% { opacity: 0; } }

  /* ── Download buttons ── */
  .dl-row { display: flex; gap: 10px; }
  .btn-dl {
    flex: 1; padding: 13px; border-radius: 12px; border: 1.5px solid var(--accent);
    background: transparent; color: var(--accent); font-size: 15px; font-weight: 600;
    font-family: var(--font); cursor: pointer; text-align: center; text-decoration: none;
    display: flex; align-items: center; justify-content: center; gap: 6px;
    transition: background .15s;
  }
  .btn-dl:active { background: rgba(0,122,255,.08); }

  /* ── Error ── */
  .error-msg { color: var(--danger); font-size: 14px; display: none; padding: 4px 0; }
  .error-msg.visible { display: block; }

  /* ── Bottom safe area ── */
  .safe-bottom { height: env(safe-area-inset-bottom, 16px); flex-shrink: 0; }
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>Lumina</h1>
    <p>本地 AI · 隐私优先</p>
  </header>

  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('translate')">📄 翻译</button>
    <button class="tab-btn" onclick="switchTab('summarize')">📝 总结</button>
  </div>

  <main>
    <!-- ── 翻译 panel ── -->
    <div id="panel-translate" class="panel active">
      <div class="card">
        <label>上传 PDF</label>
        <div class="drop-zone" id="dz-translate" ondragover="onDrag(event,'translate')" ondragleave="offDrag('translate')" ondrop="onDrop(event,'translate')">
          <input type="file" accept=".pdf" onchange="onFile(event,'translate')">
          <div class="icon">📂</div>
          <div class="hint">点击选择或<span>拖入 PDF</span></div>
          <div class="filename" id="fn-translate"></div>
        </div>
      </div>

      <div class="card">
        <label>或输入 PDF 链接</label>
        <div class="url-row">
          <input type="url" id="url-translate" placeholder="https://example.com/paper.pdf">
        </div>
      </div>

      <div class="card">
        <label>翻译方向</label>
        <div class="options">
          <button class="opt-btn selected" data-panel="translate" data-val="zh" onclick="selectOpt(this,'translate')">英 → 中</button>
          <button class="opt-btn" data-panel="translate" data-val="en" onclick="selectOpt(this,'translate')">中 → 英</button>
        </div>
      </div>

      <div class="error-msg" id="err-translate"></div>

      <div class="progress-wrap" id="prog-translate">
        <div class="progress-bar-track"><div class="progress-bar-fill" id="prog-fill-translate"></div></div>
        <div class="progress-label" id="prog-label-translate">正在翻译，请稍候…</div>
      </div>

      <div class="result-area" id="dl-translate">
        <div class="dl-row">
          <a class="btn-dl" id="dl-mono" href="#" download>⬇ 中文版</a>
          <a class="btn-dl" id="dl-dual" href="#" download>⬇ 双语版</a>
        </div>
      </div>

      <button class="btn-primary" id="btn-translate" onclick="runTranslate()">开始翻译</button>
    </div>

    <!-- ── 总结 panel ── -->
    <div id="panel-summarize" class="panel">
      <div class="card">
        <label>上传 PDF</label>
        <div class="drop-zone" id="dz-summarize" ondragover="onDrag(event,'summarize')" ondragleave="offDrag('summarize')" ondrop="onDrop(event,'summarize')">
          <input type="file" accept=".pdf" onchange="onFile(event,'summarize')">
          <div class="icon">📂</div>
          <div class="hint">点击选择或<span>拖入 PDF</span></div>
          <div class="filename" id="fn-summarize"></div>
        </div>
      </div>

      <div class="card">
        <label>或输入 PDF 链接</label>
        <div class="url-row">
          <input type="url" id="url-summarize" placeholder="https://example.com/paper.pdf">
        </div>
      </div>

      <div class="error-msg" id="err-summarize"></div>

      <div class="progress-wrap" id="prog-summarize">
        <div class="progress-bar-track"><div class="progress-bar-fill" id="prog-fill-summarize"></div></div>
        <div class="progress-label" id="prog-label-summarize">正在提取文字…</div>
      </div>

      <div class="result-area" id="res-summarize">
        <div class="result-text" id="res-text-summarize"></div>
      </div>

      <button class="btn-primary" id="btn-summarize" onclick="runSummarize()">生成总结</button>
    </div>
  </main>

  <div class="safe-bottom"></div>
</div>

<script>
// ── State ──
const state = {
  translate: { file: null, lang: 'zh' },
  summarize: { file: null },
};

// ── Tabs ──
function switchTab(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  event.currentTarget.classList.add('active');
}

// ── Options ──
function selectOpt(el, panel) {
  el.closest('.options').querySelectorAll('.opt-btn').forEach(b => b.classList.remove('selected'));
  el.classList.add('selected');
  state[panel].lang = el.dataset.val;
}

// ── File drag/drop/select ──
function onFile(e, panel) {
  const f = e.target.files[0];
  if (f) setFile(panel, f);
}
function onDrag(e, panel) {
  e.preventDefault();
  document.getElementById('dz-' + panel).classList.add('over');
}
function offDrag(panel) {
  document.getElementById('dz-' + panel).classList.remove('over');
}
function onDrop(e, panel) {
  e.preventDefault();
  offDrag(panel);
  const f = e.dataTransfer.files[0];
  if (f && f.name.toLowerCase().endsWith('.pdf')) setFile(panel, f);
}
function setFile(panel, f) {
  state[panel].file = f;
  document.getElementById('url-' + panel).value = '';
  const fn = document.getElementById('fn-' + panel);
  fn.textContent = f.name;
  fn.style.display = 'block';
  hideError(panel);
}

// ── Helpers ──
function showError(panel, msg) {
  const el = document.getElementById('err-' + panel);
  el.textContent = msg; el.classList.add('visible');
}
function hideError(panel) {
  document.getElementById('err-' + panel).classList.remove('visible');
}
function setProgress(panel, pct, label) {
  document.getElementById('prog-' + panel).classList.add('visible');
  document.getElementById('prog-fill-' + panel).style.width = pct + '%';
  if (label) document.getElementById('prog-label-' + panel).textContent = label;
}
function hideProgress(panel) {
  document.getElementById('prog-' + panel).classList.remove('visible');
}
function setBtn(panel, disabled, text) {
  const b = document.getElementById('btn-' + panel);
  b.disabled = disabled; b.textContent = text;
}

// ── Translate ──
async function runTranslate() {
  hideError('translate');
  document.getElementById('dl-translate').classList.remove('visible');

  const file = state.translate.file;
  const url  = normalizePdfUrl(document.getElementById('url-translate').value.trim());
  if (!file && !url) return showError('translate', '请上传 PDF 或输入链接');

  setBtn('translate', true, '翻译中…');
  setProgress('translate', 10, '正在上传…');

  try {
    let res;
    if (file) {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('lang_out', state.translate.lang);
      res = await fetch('/v1/pdf/upload', { method: 'POST', body: fd });
    } else {
      res = await fetch('/v1/pdf/url', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, action: 'translate', lang_out: state.translate.lang }),
      });
    }
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const { job_id } = await res.json();

    // Poll for completion
    setProgress('translate', 30, '正在翻译，可能需要几分钟…');
    let pct = 30;
    while (true) {
      await sleep(2000);
      const poll = await fetch('/v1/pdf/job/' + job_id);
      const data = await poll.json();
      if (data.status === 'done') {
        setProgress('translate', 100, '完成！');
        document.getElementById('dl-mono').href = '/v1/pdf/download/' + job_id + '/mono';
        document.getElementById('dl-dual').href = '/v1/pdf/download/' + job_id + '/dual';
        document.getElementById('dl-translate').classList.add('visible');
        hideProgress('translate');
        break;
      }
      if (data.status === 'error') throw new Error(data.error || '翻译失败');
      pct = Math.min(pct + 8, 90);
      setProgress('translate', pct);
    }
  } catch(e) {
    hideProgress('translate');
    showError('translate', '错误：' + e.message);
  } finally {
    setBtn('translate', false, '开始翻译');
  }
}

// ── Summarize ──
async function runSummarize() {
  hideError('summarize');
  const resEl = document.getElementById('res-summarize');
  const textEl = document.getElementById('res-text-summarize');
  resEl.classList.remove('visible');
  textEl.textContent = '';

  const file = state.summarize.file;
  const url  = normalizePdfUrl(document.getElementById('url-summarize').value.trim());
  if (!file && !url) return showError('summarize', '请上传 PDF 或输入链接');

  setBtn('summarize', true, '总结中…');
  setProgress('summarize', 20, '正在提取文字…');

  try {
    let endpoint, body, headers = {};
    if (file) {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('action', 'summarize');
      endpoint = '/v1/pdf/upload_stream';
      body = fd;
    } else {
      endpoint = '/v1/pdf/url_stream';
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify({ url, action: 'summarize' });
    }

    const res = await fetch(endpoint, { method: 'POST', headers, body });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);

    setProgress('summarize', 60, '正在生成摘要…');
    resEl.classList.add('visible');
    textEl.classList.add('streaming');

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') { textEl.classList.remove('streaming'); break; }
        try {
          const d = JSON.parse(payload);
          if (d.text) textEl.textContent += d.text;
        } catch {}
      }
    }
    textEl.classList.remove('streaming');
    hideProgress('summarize');
  } catch(e) {
    hideProgress('summarize');
    textEl.classList.remove('streaming');
    showError('summarize', '错误：' + e.message);
  } finally {
    setBtn('summarize', false, '生成总结');
  }
}

function normalizePdfUrl(url) {
  // arXiv abs → pdf
  url = url.replace(/arxiv\.org\/abs\/(\d+\.\d+)/g, 'arxiv.org/pdf/$1');
  return url;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
</script>
</body>
</html>"""


def create_app(llm: LLMEngine, transcriber: Transcriber) -> FastAPI:
    app = FastAPI(title="Lumina", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── PWA 前端 ──────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def pwa_index():
        return HTMLResponse(content=_PWA_HTML)

    @app.get("/manifest.json")
    async def pwa_manifest():
        return JSONResponse({
            "name": "Lumina",
            "short_name": "Lumina",
            "description": "本地 AI 翻译与摘要",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": "#007aff",
            "icons": [
                {"src": "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>✨</text></svg>",
                 "sizes": "any", "type": "image/svg+xml"}
            ]
        })

    # ── PDF Job 管理 ──────────────────────────────────────────────────────────

    async def _fetch_pdf_url(url: str) -> Path:
        """
        获取远程 PDF 的本地路径，优先命中缓存（~/.lumina/cache/pdf/）。
        返回缓存文件路径（永久文件，不应被临时目录清理）。
        """
        from lumina.pdf_cache import get_cached, put_cache
        cached = get_cached(url)
        if cached:
            return cached
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        return put_cache(url, resp.content)

    async def _run_translate_job(job_id: str, pdf_path: str, lang_out: str):
        """后台翻译任务，结果写入 _pdf_jobs。"""
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()
        try:
            from lumina.pdf_translate import translate_pdfs
            tmp_dir = _pdf_jobs[job_id]["dir"]
            results = await loop.run_in_executor(
                None, lambda: translate_pdfs(
                    paths=[pdf_path],
                    output_dir=tmp_dir,
                    lang_out=lang_out,
                )
            )
            if results:
                mono, dual = results[0]
                _pdf_jobs[job_id].update({"status": "done", "mono": mono, "dual": dual})
            else:
                _pdf_jobs[job_id].update({"status": "error", "error": "no output"})
        except Exception as e:
            _pdf_jobs[job_id].update({"status": "error", "error": str(e)})

    async def _extract_and_stream_summary(pdf_path: str):
        """提取 PDF 文字，流式生成摘要，yield SSE 数据行。"""
        import fitz
        doc = fitz.open(pdf_path)
        text = "".join(p.get_text() for p in doc)[:8000]
        doc.close()
        async for token in llm.generate_stream(text, task="summarize"):
            yield f"data: {json.dumps({'text': token})}\n\n"
        yield "data: [DONE]\n\n"

    @app.post("/v1/pdf/upload")
    async def pdf_upload(
        file: UploadFile = File(...),
        lang_out: str = Form("zh"),
    ):
        """上传 PDF → 翻译，返回 job_id。"""
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(400, "仅支持 PDF 文件")
        tmp_dir = tempfile.mkdtemp(prefix="lumina_")
        pdf_path = str(Path(tmp_dir) / file.filename)
        Path(pdf_path).write_bytes(await file.read())

        job_id = uuid.uuid4().hex
        _pdf_jobs[job_id] = {"status": "running", "dir": tmp_dir, "ts": time.time()}
        asyncio.create_task(_run_translate_job(job_id, pdf_path, lang_out))
        return {"job_id": job_id}

    @app.post("/v1/pdf/url")
    async def pdf_from_url(body: dict):
        """从 URL 下载 PDF（命中缓存则跳过下载）→ 翻译，返回 job_id。"""
        url = body.get("url", "").strip()
        lang_out = body.get("lang_out", "zh")
        if not url:
            raise HTTPException(400, "url 不能为空")
        try:
            pdf_path = await _fetch_pdf_url(url)
        except Exception as e:
            raise HTTPException(400, f"下载 PDF 失败：{e}")

        # 翻译输出放独立临时目录（与缓存目录分开，翻译完成后可清理）
        tmp_dir = tempfile.mkdtemp(prefix="lumina_out_")
        job_id = uuid.uuid4().hex
        _pdf_jobs[job_id] = {"status": "running", "dir": tmp_dir, "ts": time.time()}
        asyncio.create_task(_run_translate_job(job_id, str(pdf_path), lang_out))
        return {"job_id": job_id}

    @app.get("/v1/pdf/job/{job_id}")
    async def pdf_job_status(job_id: str):
        job = _pdf_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return {"status": job["status"], "error": job.get("error")}

    @app.get("/v1/pdf/download/{job_id}/{variant}")
    async def pdf_download(job_id: str, variant: str):
        job = _pdf_jobs.get(job_id)
        if not job or job["status"] != "done":
            raise HTTPException(404, "Job not ready")
        key = "mono" if variant == "mono" else "dual"
        path = job.get(key)
        if not path or not Path(path).exists():
            raise HTTPException(404, "File not found")
        return FileResponse(path, media_type="application/pdf", filename=Path(path).name)

    @app.post("/v1/pdf/upload_stream")
    async def pdf_upload_stream(file: UploadFile = File(...)):
        """上传 PDF → 流式摘要（SSE）。"""
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(400, "仅支持 PDF 文件")
        tmp_dir = tempfile.mkdtemp(prefix="lumina_")
        pdf_path = str(Path(tmp_dir) / file.filename)
        Path(pdf_path).write_bytes(await file.read())
        return StreamingResponse(
            _extract_and_stream_summary(pdf_path),
            media_type="text/event-stream",
            background=_cleanup_after(tmp_dir, delay=5),
        )

    @app.post("/v1/pdf/url_stream")
    async def pdf_url_stream(body: dict):
        """从 URL 下载 PDF（命中缓存则跳过下载）→ 流式摘要（SSE）。"""
        url = body.get("url", "").strip()
        if not url:
            raise HTTPException(400, "url 不能为空")
        try:
            pdf_path = await _fetch_pdf_url(url)
        except Exception as e:
            raise HTTPException(400, f"下载 PDF 失败：{e}")
        # 缓存文件是持久文件，不清理；直接流式摘要
        return StreamingResponse(
            _extract_and_stream_summary(str(pdf_path)),
            media_type="text/event-stream",
        )

    # ── 健康检查 ─────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        return {"status": "ok", "llm_loaded": llm.is_loaded}

    # ── 模型列表 ─────────────────────────────────────────────────────────────

    @app.get("/v1/models")
    async def list_models():
        return ModelList(
            data=[
                ModelCard(id="lumina"),
                ModelCard(id="lumina-whisper"),
            ]
        )

    # ── Chat Completions（OpenAI 兼容）───────────────────────────────────────

    @app.post("/v1/chat/completions")
    async def chat_completions(request: ChatCompletionRequest, raw: Request):
        system_override: Optional[str] = None
        system_msg = next((m for m in request.messages if m.role == "system"), None)
        if system_msg is not None:
            system_override = (
                system_msg.content
                if isinstance(system_msg.content, str)
                else " ".join(c.text for c in system_msg.content)
            )

        user_msg = next(
            (m for m in reversed(request.messages) if m.role == "user"), None
        )
        if user_msg is None:
            raise HTTPException(status_code=400, detail="No user message found")

        user_text = (
            user_msg.content
            if isinstance(user_msg.content, str)
            else " ".join(c.text for c in user_msg.content)
        )

        if request.stream:
            return StreamingResponse(
                _stream_chat(request, user_text, system_override),
                media_type="text/event-stream",
            )

        text = await llm.generate(
            user_text,
            task="chat",
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            system=system_override,
        )
        return ChatCompletionResponse(
            model=request.model,
            choices=[
                ChatCompletionChoice(
                    message=ChatMessage(role="assistant", content=text)
                )
            ],
            usage=UsageInfo(),
        )

    async def _stream_chat(request: ChatCompletionRequest, user_text: str, system_override: Optional[str] = None):
        req_id = f"chatcmpl-{random_uuid()}"
        async for token in llm.generate_stream(
            user_text,
            task="chat",
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            system=system_override,
        ):
            chunk = ChatCompletionStreamResponse(
                id=req_id,
                model=request.model,
                choices=[
                    ChatCompletionStreamChoice(
                        delta=ChatCompletionStreamDelta(content=token)
                    )
                ],
            )
            yield f"data: {chunk.model_dump_json()}\n\n"
            if await raw_request_disconnected(request):
                break
        end_chunk = ChatCompletionStreamResponse(
            id=req_id,
            model=request.model,
            choices=[
                ChatCompletionStreamChoice(
                    delta=ChatCompletionStreamDelta(),
                    finish_reason="stop",
                )
            ],
        )
        yield f"data: {end_chunk.model_dump_json()}\n\n"
        yield "data: [DONE]\n\n"

    # ── 翻译 ──────────────────────────────────────────────────────────────────

    @app.post("/v1/translate")
    async def translate(request: TranslateRequest):
        task = "translate_to_zh" if request.target_language == "zh" else "translate_to_en"
        if request.stream:
            return StreamingResponse(
                _stream_text(request.text, task),
                media_type="text/event-stream",
            )
        text = await llm.generate(request.text, task=task)
        return TextResponse(text=text)

    # ── 摘要 ──────────────────────────────────────────────────────────────────

    @app.post("/v1/summarize")
    async def summarize(request: SummarizeRequest):
        if request.stream:
            return StreamingResponse(
                _stream_text(request.text, "summarize"),
                media_type="text/event-stream",
            )
        text = await llm.generate(request.text, task="summarize")
        return TextResponse(text=text)

    # ── 润色 ──────────────────────────────────────────────────────────────────

    @app.post("/v1/polish")
    async def polish(request: PolishRequest):
        task = "polish_zh" if request.language == "zh" else "polish_en"
        if request.stream:
            return StreamingResponse(
                _stream_text(request.text, task),
                media_type="text/event-stream",
            )
        text = await llm.generate(request.text, task=task)
        return TextResponse(text=text)

    async def _stream_text(user_text: str, task: str):
        async for token in llm.generate_stream(user_text, task=task):
            yield f"data: {json.dumps({'text': token})}\n\n"
        yield "data: [DONE]\n\n"

    # ── 语音转写：上传文件（OpenAI 兼容）─────────────────────────────────────

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(
        file: UploadFile = File(...),
        language: Optional[str] = Form(None),
    ):
        wav_bytes = await file.read()
        text = await transcriber.transcribe(wav_bytes, language=language)
        return TranscriptionResponse(text=text)

    # ── 语音录制：按键触发流程 ────────────────────────────────────────────────

    @app.post("/v1/audio/record/start", response_model=RecordStartResponse)
    async def record_start():
        session_id = uuid.uuid4().hex
        recorder = AudioRecorder()
        stop_event = asyncio.Event()

        async def _record_task():
            return await recorder.record_until_release(stop_event)

        task = asyncio.create_task(_record_task())
        _record_sessions[session_id] = (recorder, stop_event, task)
        return RecordStartResponse(session_id=session_id)

    @app.post("/v1/audio/record/stop")
    async def record_stop(request: RecordStopRequest):
        entry = _record_sessions.pop(request.session_id, None)
        if entry is None:
            raise HTTPException(status_code=404, detail="Session not found")

        recorder, stop_event, task = entry
        stop_event.set()
        wav_bytes: bytes = await task

        if not wav_bytes:
            return TranscriptionResponse(text="")

        text = await transcriber.transcribe(wav_bytes, language=request.language)
        return TranscriptionResponse(text=text)

    return app


def _cleanup_after(tmp_dir: str, delay: int = 30):
    """返回 BackgroundTask：延迟删除临时目录。"""
    from starlette.background import BackgroundTask

    async def _do():
        await asyncio.sleep(delay)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return BackgroundTask(_do)


async def raw_request_disconnected(request) -> bool:
    """辅助函数，检查客户端是否断开（流式场景）。"""
    try:
        return await asyncio.wait_for(request.is_disconnected(), timeout=0)
    except Exception:
        return False
