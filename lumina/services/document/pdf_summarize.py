"""
PDF 摘要：提取 PDF 文本后调用 Lumina /v1/summarize 接口生成摘要。
"""
import logging
import sys
import warnings
from pathlib import Path

import httpx

from lumina.config import DEFAULT_API_BASE_URL, DEFAULT_API_KEY

logger = logging.getLogger("lumina.summarize")

_DEFAULT_BASE_URL = DEFAULT_API_BASE_URL
_MAX_CHARS = 8000  # 截取前 N 字符送给 LLM（避免超出上下文长度）


def _import_fitz():
    """导入 PyMuPDF，并局部抑制其在 Python 3.12 下的 SWIG deprecation warning。"""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"builtin type SwigPyPacked has no __module__ attribute",
            category=DeprecationWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=r"builtin type SwigPyObject has no __module__ attribute",
            category=DeprecationWarning,
        )
        import fitz  # pymupdf

    return fitz


def _extract_text(pdf_path: str, max_chars: int = _MAX_CHARS) -> str:
    try:
        fitz = _import_fitz()
    except ImportError:
        logger.error("pymupdf 未安装，请运行: uv add pymupdf")
        sys.exit(1)

    with fitz.open(pdf_path) as doc:
        parts = []
        total = 0
        for page in doc:
            text = page.get_text()
            parts.append(text)
            total += len(text)
            if total >= max_chars:
                break
    return "".join(parts)[:max_chars]


def summarize_pdf(
    path: str,
    base_url: str = _DEFAULT_BASE_URL,
    api_key: str = DEFAULT_API_KEY,
    output: str = None,
    no_proxy: bool = True,
) -> str:
    """
    提取 PDF 文字，调用 Lumina summarize 接口，返回摘要文本。
    如果指定 output，同时写入 .txt 文件。
    """
    logger.info("Extracting text from: %s", path)
    text = _extract_text(path)
    if not text.strip():
        logger.error("PDF 无可提取文字（可能是扫描版）: %s", path)
        sys.exit(1)

    logger.info("Extracted %d chars, sending to summarize API...", len(text))

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    proxies = {"http://": None, "https://": None} if no_proxy else {}

    with httpx.Client(mounts={k: None for k in (proxies or {})}, timeout=120) as client:
        resp = client.post(
            f"{base_url.rstrip('/')}/v1/summarize",
            json={"text": text, "stream": False},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    summary = data.get("summary") or data.get("text") or str(data)

    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(summary, encoding="utf-8")
        logger.info("Summary written to: %s", out_path)

    return summary
