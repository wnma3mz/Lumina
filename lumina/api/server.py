"""
Lumina HTTP 服务

提供 OpenAI 兼容接口 + 语音录制转写接口 + PWA 前端。
"""
import asyncio
import json
import logging
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

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
    PdfUrlRequest,
    RecordStopRequest,
    PolishRequest,
    SummarizeRequest,
    TextResponse,
    TranscriptionResponse,
    TranslateRequest,
    UsageInfo,
    random_uuid,
)

logger = logging.getLogger("lumina")

# PDF 翻译 job 存储：job_id -> {"status": str, "mono": path, "dual": path, "dir": tmpdir, "ts": float}
_pdf_jobs: dict[str, dict] = {}

# ── 静态文件路径 ───────────────────────────────────────────────────────────────
import sys as _sys  # noqa: E402
_STATIC_DIR = (
    Path(_sys._MEIPASS) / "lumina" / "api" / "static"
    if hasattr(_sys, "_MEIPASS")
    else Path(__file__).parent / "static"
)





try:
    from importlib.metadata import version as _pkg_version
    _LUMINA_VERSION = _pkg_version("lumina")
except Exception:
    _LUMINA_VERSION = "0.3.0"

# 服务启动时间戳，用于前端检测服务重启
_SERVER_START_TIME = time.time()


def create_app(llm: LLMEngine, transcriber: Transcriber) -> FastAPI:
    app = FastAPI(title="Lumina", version=_LUMINA_VERSION)
    # 保存后台 task 引用，防止 GC 提前回收未完成的 task
    _bg_tasks: set = set()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── PWA 前端 ──────────────────────────────────────────────────────────────

    @app.get("/")
    async def pwa_index():
        return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/logo.svg")
    async def pwa_logo():
        return FileResponse(_STATIC_DIR / "logo.svg", media_type="image/svg+xml")

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
                {"src": "/logo.svg", "sizes": "256x256", "type": "image/svg+xml", "purpose": "any"}
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
        """后台翻译任务，结果写入 _pdf_jobs。完成后立即结束，清理由独立 task 负责。"""
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
        finally:
            # 3600 秒后删临时目录（与 job 记录清理时间对齐，避免 status=done 但文件已删的 404）
            # 两个独立 task：主任务到此结束，不再被 sleep 占用
            tmp_dir = _pdf_jobs.get(job_id, {}).get("dir")
            if tmp_dir:
                t1 = _asyncio.create_task(_delayed_rmtree(tmp_dir, delay=3600))
                _bg_tasks.add(t1)
                t1.add_done_callback(_bg_tasks.discard)

            async def _cleanup_job(jid: str):
                await _asyncio.sleep(3600)
                _pdf_jobs.pop(jid, None)

            t2 = _asyncio.create_task(_cleanup_job(job_id))
            _bg_tasks.add(t2)
            t2.add_done_callback(_bg_tasks.discard)

    async def _extract_and_stream_summary(pdf_path: str):
        """提取 PDF 文字，流式生成摘要，yield SSE 数据行。"""
        import fitz

        def _extract_text() -> str:
            doc = fitz.open(pdf_path)
            try:
                return "".join(p.get_text() for p in doc)[:8000]
            finally:
                doc.close()

        text = await asyncio.to_thread(_extract_text)
        try:
            async for token in llm.generate_stream(text, task="summarize"):
                yield f"data: {json.dumps({'text': token})}\n\n"
        except Exception as e:
            logger.error("stream_summary error: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    async def _write_upload(upload_file: UploadFile, dest: str) -> None:
        """流式写入上传文件，避免全量加载进内存。"""
        def _sync_copy():
            with open(dest, "wb") as f:
                shutil.copyfileobj(upload_file.file, f)
        await asyncio.to_thread(_sync_copy)

    @app.post("/v1/pdf/upload")
    async def pdf_upload(
        file: UploadFile = File(...),
        lang_out: str = Form("zh"),
    ):
        """上传 PDF → 翻译，返回 job_id。"""
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(400, "仅支持 PDF 文件")
        tmp_dir = tempfile.mkdtemp(prefix="lumina_")
        pdf_path = str(Path(tmp_dir) / Path(file.filename).name)
        await _write_upload(file, pdf_path)

        job_id = uuid.uuid4().hex
        _pdf_jobs[job_id] = {"status": "running", "dir": tmp_dir, "ts": time.time()}
        task = asyncio.create_task(_run_translate_job(job_id, pdf_path, lang_out))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return {"job_id": job_id}

    @app.post("/v1/pdf/url")
    async def pdf_from_url(body: PdfUrlRequest):
        """从 URL 下载 PDF（命中缓存则跳过下载）→ 翻译，返回 job_id。"""
        url = body.url.strip()
        lang_out = body.lang_out
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
        task = asyncio.create_task(_run_translate_job(job_id, str(pdf_path), lang_out))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
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
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] != "done":
            raise HTTPException(409, "Job not ready")
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
        pdf_path = str(Path(tmp_dir) / Path(file.filename).name)
        await _write_upload(file, pdf_path)
        return StreamingResponse(
            _extract_and_stream_summary(pdf_path),
            media_type="text/event-stream",
            background=_cleanup_after(tmp_dir, delay=5),
        )

    @app.post("/v1/pdf/url_stream")
    async def pdf_url_stream(body: PdfUrlRequest):
        """从 URL 下载 PDF（命中缓存则跳过下载）→ 流式摘要（SSE）。"""
        url = body.url.strip()
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
                else " ".join(
                    getattr(c, "text", "")
                    for c in system_msg.content
                    if getattr(c, "type", "text") == "text"
                )
            )

        user_msg = next(
            (m for m in reversed(request.messages) if m.role == "user"), None
        )
        if user_msg is None:
            raise HTTPException(status_code=400, detail="No user message found")

        user_text = (
            user_msg.content
            if isinstance(user_msg.content, str)
            else " ".join(
                getattr(c, "text", "")
                for c in user_msg.content
                if getattr(c, "type", "text") == "text"
            )
        )

        if request.stream:
            return StreamingResponse(
                _stream_chat(request, raw, user_text, system_override),
                media_type="text/event-stream",
            )

        text = await llm.generate(
            user_text,
            task="chat",
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
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

    async def _stream_chat(request: ChatCompletionRequest, raw_req: Request, user_text: str, system_override: Optional[str] = None):
        req_id = f"chatcmpl-{random_uuid()}"
        finish_reason = "stop"
        try:
            async for token in llm.generate_stream(
                user_text,
                task="chat",
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                top_p=request.top_p,
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
                if await raw_request_disconnected(raw_req):
                    break
        except Exception as e:
            logger.error("stream_chat error: %s", e)
            finish_reason = "error"
        end_chunk = ChatCompletionStreamResponse(
            id=req_id,
            model=request.model,
            choices=[
                ChatCompletionStreamChoice(
                    delta=ChatCompletionStreamDelta(),
                    finish_reason=finish_reason,
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
        try:
            async for token in llm.generate_stream(user_text, task=task):
                yield f"data: {json.dumps({'text': token})}\n\n"
        except Exception as e:
            logger.error("stream_text error: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    # ── 语音转写：上传文件（OpenAI 兼容）─────────────────────────────────────

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(
        file: UploadFile = File(...),
        language: Optional[str] = Form(None),
        raw: Request = None,
    ):
        if raw is not None:
            content_length = raw.headers.get("content-length")
            if content_length and int(content_length) > 100 * 1024 * 1024:
                raise HTTPException(413, "文件过大，最大支持 100MB")
        wav_bytes = await file.read()
        text = await transcriber.transcribe(wav_bytes, language=language)
        return TranscriptionResponse(text=text)

    # ── 语音录制：暂不支持 ────────────────────────────────────────────────────

    @app.post("/v1/audio/record/start")
    async def record_start():
        raise HTTPException(status_code=501, detail="暂不支持")

    @app.post("/v1/audio/record/stop")
    async def record_stop(request: RecordStopRequest):
        raise HTTPException(status_code=501, detail="暂不支持")

    # ── Digest / Daily Dashboard ──────────────────────────────────────────────

    @app.get("/v1/digest")
    async def get_digest_api():
        from lumina.digest import load_digest, get_status
        status = get_status()
        return {
            "content": load_digest(),
            "generating": status["generating"],
            "generated_at": status["generated_at"],
            "server_start": _SERVER_START_TIME,
        }

    @app.post("/v1/digest/refresh")
    async def refresh_digest_api(background_tasks: BackgroundTasks):
        from lumina.digest import maybe_generate_digest

        async def _run():
            await maybe_generate_digest(llm, force_full=True)

        background_tasks.add_task(_run)
        return {"status": "refreshing"}

    @app.get("/v1/digest/debug")
    async def digest_debug_api():
        """本地调试接口（非面向最终用户）：返回上次采集的缓存数据，立即返回，不触发新的采集。

        响应包含 scan_dirs、collector 详情、cursor 时间戳及 md_files 路径列表，
        属于本机敏感信息。仅供本地诊断使用；若 Lumina 绑定 0.0.0.0 对局域网开放，
        需自行评估此接口的暴露风险（考虑添加鉴权或在生产环境禁用）。
        """
        from lumina.digest.core import get_debug_info
        return get_debug_info()

    @app.get("/v1/digest/export")
    async def digest_export_api():
        """下载完整 digest.md 文件。"""
        from lumina.digest import load_digest
        from fastapi.responses import Response
        from datetime import datetime
        content = load_digest() or ""
        filename = f"lumina-digest-{datetime.now().strftime('%Y%m%d')}.md"
        return Response(
            content=content.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return app


def _cleanup_after(tmp_dir: str, delay: int = 30):
    """返回 BackgroundTask：延迟删除临时目录（流式响应结束后挂载）。"""
    from starlette.background import BackgroundTask

    async def _do():
        await asyncio.sleep(delay)
        await asyncio.to_thread(shutil.rmtree, tmp_dir, True)

    return BackgroundTask(_do)


async def _delayed_rmtree(path: str, delay: int = 300):
    """延迟删除临时目录（在 asyncio 协程内使用）。"""
    try:
        await asyncio.sleep(delay)
    finally:
        await asyncio.to_thread(shutil.rmtree, path, True)


async def raw_request_disconnected(request) -> bool:
    """辅助函数，检查客户端是否断开（流式场景）。"""
    try:
        return await asyncio.wait_for(request.is_disconnected(), timeout=0.001)
    except asyncio.TimeoutError:
        return False
    except Exception:
        return False
