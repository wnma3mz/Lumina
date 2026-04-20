"""
lumina/api/routers/media.py — 图片 OCR / Caption 路由
"""
import base64
from mimetypes import guess_type

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from lumina.api.chat_runtime import build_image_chat_messages, run_chat_messages, to_provider_messages
from lumina.api.protocol import ImageUrlRequest, MediaTextResponse

router = APIRouter(prefix="/v1/media", tags=["media"])


def _ensure_image_file(upload: UploadFile) -> str:
    filename = (upload.filename or "").lower()
    content_type = (upload.content_type or "").lower()
    if filename and any(filename.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
        return content_type or guess_type(filename)[0] or "image/png"
    if content_type.startswith("image/"):
        return content_type
    raise HTTPException(400, "仅支持图片文件")


def _validate_image_size(image_bytes: bytes) -> None:
    max_mb = 12
    max_bytes = max_mb * 1024 * 1024
    if not image_bytes:
        raise HTTPException(400, "图片内容为空")
    if len(image_bytes) > max_bytes:
        raise HTTPException(400, f"图片过大，请控制在 {max_mb} MB 以内")


def _image_data_url(image_bytes: bytes, content_type: str) -> str:
    payload = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{content_type};base64,{payload}"


async def _run_image_task(raw: Request, *, task: str, image_ref: str, origin: str) -> MediaTextResponse:
    llm = raw.app.state.llm
    messages = to_provider_messages(build_image_chat_messages(image_ref))
    try:
        text = await run_chat_messages(
            raw,
            messages=messages,
            task=task,
            origin=origin,
        )
    except NotImplementedError as exc:
        raise HTTPException(400, f"{exc}。请切换到支持视觉输入的 OpenAI 兼容模型。")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        label = "OCR" if task == "image_ocr" else "Caption"
        raise HTTPException(500, f"{label} 失败：{exc}")
    return MediaTextResponse(text=text.strip(), model=llm.provider_model_name)


@router.post("/ocr", response_model=MediaTextResponse)
async def media_ocr(file: UploadFile = File(...), raw: Request = None):
    content_type = _ensure_image_file(file)
    image_bytes = await file.read()
    _validate_image_size(image_bytes)
    return await _run_image_task(
        raw,
        task="image_ocr",
        image_ref=_image_data_url(image_bytes, content_type),
        origin="media_ocr_api",
    )


@router.post("/ocr_url", response_model=MediaTextResponse)
async def media_ocr_url(body: ImageUrlRequest, raw: Request):
    url = body.url.strip()
    if not url:
        raise HTTPException(400, "url 不能为空")
    return await _run_image_task(raw, task="image_ocr", image_ref=url, origin="media_ocr_api")


@router.post("/caption", response_model=MediaTextResponse)
async def media_caption(file: UploadFile = File(...), raw: Request = None):
    content_type = _ensure_image_file(file)
    image_bytes = await file.read()
    _validate_image_size(image_bytes)
    return await _run_image_task(
        raw,
        task="image_caption",
        image_ref=_image_data_url(image_bytes, content_type),
        origin="media_caption_api",
    )


@router.post("/caption_url", response_model=MediaTextResponse)
async def media_caption_url(body: ImageUrlRequest, raw: Request):
    url = body.url.strip()
    if not url:
        raise HTTPException(400, "url 不能为空")
    return await _run_image_task(raw, task="image_caption", image_ref=url, origin="media_caption_api")

