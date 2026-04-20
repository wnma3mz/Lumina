"""
lumina/digest — 本地活动摘要（Daily Digest）
"""
from lumina.services.digest.config import configure, get_cfg, DigestConfig
from lumina.services.digest.core import (
    generate_digest,
    generate_report,
    maybe_generate_digest,
    load_digest,
    get_status,
)

__all__ = [
    "configure",
    "get_cfg",
    "DigestConfig",
    "generate_digest",
    "generate_report",
    "maybe_generate_digest",
    "load_digest",
    "get_status",
]
