"""
lumina/api/routers/document.py — 文档处理路由（包含文本润色/翻译、PDF 解析提取等）
"""
import asyncio
import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from lumina.api.rendering import render_markdown_html
from lumina.api.protocol import (
    PdfUrlRequest,
    PolishRequest,
    RenderMarkdownRequest,
    RenderedHtmlResponse,
    SummarizeRequest,
    TextResponse,
    TranslateRequest,
)
from lumina.services.document.pdf import (
    cleanup_after,
    extract_pdf_pairs,
    fetch_pdf_url,
    stream_pdf_summary,
    write_upload,
)
from lumina.api.sse import stream_llm
from lumina.engine.request_context import request_context

router = APIRouter(tags=["document"])

# ── 文本处理 ───────────────────────────────────────────────────────────────

@router.post("/v1/translate")
async def translate(request: TranslateRequest, raw: Request):
    llm = raw.app.state.llm
    task = "translate_to_zh" if request.target_language == "zh" else "translate_to_en"
    if request.stream:
        return StreamingResponse(
            _stream_text(request.text, task, llm, origin="translate_api"),
            media_type="text/event-stream",
        )
    with request_context(origin="translate_api", stream=False):
        text = await llm.generate(request.text, task=task)
    return TextResponse(text=text)

@router.post("/v1/summarize")
async def summarize(request: SummarizeRequest, raw: Request):
    llm = raw.app.state.llm
    if request.stream:
        return StreamingResponse(
            _stream_text(request.text, "summarize", llm, origin="summarize_api"),
            media_type="text/event-stream",
        )
    with request_context(origin="summarize_api", stream=False):
        text = await llm.generate(request.text, task="summarize")
    return TextResponse(text=text)

@router.post("/v1/polish")
async def polish(request: PolishRequest, raw: Request):
    llm = raw.app.state.llm
    task = "polish_zh" if request.language == "zh" else "polish_en"
    if request.stream:
        return StreamingResponse(
            _stream_text(request.text, task, llm, origin="polish_api"),
            media_type="text/event-stream",
        )
    with request_context(origin="polish_api", stream=False):
        text = await llm.generate(request.text, task=task)
    return TextResponse(text=text)

@router.post("/v1/render_markdown")
async def render_markdown(request: RenderMarkdownRequest):
    return RenderedHtmlResponse(html=render_markdown_html(request.text))

async def _stream_text(user_text: str, task: str, llm, *, origin: str):
    async for chunk in stream_llm(
        llm,
        user_text,
        task=task,
        log_label="stream_text",
        origin=origin,
    ):
        yield chunk

# ── PDF 处理 ───────────────────────────────────────────────────────────────

@router.post("/v1/pdf/upload")
async def pdf_upload(
    file: UploadFile = File(...),
    lang_out: str = Form("zh"),
    raw: Request = None,
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")
    manager = raw.app.state.pdf_manager
    tmp_dir = tempfile.mkdtemp(prefix="lumina_")
    pdf_path = str(Path(tmp_dir) / Path(file.filename).name)
    await write_upload(file, pdf_path)
    job_id = manager.submit_translate(pdf_path, lang_out, tmp_dir)
    return {"job_id": job_id}

@router.post("/v1/pdf/url")
async def pdf_from_url(body: PdfUrlRequest, raw: Request):
    url = body.url.strip()
    lang_out = body.lang_out
    if not url:
        raise HTTPException(400, "url 不能为空")
    try:
        pdf_path = await fetch_pdf_url(url)
    except Exception as e:
        raise HTTPException(400, f"下载 PDF 失败：{e}")
    manager = raw.app.state.pdf_manager
    tmp_dir = tempfile.mkdtemp(prefix="lumina_out_")
    job_id = manager.submit_translate(str(pdf_path), lang_out, tmp_dir)
    return {"job_id": job_id}

@router.get("/v1/pdf/job/{job_id}")
async def pdf_job_status(job_id: str, raw: Request):
    manager = raw.app.state.pdf_manager
    job = manager.get_status(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"status": job["status"], "error": job.get("error")}

@router.get("/v1/pdf/download/{job_id}/{variant}")
async def pdf_download(job_id: str, variant: str, raw: Request):
    manager = raw.app.state.pdf_manager
    job = manager.get_status(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != "done":
        raise HTTPException(409, "Job not ready")
    path = manager.get_file(job_id, variant)
    if not path:
        raise HTTPException(404, "File not found")
    return FileResponse(path, media_type="application/pdf", filename=path.name)

@router.post("/v1/pdf/upload_stream")
async def pdf_upload_stream(file: UploadFile = File(...), raw: Request = None):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")
    llm = raw.app.state.llm
    tmp_dir = tempfile.mkdtemp(prefix="lumina_")
    pdf_path = str(Path(tmp_dir) / Path(file.filename).name)
    await write_upload(file, pdf_path)
    return StreamingResponse(
        stream_pdf_summary(pdf_path, llm),
        media_type="text/event-stream",
        background=cleanup_after(tmp_dir, delay=5),
    )

@router.post("/v1/pdf/url_stream")
async def pdf_url_stream(body: PdfUrlRequest, raw: Request):
    url = body.url.strip()
    if not url:
        raise HTTPException(400, "url 不能为空")
    try:
        pdf_path = await fetch_pdf_url(url)
    except Exception as e:
        raise HTTPException(400, f"下载 PDF 失败：{e}")
    llm = raw.app.state.llm
    return StreamingResponse(
        stream_pdf_summary(str(pdf_path), llm),
        media_type="text/event-stream",
    )

@router.post("/v1/pdf/summarize_sync")
async def pdf_summarize_sync(file: UploadFile = File(...), raw: Request = None):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")
    llm = raw.app.state.llm
    tmp_dir = tempfile.mkdtemp(prefix="lumina_")
    pdf_path = str(Path(tmp_dir) / Path(file.filename).name)
    await write_upload(file, pdf_path)
    tokens: list[str] = []
    try:
        async for sse_line in stream_pdf_summary(pdf_path, llm):
            line = sse_line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
                if "text" in d:
                    tokens.append(d["text"])
                elif "error" in d:
                    raise HTTPException(500, d["error"])
            except (json.JSONDecodeError, KeyError):
                pass
    except HTTPException:
        raise
    finally:
        import shutil
        asyncio.ensure_future(asyncio.to_thread(shutil.rmtree, tmp_dir, True))
    full_text = "".join(tokens)
    html_content = render_markdown_html(full_text)
    return HTMLResponse(f'<div class="result-text digest-item-body">{html_content}</div>')

@router.post("/v1/pdf/url_summarize_sync")
async def pdf_url_summarize_sync(body: PdfUrlRequest, raw: Request):
    url = body.url.strip()
    if not url:
        raise HTTPException(400, "url 不能为空")
    try:
        pdf_path = await fetch_pdf_url(url)
    except Exception as e:
        raise HTTPException(400, f"下载 PDF 失败：{e}")
    llm = raw.app.state.llm
    tokens: list[str] = []
    async for sse_line in stream_pdf_summary(str(pdf_path), llm):
        line = sse_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            d = json.loads(payload)
            if "text" in d:
                tokens.append(d["text"])
            elif "error" in d:
                raise HTTPException(500, d["error"])
        except (json.JSONDecodeError, KeyError):
            pass
    full_text = "".join(tokens)
    html_content = render_markdown_html(full_text)
    return HTMLResponse(f'<div class="result-text digest-item-body">{html_content}</div>')

@router.get("/v1/pdf/pairs/{job_id}")
async def pdf_pairs(job_id: str, raw: Request):
    manager = raw.app.state.pdf_manager
    job = manager.get_status(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != "done":
        raise HTTPException(409, "Job not ready")
    dual_path = manager.get_file(job_id, "dual")
    if not dual_path:
        raise HTTPException(404, "Dual PDF not found")
    try:
        pairs = await asyncio.to_thread(extract_pdf_pairs, str(dual_path))
        return {"pairs": pairs}
    except Exception as e:
        raise HTTPException(500, f"解析失败：{e}")