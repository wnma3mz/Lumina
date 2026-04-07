"""
lumina/digest — 本地活动摘要（Daily Digest）

对外 API 与原 digest.py 完全一致，调用方零改动。
"""
from lumina.digest.config import configure, get_cfg, DigestConfig
from lumina.digest.core import (
    generate_digest,
    generate_changelog,
    maybe_generate_digest,
    maybe_generate_changelog,
    should_regenerate_full,
    load_digest,
    get_status,
)

__all__ = [
    "configure",
    "get_cfg",
    "DigestConfig",
    "generate_digest",
    "generate_changelog",
    "maybe_generate_digest",
    "maybe_generate_changelog",
    "should_regenerate_full",
    "load_digest",
    "get_status",
]
