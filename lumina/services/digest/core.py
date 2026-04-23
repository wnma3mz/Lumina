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
import json
import logging
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from lumina.config import DIGEST_COLLECTOR_STATE_PATH as _COLLECTOR_STATE_PATH
from lumina.config import DIGEST_CONTEXT_LOG_DIR as _CONTEXT_LOG_DIR
from lumina.config import DIGEST_PATH as _DIGEST_PATH
from lumina.services.digest.collectors import COLLECTORS as _COLLECTORS
from lumina.services.digest.config import get_cfg, override_history_hours
from lumina.engine.request_context import request_context

logger = logging.getLogger("lumina.services.digest")

# 当前进程启动时间，用于识别旧状态
_PROCESS_STARTED_TS = time.time()


def _save_collector_state(results: dict) -> None:
    """持久化最近一次 collector 采集快照，供重启后恢复 UI 状态。"""
    import uuid
    try:
        _COLLECTOR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": datetime.now().astimezone().isoformat(),
            "process_started_ts": _PROCESS_STARTED_TS,
            "collectors": results,
        }
        tmp = _COLLECTOR_STATE_PATH.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(_COLLECTOR_STATE_PATH)
    except Exception as e:
        logger.debug("Digest: failed to save collector state: %s", e)
        if "tmp" in locals():
            tmp.unlink(missing_ok=True)


def _load_collector_state() -> dict:
    """读取最近一次持久化的 collector 快照，失败时返回空 dict。"""
    try:
        data = json.loads(_COLLECTOR_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.debug("Digest: failed to load collector state: %s", e)
        return {}

    collectors = data.get("collectors")
    return collectors if isinstance(collectors, dict) else {}


# ── DigestState ───────────────────────────────────────────────────────────────

class DigestState:
    """线程安全的 Digest 状态容器，替代原模块级可变变量。

    使用 threading.Lock 保护所有状态字段的读写；asyncio.Lock 懒初始化，
    确保在 event loop 内创建，防止同一 event loop 内两个协程并发生成 digest。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._digest_lock: Optional[asyncio.Lock] = None
        self._report_locks: dict = {}  # (report_type, key) -> asyncio.Lock
        self.generating: bool = False
        self.generated_at: Optional[str] = None
        self.last_generated_ts: Optional[float] = None
        self.last_collector_results: dict = {}

    def get_digest_lock(self) -> asyncio.Lock:
        """懒初始化 asyncio.Lock，确保在 event loop 内创建。"""
        if self._digest_lock is None:
            self._digest_lock = asyncio.Lock()
        return self._digest_lock

    def get_report_lock(self, report_type: str, key: str) -> asyncio.Lock:
        """为每个 (report_type, key) 组合懒初始化独立的 asyncio.Lock。"""
        lock_key = (report_type, key)
        if lock_key not in self._report_locks:
            self._report_locks[lock_key] = asyncio.Lock()
        return self._report_locks[lock_key]

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
        _save_collector_state(results)

    def sync_collector_results(self) -> None:
        """进程重启后，从持久化快照恢复最近一次 collector 状态。"""
        with self._lock:
            if self.last_collector_results:
                return
        collectors = _load_collector_state()
        if not collectors:
            return
        with self._lock:
            if not self.last_collector_results:
                self.last_collector_results = collectors

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

    不再使用长期单例的 ThreadPoolExecutor 以避免超时任务堆积导致资源耗尽。
    每次采集单独创建一个 Executor，并在 finally 中进行 shutdown(wait=False)。
    这样即使主任务超时，后台线程能够被正常孤立并在执行完后回收。
    collector 的 history_hours 覆盖在工作线程内生效，避免与配置热重载竞争。
    """

    TIMEOUT = 30

    async def run_all(self, collectors: list, effective_hours: float) -> dict:
        """并发执行所有 collector，返回 {fn_name: result_or_Exception}。"""
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="lumina_collector")

        def _run_with_effective_hours(fn):
            with override_history_hours(effective_hours):
                return fn()

        async def _run_one(fn):
            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(executor, _run_with_effective_hours, fn),
                    timeout=self.TIMEOUT,
                )
                logger.debug("Digest: collector %s done in %.2fs", fn.__name__, time.monotonic() - t0)
                return result
            except asyncio.TimeoutError:
                logger.warning(
                    "Digest: collector %s timed out after %ds", fn.__name__, self.TIMEOUT
                )
                return Exception(f"timeout after {self.TIMEOUT}s")
            except Exception as e:
                logger.warning("Digest: collector %s raised: %s", fn.__name__, e)
                return e

        try:
            results = await asyncio.gather(*[_run_one(fn) for fn in collectors])
            return {fn.__name__: r for fn, r in zip(collectors, results)}
        finally:
            # 无论成功或超时，让 executor 在后台结束，不阻塞主协程
            executor.shutdown(wait=False)


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


def _list_context_logs(label: str) -> list[Path]:
    try:
        return sorted(_CONTEXT_LOG_DIR.glob(f"*_{label}.txt"))
    except Exception:
        return []


def _normalize_dedupe_line(line: str) -> str:
    return " ".join(line.strip().split())


def _split_context_sections(context: str) -> list[str]:
    return [part for part in context.split("\n\n---\n\n") if part.strip()]


def _extract_section_key(section: str) -> Optional[str]:
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return stripped
        if stripped.startswith("### "):
            return stripped
    return None


def _build_recent_section_lines(recent_contexts: list[str]) -> dict[str, set[str]]:
    recent_lines: dict[str, set[str]] = {}
    for context in recent_contexts:
        for section in _split_context_sections(context):
            section_key = _extract_section_key(section)
            if not section_key:
                continue
            bucket = recent_lines.setdefault(section_key, set())
            for line in section.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                bucket.add(_normalize_dedupe_line(line))
    return recent_lines


def _dedupe_context_against_recent(context: str, recent_contexts: list[str]) -> str:
    recent_lines = _build_recent_section_lines(recent_contexts)
    deduped_sections: list[str] = []
    removed = 0

    for section in _split_context_sections(context):
        section_key = _extract_section_key(section)
        if not section_key:
            deduped_sections.append(section)
            continue

        seen_lines = recent_lines.setdefault(section_key, set())
        new_lines: list[str] = []
        for line in section.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue

            normalized = _normalize_dedupe_line(line)
            if normalized in seen_lines:
                removed += 1
                continue
            seen_lines.add(normalized)
            new_lines.append(line)

        deduped_sections.append("\n".join(new_lines).strip())

    if removed:
        logger.info("Digest: removed %d duplicate lines against recent raw contexts", removed)
    return "\n\n---\n\n".join(section for section in deduped_sections if section.strip())


async def generate_digest(llm) -> str:
    """生成摘要，作为最新一条插入 digest.md 头部，历史记录永久保留。同时保存快照。"""
    _state.set_generating(True)
    try:
        context = await _collect_all()
        _save_context_log(context, "raw")
        raw_logs = _list_context_logs("raw")
        recent_contexts = []
        for path in reversed(raw_logs[:-1]):
            try:
                recent_contexts.append(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.debug("Digest: failed to read raw context log %s: %s", path, e)
            if len(recent_contexts) >= 3:
                break
        context = _dedupe_context_against_recent(context, recent_contexts)
        _save_context_log(context, "full")
        logger.info("Digest: generating summary...")
        with request_context(origin="digest", stream=False):
            from lumina.config import get_config
            import dataclasses
            kwargs = {"max_tokens": 600}
            sampling = getattr(get_config().digest, "sampling", {})
            if sampling:
                s_dict = dataclasses.asdict(sampling) if dataclasses.is_dataclass(sampling) else dict(sampling)
                kwargs.update({k: v for k, v in s_dict.items() if v is not None})
            summary = await llm.generate(
                context, task="digest",
                **kwargs
            )
        now = datetime.now()
        entry = (f"<!-- generated: {now.isoformat()} -->\n"
                 f"# {now.strftime('%Y-%m-%d %H:%M')}\n\n"
                 + summary.strip() + "\n")
        _prepend_entry(entry)
        _state.set_generated(now.timestamp())
        logger.info("Digest: saved to %s", _DIGEST_PATH)
        # 同时保存快照，供日报生成使用
        try:
            from lumina.services.digest.reports import save_snapshot
            save_snapshot(entry, now)
        except Exception as e:
            logger.warning("Digest: failed to save snapshot: %s", e)
        return entry
    finally:
        _state.set_generating(False)


async def generate_report(llm, report_type: str, key: str) -> Optional[str]:
    """生成指定类型的报告（daily/weekly/monthly）并保存，返回内容。

    report_type: "daily" | "weekly" | "monthly"
    key:         对应格式的日期键（日报: YYYY-MM-DD，周报: YYYY-Www，月报: YYYY-MM）

    用 per-(type, key) asyncio.Lock 防止同一报告被并发重复生成。
    """
    from datetime import date
    from lumina.services.digest.reports import (
        build_daily_input, build_weekly_input, build_monthly_input,
        save_report,
    )

    # task 名对应 config.system_prompts 中的 key，由 LLMEngine._resolve_system 查找
    task_names = {
        "daily": "daily_report",
        "weekly": "weekly_report",
        "monthly": "monthly_report",
    }
    builders = {
        "daily": lambda: build_daily_input(date.fromisoformat(key)),
        "weekly": lambda: build_weekly_input(key),
        "monthly": lambda: build_monthly_input(key),
    }
    max_tokens_map = {"daily": 700, "weekly": 900, "monthly": 1100}

    if report_type not in task_names:
        raise ValueError(f"Unknown report_type: {report_type!r}")

    lock = _state.get_report_lock(report_type, key)
    if lock.locked():
        logger.debug("Report(%s/%s): already generating, skipping", report_type, key)
        return None

    async with lock:
        input_text = builders[report_type]()
        if not input_text:
            logger.warning("Report(%s/%s): no input data available", report_type, key)
            return None

        logger.info("Report(%s/%s): generating...", report_type, key)
        with request_context(origin="digest", stream=False):
            from lumina.config import get_config
            import dataclasses
            kwargs = {"max_tokens": max_tokens_map[report_type]}
            sampling = getattr(get_config().digest, "sampling", {})
            if sampling:
                s_dict = dataclasses.asdict(sampling) if dataclasses.is_dataclass(sampling) else dict(sampling)
                kwargs.update({k: v for k, v in s_dict.items() if v is not None})
            content = await llm.generate(
                input_text, task=task_names[report_type],
                **kwargs
            )

        header = f"<!-- generated: {datetime.now().astimezone().isoformat()} -->\n"
        full_content = header + content.strip() + "\n"
        save_report(report_type, key, full_content)
        logger.info("Report(%s/%s): saved", report_type, key)
        return full_content


# ── 对外接口 ──────────────────────────────────────────────────────────────────

def load_digest() -> Optional[str]:
    if not _DIGEST_PATH.exists():
        return None
    return _DIGEST_PATH.read_text(encoding="utf-8")


async def maybe_generate_digest(llm, force_full: bool = False) -> None:
    """定时/启动时调用。用 asyncio.Lock 防并发。"""
    if not get_cfg().enabled:
        logger.info("Digest disabled, skipping generation")
        return
    _state.sync_from_digest_file()

    # 距上次生成不足 refresh_hours 则跳过，避免启动时重复采集
    cfg = get_cfg()
    if not force_full:
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
    from lumina.services.digest.collectors.files import _last_md_files
    cfg = get_cfg()
    _state.sync_collector_results()
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
