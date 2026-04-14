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

关键类：
  DigestState     — 线程安全的状态容器，替代原模块级可变变量
  CollectorRunner — 封装 ThreadPoolExecutor + asyncio.wait_for 超时逻辑
──────────────────────────────────────────────────────────────────────────────
"""
import asyncio
import logging
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from lumina.config import DIGEST_CONTEXT_LOG_DIR as _CONTEXT_LOG_DIR
from lumina.config import DIGEST_PATH as _DIGEST_PATH
from lumina.digest.collectors import COLLECTORS as _COLLECTORS
from lumina.digest.config import get_cfg, override_history_hours
from lumina.request_context import request_context

logger = logging.getLogger("lumina.digest")

# 当前进程启动时间，用于识别旧状态
_PROCESS_STARTED_TS = time.time()

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


# ── DigestState ───────────────────────────────────────────────────────────────

class DigestState:
    """线程安全的 Digest 状态容器，替代原模块级可变变量。

    使用 threading.Lock 保护所有状态字段的读写；asyncio.Lock 懒初始化，
    确保在 event loop 内创建，防止同一 event loop 内两个协程并发生成 digest。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._digest_lock: Optional[asyncio.Lock] = None
        self.generating: bool = False
        self.generated_at: Optional[str] = None
        self.last_generated_ts: Optional[float] = None
        self.last_collector_results: dict = {}

    def get_digest_lock(self) -> asyncio.Lock:
        """懒初始化 asyncio.Lock，确保在 event loop 内创建。"""
        if self._digest_lock is None:
            self._digest_lock = asyncio.Lock()
        return self._digest_lock

    def set_generating(self, val: bool) -> None:
        with self._lock:
            self.generating = val

    def set_generated(self, ts: float) -> None:
        with self._lock:
            self.generated_at = datetime.fromtimestamp(ts).isoformat()
            self.last_generated_ts = ts

    def set_collector_results(self, results: dict) -> None:
        with self._lock:
            self.last_collector_results = results

    def sync_from_digest_file(self) -> None:
        """进程重启后，从已有 digest.md 恢复最近一次生成时间。"""
        with self._lock:
            if self.generated_at is not None:
                return
        if not _DIGEST_PATH.exists():
            return
        try:
            mtime = _DIGEST_PATH.stat().st_mtime
        except OSError:
            return
        with self._lock:
            self.generated_at = datetime.fromtimestamp(mtime).isoformat()
            self.last_generated_ts = mtime

    def to_status(self) -> dict:
        with self._lock:
            return {
                "generating": self.generating,
                "generated_at": self.generated_at,
            }


# 模块级单例
_state = DigestState()


# ── CollectorRunner ───────────────────────────────────────────────────────────

class CollectorRunner:
    """封装 ThreadPoolExecutor + asyncio.wait_for 超时逻辑。

    进程生命周期内复用线程池，避免超时后线程累积。
    每个 collector 独立 30s 超时，超时视为异常，不 block 其他 collector。
    """

    TIMEOUT = 30

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="lumina_collector"
        )

    async def run_all(self, collectors: list, effective_hours: float) -> dict:
        """并发执行所有 collector，返回 {fn_name: result_or_Exception}。"""
        loop = asyncio.get_running_loop()

        async def _run_one(fn):
            t0 = time.time()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, fn),
                    timeout=self.TIMEOUT,
                )
                logger.debug("Digest: collector %s done in %.2fs", fn.__name__, time.time() - t0)
                return result
            except asyncio.TimeoutError:
                logger.warning(
                    "Digest: collector %s timed out after %ds", fn.__name__, self.TIMEOUT
                )
                return Exception(f"timeout after {self.TIMEOUT}s")
            except Exception as e:
                logger.warning("Digest: collector %s raised: %s", fn.__name__, e)
                return e

        with override_history_hours(effective_hours):
            results = await asyncio.gather(*[_run_one(fn) for fn in collectors])
        return {fn.__name__: r for fn, r in zip(collectors, results)}


# 模块级单例
_runner = CollectorRunner()


# ── 采集 ──────────────────────────────────────────────────────────────────────

async def _collect_all() -> str:
    """采集所有数据源，始终使用 cfg.history_hours 窗口的完整快照。"""
    cfg = get_cfg()
    effective_hours = cfg.history_hours

    active = list(_COLLECTORS)
    if cfg.enabled_collectors is not None:
        enabled_set = set(cfg.enabled_collectors)
        active = [fn for fn in active if fn.__name__ in enabled_set]
    random.shuffle(active)

    logger.info(
        "Digest collect start: effective_hours=%.2f active_collectors=%s",
        effective_hours,
        [fn.__name__ for fn in active],
    )

    results = await _runner.run_all(active, effective_hours)

    cache: dict = {}
    sections: list[str] = []
    for name, r in results.items():
        if isinstance(r, Exception):
            cache[name] = {"chars": 0, "lines": 0, "preview": None, "error": str(r)}
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

    _state.set_collector_results(cache)

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
    调用方须持有 _state.get_digest_lock() 后再调用此函数。
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
    _state.set_generating(True)
    try:
        context = await _collect_all()
        _save_context_log(context, "full")
        logger.info("Digest: generating summary...")
        with request_context(origin="digest", stream=False):
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
        _state.set_generated(now.timestamp())
        logger.info("Digest: saved to %s", _DIGEST_PATH)
        return entry
    finally:
        _state.set_generating(False)


# ── 对外接口 ──────────────────────────────────────────────────────────────────

def load_digest() -> Optional[str]:
    if not _DIGEST_PATH.exists():
        return None
    return _DIGEST_PATH.read_text(encoding="utf-8")


async def maybe_generate_digest(llm, force_full: bool = False) -> None:
    """定时/启动时调用。用 asyncio.Lock 防并发。force_full 忽略不用。"""
    if not get_cfg().enabled:
        logger.info("Digest disabled, skipping generation")
        return
    _state.sync_from_digest_file()

    # 距上次生成不足 refresh_hours 则跳过，避免启动时重复采集
    cfg = get_cfg()
    with _state._lock:
        last_ts = _state.last_generated_ts
    if last_ts is not None:
        elapsed = time.time() - last_ts
        cooldown = cfg.refresh_hours * 3600
        if elapsed < cooldown:
            logger.info(
                "Digest: skipping, last generated %.0f min ago (cooldown %.0f min)",
                elapsed / 60, cooldown / 60,
            )
            return

    lock = _state.get_digest_lock()
    if lock.locked():
        logger.debug("Digest: locked, skipping")
        return

    async with lock:
        try:
            await generate_digest(llm)
        except Exception as e:
            logger.error("Digest generation failed: %s", e)


def get_status() -> dict:
    _state.sync_from_digest_file()
    status = _state.to_status()
    status["enabled"] = get_cfg().enabled
    return status


def get_debug_info() -> dict:
    """返回上次采集的缓存数据，不触发任何新的采集，立即返回。"""
    from lumina.digest.collectors.files import _last_md_files
    cfg = get_cfg()
    with _state._lock:
        last_ts = _state.last_generated_ts
        collectors_snapshot = dict(_state.last_collector_results)
    return {
        "config": {
            "scan_dirs": cfg.scan_dirs,
            "history_hours": cfg.history_hours,
            "refresh_hours": cfg.refresh_hours,
        },
        "last_generated_ts": last_ts,
        "collectors": collectors_snapshot,
        "md_files": _last_md_files,
    }
