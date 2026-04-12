"""
lumina/api/routers/audio.py — 语音转写 + 录制路由
"""
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from lumina.api.protocol import RecordStopRequest, TranscriptionResponse

router = APIRouter(prefix="/v1/audio", tags=["audio"])


@router.post("/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
    raw: Request = None,
):
    if raw is not None:
        content_length = raw.headers.get("content-length")
        if content_length and int(content_length) > 100 * 1024 * 1024:
            raise HTTPException(413, "文件过大，最大支持 100MB")
    transcriber = raw.app.state.transcriber
    wav_bytes = await file.read()
    text = await transcriber.transcribe(wav_bytes, language=language)
    return TranscriptionResponse(text=text)


@router.post("/record/start")
async def record_start():
    raise HTTPException(status_code=501, detail="暂不支持")


@router.post("/record/stop")
async def record_stop(request: RecordStopRequest):
    raise HTTPException(status_code=501, detail="暂不支持")
