"""
lumina/api/routers/audio.py — 语音转写 + 录制路由
"""
import json
from typing import Optional
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from sse_starlette.sse import EventSourceResponse

from lumina.api.protocol import RecordStopRequest, TranscriptionResponse
from lumina.asr.live import LiveTranslator

router = APIRouter(prefix="/v1/audio", tags=["audio"])

@router.get("/live")
async def live_translate(request: Request, lang_in: str = "auto", lang_out: str = "zh"):
    """SSE 流：实时系统音频同传。"""
    translator = LiveTranslator(
        request.app.state.llm, 
        request.app.state.transcriber, 
        lang_in=lang_in, 
        lang_out=lang_out
    )
    
    async def event_generator():
        try:
            async for item in translator.stream_translate():
                # 检查客户端是否断开
                if await request.is_disconnected():
                    break
                yield {
                    "data": json.dumps(item)
                }
        finally:
            translator.stop()

    return EventSourceResponse(event_generator())


@router.get("/check_env")
async def check_audio_env():
    """检查是否安装了虚拟音频回路驱动（如 BlackHole）。"""
    from lumina.asr.recorder import AudioRecorder
    device_index = AudioRecorder.find_loopback_device()
    if device_index is None:
        raise HTTPException(
            status_code=400, 
            detail="未检测到虚拟音频回路设备 (如 BlackHole, Virtual Audio Cable)。请先安装才能使用系统级实时同传功能。"
        )
    return {"ok": True, "device_index": device_index}


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
    lang_param = None if language == "auto" else language
    text = await transcriber.transcribe(wav_bytes, language=lang_param)
    return TranscriptionResponse(text=text)


@router.post("/record/start")
async def record_start():
    raise HTTPException(status_code=501, detail="暂不支持")


@router.post("/record/stop")
async def record_stop(request: RecordStopRequest):
    raise HTTPException(status_code=501, detail="暂不支持")
