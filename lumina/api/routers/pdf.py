"""
lumina/api/routers/pdf.py — PDF 相关路由

包含：上传翻译、URL 翻译、job 状态、文件下载、流式摘要（upload/url）。
依赖通过 request.app.state 获取（FastAPI app.state DI 模式）。
"""
import asyncio
import json
import tempfile
from pathlib import Path

import markdown as md
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from lumina.api.protocol import PdfUrlRequest
from lumina.services.pdf import (
    cleanup_after,
    extract_pdf_pairs,
    fetch_pdf_url,
    stream_pdf_summary,
    write_upload,
)

router = APIRouter(prefix="/v1/pdf", tags=["pdf"])


@router.post("/upload")
async def pdf_upload(
    file: UploadFile = File(...),
    lang_out: str = Form("zh"),
    raw: Request = None,
):
    """上传 PDF → 翻译，返回 job_id。"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")
    manager = raw.app.state.pdf_manager
    tmp_dir = tempfile.mkdtemp(prefix="lumina_")
    pdf_path = str(Path(tmp_dir) / Path(file.filename).name)
    await write_upload(file, pdf_path)
    job_id = manager.submit_translate(pdf_path, lang_out, tmp_dir)
    return {"job_id": job_id}


@router.post("/url")
async def pdf_from_url(body: PdfUrlRequest, raw: Request):
    """从 URL 下载 PDF（命中缓存则跳过下载）→ 翻译，返回 job_id。"""
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


@router.get("/job/{job_id}")
async def pdf_job_status(job_id: str, raw: Request):
    manager = raw.app.state.pdf_manager
    job = manager.get_status(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"status": job["status"], "error": job.get("error")}


@router.get("/download/{job_id}/{variant}")
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


@router.post("/upload_stream")
async def pdf_upload_stream(file: UploadFile = File(...), raw: Request = None):
    """上传 PDF → 流式摘要（SSE）。"""
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


@router.post("/url_stream")
async def pdf_url_stream(body: PdfUrlRequest, raw: Request):
    """从 URL 下载 PDF（命中缓存则跳过下载）→ 流式摘要（SSE）。"""
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


@router.post("/summarize_sync")
async def pdf_summarize_sync(file: UploadFile = File(...), raw: Request = None):
    """上传 PDF → 等待生成完毕 → 返回 HTML 片段（供 HTMX hx-target 直接插入）。"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF 文件")
    llm = raw.app.state.llm
    tmp_dir = tempfile.mkdtemp(prefix="lumina_")
    pdf_path = str(Path(tmp_dir) / Path(file.filename).name)
    await write_upload(file, pdf_path)
    tokens: list[str] = []
    try:
        async for sse_line in stream_pdf_summary(pdf_path, llm):
            # SSE 格式：'data: {"text": "..."}\n\n' 或 'data: [DONE]\n\n'
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
    html_content = md.markdown(full_text, extensions=["fenced_code", "tables"])
    return HTMLResponse(f'<div class="result-text digest-item-body">{html_content}</div>')


@router.post("/url_summarize_sync")
async def pdf_url_summarize_sync(body: PdfUrlRequest, raw: Request):
    """从 URL 下载 PDF → 等待生成完毕 → 返回 HTML 片段。"""
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
    html_content = md.markdown(full_text, extensions=["fenced_code", "tables"])
    return HTMLResponse(f'<div class="result-text digest-item-body">{html_content}</div>')


@router.get("/pairs/{job_id}")
async def pdf_pairs(job_id: str, raw: Request):
    """解析双语 PDF，返回原文/译文对列表。"""
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
