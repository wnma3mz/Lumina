"""
lumina/digest/core.py — 摘要生成、增量检测、状态管理
"""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import lumina.digest.collectors as _collectors_mod
from lumina.digest.config import get_cfg, override_history_hours
from lumina.digest.cursor_store import load_cursors, save_cursors
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

# 模块级状态，供 API 查询
_generating: bool = False
_generated_at: Optional[str] = None

# 上次 collector 结果缓存（key=函数名, value={chars, lines, preview, error}）
_last_collector_results: dict = {}

# 上次成功生成日报的时间戳（秒），用于计算增量采集窗口
_last_generated_ts: Optional[float] = None

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


# ── 采集 ──────────────────────────────────────────────────────────────────────

async def _collect_all(since_ts: Optional[float] = None) -> str:
    """采集所有数据源。

    since_ts: 上次生成的时间戳（秒）。若提供，采集窗口 = min(距今时长, max_hours)；
              否则使用 config.history_hours（全量，首次启动或强制刷新时）。
    """
    global _last_collector_results
    cfg = get_cfg()
    if since_ts is not None:
        elapsed_hours = (time.time() - since_ts) / 3600
        effective_hours = min(elapsed_hours, cfg.history_hours)
        # 至少 5 分钟，避免极短窗口导致采集为空
        effective_hours = max(effective_hours, 5 / 60)
    else:
        effective_hours = cfg.history_hours

    # ── 注入 per-collector cursor ─────────────────────────────────────────────
    cursors = load_cursors()
    cursors["_fallback"] = time.time() - effective_hours * 3600
    _collectors_mod._CURSORS = cursors          # 线程启动前写入，collector 只读

    loop = asyncio.get_running_loop()
    with override_history_hours(effective_hours):
        with ThreadPoolExecutor(max_workers=len(_COLLECTORS)) as executor:
            futures = [loop.run_in_executor(executor, fn) for fn in _COLLECTORS]
            results = await asyncio.gather(*futures, return_exceptions=True)

    # ── 保存 collector 写回的 cursor（去掉内部哨兵 key）─────────────────────
    updated = {k: v for k, v in _collectors_mod._CURSORS.items()
               if not k.startswith("_")}
    save_cursors(updated)

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
            if text == "__PERMISSION_DENIED__":
                cache[name] = {
                    "chars": 0, "lines": 0, "preview": None,
                    "permission_denied": True,
                    "error": None,
                }
            else:
                cache[name] = {
                    "chars": len(text),
                    "lines": text.count("\n"),
                    "preview": text[:300] if text else None,
                    "permission_denied": False,
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
    global _generating, _generated_at, _last_generated_ts
    _generating = True
    try:
        # 全量生成不传 since_ts，使用 config.history_hours
        context = await _collect_all(since_ts=None)
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
        _last_generated_ts = now.timestamp()
        logger.info("Digest: saved to %s", _DIGEST_PATH)
        return entry
    finally:
        _generating = False


async def generate_changelog(llm) -> Optional[str]:
    """增量 Change Log：采集新活动并追加条目，collector 无新数据时 LLM 自行判断跳过。"""
    global _generating, _generated_at, _last_generated_ts
    _generating = True
    try:
        # 增量生成：只采集上次生成到现在的数据
        context = await _collect_all(since_ts=_last_generated_ts)
        elapsed = (time.time() - _last_generated_ts) / 3600 if _last_generated_ts else None
        logger.info("Digest: generating changelog (window=%.1fh)...",
                    min(elapsed, get_cfg().history_hours) if elapsed else get_cfg().history_hours)
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
        _last_generated_ts = now.timestamp()
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
    global _generated_at, _last_generated_ts
    if _generated_at is None and _DIGEST_PATH.exists():
        mtime = _DIGEST_PATH.stat().st_mtime
        _generated_at = datetime.fromtimestamp(mtime).isoformat()
        _last_generated_ts = mtime

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
        "last_generated_ts": _last_generated_ts,
        "collectors": _last_collector_results,
        "cursors": load_cursors(),
    }
