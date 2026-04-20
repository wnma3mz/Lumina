"""
lumina/services/vision/core.py — 视觉处理核心服务逻辑
"""
import base64
from mimetypes import guess_type
from typing import Optional

from lumina.engine.llm import LLMEngine
from lumina.engine.request_context import request_context


def ensure_image_file(filename: Optional[str], content_type: Optional[str]) -> str:
    """验证并推断图片类型。"""
    filename = (filename or "").lower()
    content_type = (content_type or "").lower()
    if filename and any(filename.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
        return content_type or guess_type(filename)[0] or "image/png"
    if content_type.startswith("image/"):
        return content_type
    raise ValueError("仅支持图片文件")


def validate_image_size(image_bytes: bytes) -> None:
    """校验图片大小，避免过大。"""
    max_mb = 12
    max_bytes = max_mb * 1024 * 1024
    if not image_bytes:
        raise ValueError("图片内容为空")
    if len(image_bytes) > max_bytes:
        raise ValueError(f"图片过大，请控制在 {max_mb} MB 以内")


def image_data_url(image_bytes: bytes, content_type: str) -> str:
    """构造图片 Data URL。"""
    payload = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{content_type};base64,{payload}"


async def process_image_task(
    llm: LLMEngine,
    task: str,
    image_ref: str,
    origin: str = "media_api",
    request_id: Optional[str] = None,
) -> str:
    """核心多模态图文处理逻辑，调用底层 LLMEngine。"""
    instruction = "请根据 system prompt 处理这张图片。"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": image_ref}},
            ],
        }
    ]
    try:
        with request_context(
            origin=origin,
            stream=False,
            client_model="lumina",
            request_id=request_id,
        ):
            text = await llm.generate_messages(messages=messages, task=task)
            return text.strip()
    except NotImplementedError as exc:
        raise ValueError(f"{exc}。请切换到支持视觉输入的 OpenAI 兼容模型。")
    except ValueError:
        raise
    except Exception as exc:
        label = "OCR" if task == "image_ocr" else "Caption"
        raise RuntimeError(f"{label} 失败：{exc}")
