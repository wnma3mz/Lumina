"""
lumina/request_history.py — LLM 请求历史记录

设计目标：
  - 推理热路径只做内存入队，不做同步磁盘写入
  - 当前日志为易读的 JSONL，历史日志按日 gzip 压缩
  - 支持按保留天数与总空间双阈值清理
"""
import gzip
import json
import logging
import queue
import shutil
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from lumina.config import REQUEST_HISTORY_DIR, RequestHistoryConfig

logger = logging.getLogger("lumina")

_QUEUE_MAX_ENTRIES = 1024
_MAINTENANCE_INTERVAL_SECONDS = 300.0
_STOP = object()


def _parse_day(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


class RequestHistoryRecorder:
    def __init__(self, base_dir: Path, queue_max_entries: int = _QUEUE_MAX_ENTRIES):
        self._base_dir = base_dir
        self._current_dir = base_dir / "current"
        self._archive_dir = base_dir / "archive"
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=max(1, queue_max_entries))
        self._cfg = RequestHistoryConfig()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._fh = None
        self._current_day: Optional[str] = None
        self._last_maintenance = 0.0
        self._drop_warning_at = 0.0

    def configure(self, cfg: RequestHistoryConfig, *, run_startup_cleanup: bool = False) -> None:
        with self._lock:
            self._cfg = cfg
            self._ensure_dirs_locked()
            if cfg.enabled and (self._thread is None or not self._thread.is_alive()):
                self._start_worker_locked()
            elif not cfg.enabled and self._thread is not None:
                pass

        if cfg.enabled and run_startup_cleanup and cfg.cleanup_on_startup:
            self.prune_now()
        elif not cfg.enabled:
            self._stop_worker(flush=True)

    def get_cfg(self) -> RequestHistoryConfig:
        with self._lock:
            return self._cfg

    def record(self, entry: Dict[str, Any]) -> None:
        cfg = self.get_cfg()
        if not cfg.enabled:
            return

        if not cfg.capture_full_body:
            entry = dict(entry)
            entry["system_text"] = None
            entry["user_text"] = None
            entry["response_text"] = None

        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            now = time.time()
            if now >= self._drop_warning_at:
                logger.warning(
                    "Request history queue full, dropping records (max=%d)",
                    self._queue.maxsize,
                )
                self._drop_warning_at = now + 60.0

    def flush(self, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.time() + max(0.0, timeout)
        while self._queue.unfinished_tasks:
            if deadline is not None and time.time() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def shutdown(self, flush_timeout: float = 5.0) -> None:
        self._stop_worker(flush=True, flush_timeout=flush_timeout)

    def prune_now(self) -> Dict[str, int]:
        self.flush(timeout=5.0)
        with self._lock:
            return self._maintenance_locked(force=True)

    def _ensure_dirs_locked(self) -> None:
        self._current_dir.mkdir(parents=True, exist_ok=True)
        self._archive_dir.mkdir(parents=True, exist_ok=True)

    def _start_worker_locked(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name="lumina_request_history",
        )
        self._thread.start()

    def _stop_worker(self, *, flush: bool, flush_timeout: float = 5.0) -> None:
        if flush:
            self.flush(timeout=flush_timeout)
        with self._lock:
            thread = self._thread
            if thread is None:
                self._close_file_locked()
                return
            self._thread = None
            self._stop_event.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass
        thread.join(timeout=max(1.0, flush_timeout))
        with self._lock:
            self._close_file_locked()

    def _writer_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop_event.is_set():
                    break
                self._maybe_run_maintenance()
                continue

            if item is _STOP:
                self._queue.task_done()
                break

            try:
                self._write_entry(item)
            except Exception as e:
                logger.warning("Request history write failed: %s", e, exc_info=True)
            finally:
                self._queue.task_done()

            self._maybe_run_maintenance()

        with self._lock:
            self._close_file_locked()

    def _write_entry(self, entry: Dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        day = self._entry_day(entry)
        with self._lock:
            self._ensure_dirs_locked()
            self._ensure_current_file_locked(day)
            self._fh.write(line + "\n")
            self._fh.flush()

    def _entry_day(self, entry: Dict[str, Any]) -> str:
        ts = entry.get("ts_start") or entry.get("ts_end")
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts).date().isoformat()
            except Exception:
                pass
        return datetime.now().date().isoformat()

    def _ensure_current_file_locked(self, day: str) -> None:
        if self._current_day == day and self._fh is not None and not self._fh.closed:
            return
        self._close_file_locked()
        path = self._current_dir / f"{day}.jsonl"
        self._fh = open(path, "a", encoding="utf-8")
        self._current_day = day

    def _close_file_locked(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        self._fh = None
        self._current_day = None

    def _maybe_run_maintenance(self) -> None:
        now = time.monotonic()
        if now - self._last_maintenance < _MAINTENANCE_INTERVAL_SECONDS:
            return
        with self._lock:
            self._maintenance_locked(force=False)

    def _maintenance_locked(self, force: bool) -> Dict[str, int]:
        self._ensure_dirs_locked()
        stats = {"compressed": 0, "deleted": 0, "freed_bytes": 0}
        stats = self._rotate_old_current_locked(stats, force=force)
        stats = self._prune_locked(stats)
        self._last_maintenance = time.monotonic()
        return stats

    def _rotate_old_current_locked(self, stats: Dict[str, int], *, force: bool) -> Dict[str, int]:
        today = datetime.now().date()
        threshold = max(0, self._cfg.compress_after_days)

        for path in sorted(self._current_dir.glob("*.jsonl")):
            file_day = _parse_day(path.stem)
            if file_day is None or file_day >= today:
                continue
            age_days = (today - file_day).days
            if not force and age_days < threshold:
                continue

            if path.stem == self._current_day:
                self._close_file_locked()
            archive_path = self._archive_dir / f"{path.stem}.jsonl.gz"
            source_size = path.stat().st_size
            before_size = archive_path.stat().st_size if archive_path.exists() else 0
            
            # 临时写入 .tmp 文件，完成后重命名，保证原子性并避免 crash 导致数据损坏
            tmp_archive_path = archive_path.with_suffix(f".{uuid.uuid4().hex[:8]}.gz.tmp")
            with path.open("rb") as src, gzip.open(tmp_archive_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
                
            tmp_archive_path.replace(archive_path)
            after_size = archive_path.stat().st_size
            
            path.unlink(missing_ok=True)
            stats["compressed"] += 1
            stats["freed_bytes"] += max(0, source_size + before_size - after_size)
        return stats

    def _prune_locked(self, stats: Dict[str, int]) -> Dict[str, int]:
        today = datetime.now().date()
        retention = max(0, self._cfg.retention_days)
        cutoff = today - timedelta(days=retention)

        archived = []
        for path in sorted(self._archive_dir.glob("*.jsonl.gz")):
            file_day = _parse_day(path.name.replace(".jsonl.gz", ""))
            if file_day is None:
                continue
            size = path.stat().st_size
            archived.append((file_day, path, size))
            if file_day < cutoff:
                path.unlink(missing_ok=True)
                stats["deleted"] += 1
                stats["freed_bytes"] += size

        total_bytes = self._total_bytes_locked()
        limit_bytes = max(1, self._cfg.max_total_mb) * 1024 * 1024
        if total_bytes <= limit_bytes:
            return stats

        deletable = []
        for file_day, path, size in archived:
            if not path.exists():
                continue
            deletable.append((file_day, path, size))

        for _, path, size in sorted(deletable, key=lambda item: item[0]):
            if total_bytes <= limit_bytes:
                break
            if not path.exists():
                continue
            path.unlink(missing_ok=True)
            total_bytes -= size
            stats["deleted"] += 1
            stats["freed_bytes"] += size

        return stats

    def _total_bytes_locked(self) -> int:
        total = 0
        for path in self._current_dir.glob("*.jsonl"):
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                pass
        for path in self._archive_dir.glob("*.jsonl.gz"):
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                pass
        return total

    def total_bytes(self) -> int:
        """返回当前已使用的磁盘字节数（current + archive）。"""
        with self._lock:
            self._ensure_dirs_locked()
            return self._total_bytes_locked()


_cfg = RequestHistoryConfig()
_recorder = RequestHistoryRecorder(REQUEST_HISTORY_DIR)


def get_cfg() -> RequestHistoryConfig:
    try:
        from lumina.config import peek_config

        cfg = peek_config()
        if cfg is not None:
            return cfg.request_history
    except Exception:
        pass
    return _cfg


def get_recorder() -> RequestHistoryRecorder:
    return _recorder


def configure(data, *, run_startup_cleanup: bool = False) -> None:
    global _cfg

    if isinstance(data, RequestHistoryConfig):
        new_cfg = data
    else:
        node = data.get("request_history", {}) if isinstance(data, dict) else {}
        if not isinstance(node, dict):
            node = {}
        new_cfg = RequestHistoryConfig.model_validate(node)

    _cfg = new_cfg
    try:
        from lumina.config import peek_config

        cfg = peek_config()
        if cfg is not None:
            cfg.system.request_history = new_cfg
    except Exception:
        pass

    _recorder.configure(new_cfg, run_startup_cleanup=run_startup_cleanup)


def shutdown(flush_timeout: float = 5.0) -> None:
    _recorder.shutdown(flush_timeout=flush_timeout)


def record(entry: Dict[str, Any]) -> None:
    _recorder.record(entry)


def prune_now() -> Dict[str, int]:
    return _recorder.prune_now()


def _percentile(sorted_vals: list, p: float) -> int:
    """计算已排序列表的第 p 百分位数（0-100）。"""
    if not sorted_vals:
        return 0
    idx = (len(sorted_vals) - 1) * p / 100
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return round(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def query_stats(days: int = 7) -> Dict[str, Any]:
    """读取最近 days 天的请求日志，按 origin 聚合统计。

    返回字段：
      24h / 7d          — 按 origin 聚合的窗口数据
      prev_24h          — 前一个 24h 窗口（用于昨日对比）
      daily_counts      — 过去 days 天每日请求量列表（时序趋势）
      model_dist        — 7d 内各 provider_model 调用次数
      summary           — 全局汇总（total_24h/7d, avg_ms_7d, prev_total_24h）
    """
    from datetime import timezone

    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=days)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_prev_24h = now - timedelta(hours=48)  # 昨日同期起点

    recorder = _recorder
    current_dir = recorder._current_dir
    archive_dir = recorder._archive_dir

    def _iter_entries():
        if current_dir.exists():
            for f in sorted(current_dir.glob("*.jsonl")):
                try:
                    with open(f, encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if line:
                                try:
                                    yield json.loads(line)
                                except Exception:
                                    pass
                except Exception:
                    pass
        if archive_dir.exists():
            for f in sorted(archive_dir.glob("*.jsonl.gz")):
                date_str = f.name.replace(".jsonl.gz", "")
                try:
                    file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                    if file_date < cutoff_prev_24h:
                        continue
                except ValueError:
                    pass
                try:
                    with gzip.open(f, "rt", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if line:
                                try:
                                    yield json.loads(line)
                                except Exception:
                                    pass
                except Exception:
                    pass

    # origin 聚合 bucket 模板
    def _new_bucket() -> Dict[str, Any]:
        return {
            "count": 0,
            "ok": 0,
            "error": 0,
            "total_ms": 0,
            "durations": [],  # 用于 P95 计算，最终会删除
            "user_chars": 0,
            "resp_chars": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "token_count": 0,
            "avg_ms": 0,
            "p95_ms": 0,
        }

    buckets: Dict[str, Dict[str, Any]] = {"24h": {}, "7d": {}, "prev_24h": {}}

    # 按日期统计请求量（只统计 7d 窗口）
    daily_counts: Dict[str, int] = {}
    # 按小时统计请求量（只统计 24h 窗口，key = "HH"）
    hourly_counts: Dict[str, int] = {}
    # 7d 内各模型调用次数
    model_dist: Dict[str, int] = {}

    for entry in _iter_entries():
        ts_str = entry.get("ts_start", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        in_7d = ts >= cutoff_7d
        in_24h = ts >= cutoff_24h
        in_prev_24h = (ts >= cutoff_prev_24h) and (ts < cutoff_24h)

        if not in_7d and not in_prev_24h:
            continue

        origin = entry.get("origin") or "unknown"
        status = entry.get("status", "unknown")
        duration_ms = entry.get("duration_ms") or 0
        user_chars = entry.get("user_chars") or 0
        resp_chars = entry.get("response_chars") or 0
        prompt_tokens = entry.get("prompt_tokens")
        completion_tokens = entry.get("completion_tokens")

        # 每日请求量（仅 7d 窗口）
        if in_7d:
            day_key = ts.date().isoformat()
            daily_counts[day_key] = daily_counts.get(day_key, 0) + 1

            # 模型分布（仅 7d）
            model = entry.get("provider_model") or entry.get("client_model") or "unknown"
            model_dist[model] = model_dist.get(model, 0) + 1

        # 每小时请求量（仅 24h 窗口）
        # key 精确到"日期+小时"，避免跨天同一小时数值叠加（如昨日14时与今日14时被合并）
        if in_24h:
            local_ts = ts.astimezone()
            hour_key = local_ts.strftime("%Y-%m-%d %H")
            hourly_counts[hour_key] = hourly_counts.get(hour_key, 0) + 1

        windows = []
        if in_7d:
            windows.append("7d")
        if in_24h:
            windows.append("24h")
        if in_prev_24h:
            windows.append("prev_24h")

        for window in windows:
            if origin not in buckets[window]:
                buckets[window][origin] = _new_bucket()
            b = buckets[window][origin]
            b["count"] += 1
            if status == "ok":
                b["ok"] += 1
            elif status == "error":
                b["error"] += 1
            b["total_ms"] += duration_ms
            if duration_ms > 0:
                b["durations"].append(duration_ms)
            b["user_chars"] += user_chars
            b["resp_chars"] += resp_chars
            if prompt_tokens is not None:
                b["prompt_tokens"] += int(prompt_tokens)
                b["token_count"] += 1
            if completion_tokens is not None:
                b["completion_tokens"] += int(completion_tokens)

    # 计算 avg_ms / p95_ms，删除原始 durations 列表
    for window in buckets:
        for b in buckets[window].values():
            b["avg_ms"] = round(b["total_ms"] / b["count"]) if b["count"] else 0
            durations = sorted(b.pop("durations", []))
            b["p95_ms"] = _percentile(durations, 95)

    # 补全过去 days 天的每日数据（无记录的日期填 0）
    daily_series = []
    for i in range(days - 1, -1, -1):
        day = (now - timedelta(days=i)).date().isoformat()
        daily_series.append({"date": day, "count": daily_counts.get(day, 0)})

    # 补全过去 24 小时的每小时数据（本地时间，从最早到最新）
    local_now = now.astimezone()
    hourly_series = []
    for i in range(23, -1, -1):
        h = (local_now - timedelta(hours=i))
        hour_key = h.strftime("%Y-%m-%d %H")
        label = h.strftime("%H:00")
        hourly_series.append({"hour": label, "count": hourly_counts.get(hour_key, 0)})

    total_24h = sum(b["count"] for b in buckets["24h"].values())
    total_7d = sum(b["count"] for b in buckets["7d"].values())
    total_ms_7d = sum(b["total_ms"] for b in buckets["7d"].values())
    avg_ms_7d = round(total_ms_7d / total_7d) if total_7d else 0
    prev_total_24h = sum(b["count"] for b in buckets["prev_24h"].values())

    return {
        "24h": buckets["24h"],
        "7d": buckets["7d"],
        "prev_24h": buckets["prev_24h"],
        "daily_series": daily_series,
        "hourly_series": hourly_series,
        "model_dist": model_dist,
        "summary": {
            "total_24h": total_24h,
            "total_7d": total_7d,
            "avg_ms_7d": avg_ms_7d,
            "prev_total_24h": prev_total_24h,
        },
    }


__all__ = [
    "RequestHistoryRecorder",
    "configure",
    "get_cfg",
    "get_recorder",
    "shutdown",
    "record",
    "prune_now",
    "query_stats",
]
