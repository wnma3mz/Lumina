"""轻量的 GitHub release 检查，单例内存缓存（TTL 1h）。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

GITHUB_API = "https://api.github.com/repos/wnma3mz/Lumina/releases/latest"
_TIMEOUT = aiohttp.ClientTimeout(total=8)
_logger = logging.getLogger(__name__)


@dataclass
class UpdateInfo:
    current: str = ""
    latest: str = ""
    has_update: bool = False
    release_url: str = ""
    checked_at: datetime | None = None
    error: str = ""


_cache: UpdateInfo | None = None


async def check_update(current_version: str) -> UpdateInfo:
    """查询 GitHub API，1h 内有缓存则直接返回。"""
    global _cache
    now = datetime.now(timezone.utc)
    if (
        _cache is not None
        and _cache.checked_at is not None
        and (now - _cache.checked_at).total_seconds() < 3600
    ):
        return _cache

    info = UpdateInfo(current=current_version, checked_at=now)
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as sess:
            async with sess.get(GITHUB_API, headers={"User-Agent": "Lumina"}) as resp:
                resp.raise_for_status()
                data = await resp.json()
        latest = data.get("tag_name", "").lstrip("v")
        info.latest = latest
        info.release_url = data.get("html_url", "")
        info.has_update = _version_gt(latest, current_version)
    except Exception as e:
        info.error = str(e)
        _logger.debug("update check failed: %s", e)

    _cache = info
    return info


def _version_gt(a: str, b: str) -> bool:
    """返回 a > b（三段式语义版本比较）。"""
    def parse(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.split(".")[:3])
        except Exception:
            return (0, 0, 0)
    return parse(a) > parse(b)
