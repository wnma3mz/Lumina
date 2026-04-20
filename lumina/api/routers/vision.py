"""
lumina/api/routers/vision.py — 图片 OCR / Caption 路由
"""

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from lumina.api.protocol import ImageUrlRequest, MediaTextResponse
from lumina.services.vision.core import (
    ensure_image_file,
    image_data_url,
    process_image_task,
    validate_image_size,
)

router = APIRouter(prefix="/v1/media", tags=["media"])


async def _run_image_task_handler(
    raw: Request, *, task: str, image_ref: str, origin: str
) -> MediaTextResponse:
    llm = raw.app.state.llm
    try:
        text = await process_image_task(
            llm=llm,
            task=task,
            image_ref=image_ref,
            origin=origin,
        )
        return MediaTextResponse(text=text, model=llm.provider_model_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))


@router.post("/ocr", response_model=MediaTextResponse)
async def media_ocr(file: UploadFile = File(...), raw: Request = None):
    try:
        content_type = ensure_image_file(file.filename, file.content_type)
        image_bytes = await file.read()
        validate_image_size(image_bytes)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await _run_image_task_handler(
        raw,
        task="image_ocr",
        image_ref=image_data_url(image_bytes, content_type),
        origin="media_ocr_api",
    )


@router.post("/ocr_url", response_model=MediaTextResponse)
async def media_ocr_url(body: ImageUrlRequest, raw: Request):
    url = body.url.strip()
    if not url:
        raise HTTPException(400, "url 不能为空")
    return await _run_image_task_handler(raw, task="image_ocr", image_ref=url, origin="media_ocr_api")


@router.post("/caption", response_model=MediaTextResponse)
async def media_caption(file: UploadFile = File(...), raw: Request = None):
    try:
        content_type = ensure_image_file(file.filename, file.content_type)
        image_bytes = await file.read()
        validate_image_size(image_bytes)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return await _run_image_task_handler(
        raw,
        task="image_caption",
        image_ref=image_data_url(image_bytes, content_type),
        origin="media_caption_api",
    )


@router.post("/caption_url", response_model=MediaTextResponse)
async def media_caption_url(body: ImageUrlRequest, raw: Request):
    url = body.url.strip()
    if not url:
        raise HTTPException(400, "url 不能为空")
    return await _run_image_task_handler(
        raw, task="image_caption", image_ref=url, origin="media_caption_api"
    )
