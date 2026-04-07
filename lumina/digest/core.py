"""
lumina/digest/core.py — 摘要生成、增量检测、状态管理
"""
import asyncio
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from lumina.digest.config import get_cfg
from lumina.digest.collectors import (
    collect_shell_history,
    collect_git_logs,
    collect_clipboard,
    collect_browser_history,
    collect_notes_app,
    collect_markdown_notes,
    collect_ai_queries,
)

logger = logging.getLogger("lumina.digest")

_DIGEST_PATH   = Path.home() / ".lumina" / "digest.md"
_LOCK_PATH     = Path.home() / ".lumina" / "digest.lock"
_SNAPSHOT_PATH = Path.home() / ".lumina" / "digest_snapshot.txt"

# 模块级状态，供 API 查询
_generating: bool = False
_generated_at: Optional[str] = None

# 上次活动检测结果缓存
_last_activity_check: Optional[dict] = None   # {time: str, has_new: bool}

# 上次 collector 结果缓存（key=函数名, value={chars, lines, preview, error}）
_last_collector_results: dict = {}

# 注册的采集器列表——新增数据源在此追加
_COLLECTORS = [
    collect_shell_history,
    collect_git_logs,
    collect_clipboard,
    collect_browser_history,
    collect_notes_app,
    collect_markdown_notes,
    collect_ai_queries,
]

DIGEST_SYSTEM_PROMPT = """\
你是一个个人助手，帮助用户回顾自己的数字活动。
根据以下本地活动记录，生成一份简洁的中文摘要，包含：
1. 最近在做什么（项目、任务）
2. 当前工作上下文（进行中的事项）
3. 值得关注的信息（剪贴板内容、频繁访问的页面等）
风格简洁，用 Markdown 格式，不超过 400 字。"""

CHANGELOG_SYSTEM_PROMPT = """\
你是一个个人助手。以下是用户过去一小时内的新增本地活动记录。
用 2-4 句话总结这段时间发生了哪些变化或新进展，中文，简洁。
如果内容很少或不重要，输出"（无显著变化）"即可。"""


# ── 增量检测 ──────────────────────────────────────────────────────────────────

def _make_snapshot() -> str:
    parts = []
    try:
        zsh = Path.home() / ".zsh_history"
        if zsh.exists():
            lines = zsh.read_text(errors="replace").splitlines()
            parts.append(lines[-1] if lines else "")
    except Exception:
        pass
    try:
        result = subprocess.check_output(["pbpaste"], timeout=2, text=True)
        parts.append(result.strip()[:50])
    except Exception:
        pass
    return "\n".join(parts)


def _has_new_activity() -> bool:
    global _last_activity_check
    current = _make_snapshot()
    if not _SNAPSHOT_PATH.exists():
        _SNAPSHOT_PATH.write_text(current, encoding="utf-8")
        result = True
    else:
        old = _SNAPSHOT_PATH.read_text(encoding="utf-8")
        if current != old:
            _SNAPSHOT_PATH.write_text(current, encoding="utf-8")
            result = True
        else:
            result = False
    _last_activity_check = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "has_new": result,
    }
    return result


# ── 采集 ──────────────────────────────────────────────────────────────────────

async def _collect_all() -> str:
    global _last_collector_results
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=len(_COLLECTORS)) as executor:
        futures = [loop.run_in_executor(executor, fn) for fn in _COLLECTORS]
        results = await asyncio.gather(*futures, return_exceptions=True)

    cache = {}
    sections = []
    for fn, r in zip(_COLLECTORS, results):
        name = fn.__name__
        if isinstance(r, Exception):
            import traceback
            cache[name] = {"chars": 0, "lines": 0, "preview": None,
                           "error": traceback.format_exc()}
        else:
            text = r or ""
            cache[name] = {
                "chars": len(text),
                "lines": text.count("\n"),
                "preview": text[:300] if text else None,
                "error": None,
            }
            if text.strip():
                sections.append(text)
    _last_collector_results = cache

    if not sections:
        return "（未采集到任何本地活动记录）"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"当前时间：{now_str}\n\n" + "\n\n---\n\n".join(sections)


# ── 摘要生成 ──────────────────────────────────────────────────────────────────

def _prepend_entry(entry: str) -> None:
    """将新条目插入 digest.md 最前面，旧内容跟在后面（以 \n---\n 分隔）。原子写入防止进程崩溃导致内容丢失。"""
    _DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _DIGEST_PATH.exists():
        try:
            old = _DIGEST_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            old = ""
        new_content = entry + "\n---\n\n" + old + "\n" if old else entry
    else:
        new_content = entry
    # 原子写入：先写临时文件再 rename，防止写入过程中崩溃导致内容损坏
    tmp = _DIGEST_PATH.with_suffix(".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(_DIGEST_PATH)


async def generate_digest(llm) -> str:
    """生成摘要，作为最新一条插入 digest.md 头部，历史记录永久保留。"""
    global _generating, _generated_at, _last_activity_check
    _generating = True
    _last_activity_check = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "has_new": True,
    }
    try:
        context = await _collect_all()
        logger.info("Digest: generating full summary...")
        summary = await llm.generate(
            context, task="chat",
            system=DIGEST_SYSTEM_PROMPT,
            max_tokens=600, temperature=0.4,
        )
        now = datetime.now()
        entry = (f"<!-- generated: {now.isoformat()} -->\n"
                 f"# {now.strftime('%Y-%m-%d %H:%M')}\n\n"
                 + summary.strip() + "\n")
        _prepend_entry(entry)
        _generated_at = now.isoformat()
        _SNAPSHOT_PATH.write_text(_make_snapshot(), encoding="utf-8")
        logger.info("Digest: saved to %s", _DIGEST_PATH)
        return entry
    finally:
        _generating = False


async def generate_changelog(llm) -> Optional[str]:
    """增量 Change Log：检测到新活动时追加最新条目，无变化返回 None。"""
    global _generating, _generated_at
    if not _has_new_activity():
        logger.debug("Digest: no new activity, skipping changelog")
        return None

    _generating = True
    try:
        context = await _collect_all()
        logger.info("Digest: generating changelog...")
        changelog = await llm.generate(
            context, task="chat",
            system=CHANGELOG_SYSTEM_PROMPT,
            max_tokens=200, temperature=0.4,
        )
        if "无显著变化" in changelog or not changelog.strip():
            logger.debug("Digest: changelog indicates no significant change")
            return None

        now = datetime.now()
        entry = (f"<!-- generated: {now.isoformat()} -->\n"
                 f"# {now.strftime('%Y-%m-%d %H:%M')} 更新\n\n"
                 + changelog.strip() + "\n")
        _prepend_entry(entry)
        _generated_at = now.isoformat()
        logger.info("Digest: changelog appended")
        return entry
    finally:
        _generating = False


# ── 对外接口 ──────────────────────────────────────────────────────────────────

def should_regenerate_full(max_age_hours: Optional[float] = None) -> bool:
    cfg = get_cfg()
    age_limit = max_age_hours if max_age_hours is not None else cfg.history_hours
    if not _DIGEST_PATH.exists():
        return True
    return time.time() - _DIGEST_PATH.stat().st_mtime > age_limit * 3600


def load_digest() -> Optional[str]:
    if not _DIGEST_PATH.exists():
        return None
    return _DIGEST_PATH.read_text(encoding="utf-8")


async def maybe_generate_digest(llm, force_full: bool = False) -> None:
    """启动时调用：全量摘要（若需要）。用 lock 文件防并发。"""
    if _LOCK_PATH.exists():
        # lock 超过 10 分钟视为上次进程异常退出，强制清理
        age = time.time() - _LOCK_PATH.stat().st_mtime
        if age < 600:
            logger.debug("Digest: locked, skipping")
            return
        logger.warning("Digest: stale lock (%.0fs old), removing", age)
        _LOCK_PATH.unlink(missing_ok=True)
    global _generated_at
    if _generated_at is None and _DIGEST_PATH.exists():
        _generated_at = datetime.fromtimestamp(_DIGEST_PATH.stat().st_mtime).isoformat()

    if not force_full and not should_regenerate_full():
        logger.debug("Digest: still fresh, skipping full generation")
        return

    try:
        _LOCK_PATH.touch()
        await generate_digest(llm)
    except Exception as e:
        logger.error("Digest full generation failed: %s", e)
    finally:
        _LOCK_PATH.unlink(missing_ok=True)


async def maybe_generate_changelog(llm) -> None:
    """每小时定时调用：增量 Change Log。"""
    if _LOCK_PATH.exists():
        return
    try:
        _LOCK_PATH.touch()
        await generate_changelog(llm)
    except Exception as e:
        logger.error("Digest changelog failed: %s", e)
    finally:
        _LOCK_PATH.unlink(missing_ok=True)


def get_status() -> dict:
    return {
        "generating": _generating,
        "generated_at": _generated_at,
    }


def get_debug_info() -> dict:
    """返回上次采集的缓存数据，不触发任何新的采集，立即返回。"""
    from lumina.digest.config import get_cfg
    cfg = get_cfg()
    return {
        "config": {
            "scan_dirs": cfg.scan_dirs,
            "history_hours": cfg.history_hours,
            "refresh_hours": cfg.refresh_hours,
        },
        "activity_check": _last_activity_check,
        "collectors": _last_collector_results,
    }
