"""
PDF URL 下载缓存。

缓存目录：~/.lumina/cache/pdf/
Key：sha256(url)[:16] + 原始文件名后缀，如 a3f2b1c0_2602.23881.pdf
永不过期；先写 .tmp 再 rename，保证并发安全。
"""
import hashlib
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lumina.pdf_cache")

CACHE_DIR = Path.home() / ".lumina" / "cache" / "pdf"


def _cache_path(url: str) -> Path:
    """根据 URL 计算缓存文件路径（不保证文件存在）。"""
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    # 取 URL 路径部分的最后一段作为可读后缀，去掉 query string
    fname = url.split("/")[-1].split("?")[0] or "download.pdf"
    if not fname.lower().endswith(".pdf"):
        fname += ".pdf"
    return CACHE_DIR / f"{digest}_{fname}"


def get_cached(url: str) -> Optional[Path]:
    """返回缓存文件路径（若存在且非空），否则返回 None。"""
    path = _cache_path(url)
    if path.exists() and path.stat().st_size > 0:
        return path
    return None


def put_cache(url: str, data: bytes) -> Path:
    """
    将下载内容原子写入缓存，返回缓存文件路径。
    先写 .tmp 再 rename，避免并发写入产生损坏文件。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = _cache_path(url)
    tmp = dest.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        tmp.rename(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    logger.info("Cached PDF: %s", dest)
    return dest
