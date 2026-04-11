"""
lumina/digest/core.py — 摘要生成、状态管理

──────────────────────────────────────────────────────────────────────────────
Digest 机制说明
──────────────────────────────────────────────────────────────────────────────

Lumina 每小时运行一次摘要生成，每次都取过去 history_hours（默认 24h）内各来源
最新的 top N 条记录。没有增量/全量之分，每次都是当前时刻回头看的完整快照。

生成结果写入 ~/.lumina/digest.md（新条目 prepend 到头部，历史永久保留）。

每日通知（daily notify，在 main.py 中触发）：
  ─ 触发：每天到 config.notify_time（默认 20:00）的那次定时器
  ─ 行为：调用 generate_digest（与普通定时器相同）
  ─ 额外：通过系统通知推送给用户

关键状态变量：
  _generated_at  ISO 时间字符串，供 API /v1/digest 返回给前端展示
  _generating    布尔，防止并发生成，前端可轮询此字段显示 loading 状态
──────────────────────────────────────────────────────────────────────────────
"""
import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from lumina.digest.config import get_cfg, override_history_hours
from lumina.digest.collectors import (
    collect_shell_history,
    collect_git_logs,
    collect_clipboard,
    collect_browser_history,
    collect_notes_app,
    collect_calendar,
    collect_markdown_notes,
    collect_ai_queries,
)

logger = logging.getLogger("lumina.digest")

_DIGEST_PATH   = Path.home() / ".lumina" / "digest.md"
_CONTEXT_LOG_DIR = Path.home() / ".lumina" / "digest_context_log"

# 模块级状态，供 API 查询；asyncio 协程与 threading.Timer 线程共享，需加锁保护
_state_lock = threading.Lock()
# asyncio.Lock：真正防止同一 event loop 内两个协程并发生成 digest
# 注意：必须在 event loop 内使用（maybe_generate_digest 为 async），不跨进程
_digest_lock: Optional[asyncio.Lock] = None
_generating: bool = False
_generated_at: Optional[str] = None
_last_generated_ts: Optional[float] = None
# 上次 collector 结果缓存（key=函数名, value={chars, lines, preview, error}）
_last_collector_results: dict = {}

# 当前进程启动时间，用于识别旧状态
_PROCESS_STARTED_TS = time.time()

# 模块级 collector 线程池，进程生命周期内复用，避免超时后线程累积
_COLLECT_EXECUTOR: Optional[ThreadPoolExecutor] = None


def _get_collect_executor() -> ThreadPoolExecutor:
    global _COLLECT_EXECUTOR
    if _COLLECT_EXECUTOR is None:
        _COLLECT_EXECUTOR = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="lumina_collector"
        )
    return _COLLECT_EXECUTOR


def _get_digest_lock() -> asyncio.Lock:
    """懒初始化 asyncio.Lock，确保在 event loop 内创建。"""
    global _digest_lock
    if _digest_lock is None:
        _digest_lock = asyncio.Lock()
    return _digest_lock

# 注册的采集器列表——新增数据源在此追加
_COLLECTORS = [
    collect_shell_history,
    collect_git_logs,
    collect_clipboard,
    collect_browser_history,
    collect_calendar,
    collect_notes_app,
    collect_markdown_notes,
    collect_ai_queries,
]

DIGEST_SYSTEM_PROMPT = """\
你是用户的本地工作上下文助手，不是日报生成器。

你的任务是根据用户最近的本地活动记录，帮助用户快速回答这几个问题：
1. 我最近主要在做什么？
2. 现在最可能处于哪个任务或项目阶段？
3. 接下来最自然的下一步是什么？

请遵循以下原则：
- 优先提炼最核心的 1-3 个项目、任务或主题，不要平均概括所有活动。
- 重点写对继续工作有帮助的信息，例如项目、文件、主题、操作、未完成事项、阻塞点、待确认点。
- 终端命令、Git 提交、笔记、AI 对话通常比零散网页浏览更能反映真实工作重点。
- 剪贴板、浏览器历史这类信息只有在明显相关时才写入。
- 如果信息不足，不要臆测；可以保守表达。
- 不要写空泛总结，不要写鼓励语，不要重复原始记录。

请用 Markdown 输出，格式固定为：

## 最近在推进
用 2-4 句说明最近主要在做什么，尽量点出项目、主题或任务。

## 当前上下文
用 2-4 条简短要点说明当前做到哪、卡在哪、哪些线索值得记住。

## 下一步建议
用 2-3 条简短要点写最自然的后续动作，必须基于已有记录，不要发散。

整体简洁、具体、可继续，总长度控制在 220-350 字。"""


# ── 采集 ──────────────────────────────────────────────────────────────────────

async def _collect_all() -> str:
    """采集所有数据源，始终使用 cfg.history_hours 窗口的完整快照。"""
    global _last_collector_results
    cfg = get_cfg()
    effective_hours = cfg.history_hours

    active = _COLLECTORS
    if cfg.enabled_collectors is not None:
        enabled_set = set(cfg.enabled_collectors)
        active = [fn for fn in _COLLECTORS if fn.__name__ in enabled_set]

    logger.info(
        "Digest collect start: effective_hours=%.2f active_collectors=%s",
        effective_hours,
        [fn.__name__ for fn in active],
    )

    # 每个 collector 独立超时 30s，超时直接视为异常，不 block 其他 collector
    _COLLECTOR_TIMEOUT = 30

    loop = asyncio.get_running_loop()
    executor = _get_collect_executor()

    async def _run_with_timeout(fn):
        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(executor, fn),
                timeout=_COLLECTOR_TIMEOUT,
            )
            logger.debug("Digest: collector %s done in %.2fs", fn.__name__, time.time() - t0)
            return result
        except asyncio.TimeoutError:
            logger.warning("Digest: collector %s timed out after %ds", fn.__name__, _COLLECTOR_TIMEOUT)
            return Exception(f"timeout after {_COLLECTOR_TIMEOUT}s")

    with override_history_hours(effective_hours):
        results = await asyncio.gather(*[_run_with_timeout(fn) for fn in active])

    cache = {}
    sections = []
    for fn, r in zip(active, results):
        name = fn.__name__
        if isinstance(r, Exception):
            cache[name] = {"chars": 0, "lines": 0, "preview": None,
                           "error": str(r)}
            logger.warning("Digest: collector %s error: %s", name, str(r))
        else:
            text = r or ""
            if text == "__PERMISSION_DENIED__":
                cache[name] = {
                    "chars": 0, "lines": 0, "preview": None,
                    "permission_denied": True,
                    "error": None,
                }
                logger.warning("Digest: collector %s permission denied", name)
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
                logger.debug("Digest: collector %s → %d chars", name, len(text))
    with _state_lock:
        _last_collector_results = cache

    logger.info(
        "Digest collect done: %d/%d collectors produced content, total_chars=%d",
        len(sections), len(active), sum(len(s) for s in sections),
    )

    if not sections:
        return "（未采集到任何本地活动记录）"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"当前时间：{now_str}\n\n" + "\n\n---\n\n".join(sections)


# ── 摘要生成 ──────────────────────────────────────────────────────────────────

def _prepend_entry(entry: str) -> None:
    """将新条目插入 digest.md 最前面，旧内容跟在后面（以 \n---\n 分隔）。

    原子写入：使用随机 UUID 命名的 .tmp 文件，避免多写者场景下 tmp 文件互相覆盖。
    调用方须持有 _digest_lock 后再调用此函数。
    """
    _DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _DIGEST_PATH.exists():
        try:
            old = _DIGEST_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            old = ""
        new_content = entry + "\n---\n\n" + old + "\n" if old else entry
    else:
        new_content = entry
    # 随机 tmp 名防止并发写者互相覆盖（_digest_lock 已防并发，此处是额外保险）
    tmp = _DIGEST_PATH.with_name(f"digest.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(new_content, encoding="utf-8")
        tmp.replace(_DIGEST_PATH)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _save_context_log(context: str, label: str) -> None:
    """将送给 LLM 的原始上下文保存到 ~/.lumina/digest_context_log/，保留最近 20 份。"""
    try:
        _CONTEXT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = _CONTEXT_LOG_DIR / f"{ts}_{label}.txt"
        log_file.write_text(context, encoding="utf-8")
        # 只保留最近 20 份，按文件名（时间戳）排序，删最旧的
        files = sorted(_CONTEXT_LOG_DIR.glob("*.txt"))
        for old in files[:-20]:
            old.unlink(missing_ok=True)
    except Exception as e:
        logger.debug("Digest: failed to save context log: %s", e)


async def generate_digest(llm) -> str:
    """生成摘要，作为最新一条插入 digest.md 头部，历史记录永久保留。"""
    global _generating, _generated_at, _last_generated_ts
    with _state_lock:
        _generating = True
    try:
        context = await _collect_all()
        _save_context_log(context, "full")
        logger.info("Digest: generating summary...")
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
        with _state_lock:
            _generated_at = now.isoformat()
            _last_generated_ts = now.timestamp()
        logger.info("Digest: saved to %s", _DIGEST_PATH)
        return entry
    finally:
        with _state_lock:
            _generating = False


# ── 对外接口 ──────────────────────────────────────────────────────────────────

def _sync_status_from_digest_file() -> None:
    """进程重启后，从已有 digest.md 恢复最近一次生成时间。"""
    global _generated_at, _last_generated_ts
    with _state_lock:
        if _generated_at is not None:
            return
    if not _DIGEST_PATH.exists():
        return
    try:
        mtime = _DIGEST_PATH.stat().st_mtime
    except OSError:
        return
    with _state_lock:
        _generated_at = datetime.fromtimestamp(mtime).isoformat()
        _last_generated_ts = mtime


def load_digest() -> Optional[str]:
    if not _DIGEST_PATH.exists():
        return None
    return _DIGEST_PATH.read_text(encoding="utf-8")


async def maybe_generate_digest(llm, force_full: bool = False) -> None:
    """定时/启动时调用。用 asyncio.Lock 防并发。force_full 忽略不用。"""
    if not get_cfg().enabled:
        logger.info("Digest disabled, skipping generation")
        return
    _sync_status_from_digest_file()

    lock = _get_digest_lock()
    if lock.locked():
        logger.debug("Digest: locked, skipping")
        return

    async with lock:
        try:
            await generate_digest(llm)
        except Exception as e:
            logger.error("Digest generation failed: %s", e)


def get_status() -> dict:
    _sync_status_from_digest_file()
    with _state_lock:
        return {
            "enabled": get_cfg().enabled,
            "generating": _generating,
            "generated_at": _generated_at,
        }


def get_debug_info() -> dict:
    """返回上次采集的缓存数据，不触发任何新的采集，立即返回。"""
    from lumina.digest.config import get_cfg
    import lumina.digest.collectors as _col
    cfg = get_cfg()
    with _state_lock:
        last_ts = _last_generated_ts
        collectors_snapshot = dict(_last_collector_results)
    return {
        "config": {
            "scan_dirs": cfg.scan_dirs,
            "history_hours": cfg.history_hours,
            "refresh_hours": cfg.refresh_hours,
        },
        "last_generated_ts": last_ts,
        "collectors": collectors_snapshot,
        "md_files": _col._last_md_files,
    }
