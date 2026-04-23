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
                logger.warning("Request history write failed: %s", e)
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
            with path.open("rb") as src, gzip.open(archive_path, "ab") as dst:
                shutil.copyfileobj(src, dst)
            after_size = archive_path.stat().st_size if archive_path.exists() else before_size
            path.unlink(missing_ok=True)
            stats["compressed"] += 1
            stats["freed_bytes"] += max(0, source_size - max(0, after_size - before_size))
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


__all__ = [
    "RequestHistoryRecorder",
    "configure",
    "get_cfg",
    "get_recorder",
    "shutdown",
    "record",
    "prune_now",
]
