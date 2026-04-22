"""
lumina/digest/config.py — DigestConfig 及全局配置单例
"""
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, ConfigDict


class DigestConfig(BaseModel):
    """从 config.json["digest"] 读取，或使用默认值。"""
    model_config = ConfigDict(extra="ignore")

    scan_dirs: List[str] = Field(default_factory=lambda: [
        d for d in [
            str(Path.home() / "Documents"),
            str(Path.home() / "Desktop"),
            str(Path.home() / "Projects"),
            str(Path.home() / "projects"),
            str(Path.home() / "code"),
            str(Path.home() / "dev"),
            str(Path.home() / "src"),
            str(Path.home() / "work"),
            str(Path.home() / "workspace"),
            str(Path.home() / "repos"),
            str(Path.home() / "notes"),
            str(Path.home() / "Notes"),
        ] if Path(d).exists()
    ])
    history_hours: float = 24.0   # 采集窗口（小时）
    refresh_hours: float = 1.0    # 每隔多久检查一次增量
    notify_time: str = "20:00"    # 每日通知时间，格式 "HH:MM"，空字符串表示禁用
    weekly_report_day: int = 0    # 周报触发的星期几（0=周一 … 6=周日，ISO weekday()）
    monthly_report_day: int = 1   # 月报触发的日期（1-28，每月第几日）
    ai_queries_max_source_chars: int = 4000
    enabled_collectors: Optional[List[str]] = None  # None = 全部启用
    enabled: bool = False  # False = 完全关闭 digest（不自动也不手动生成）
    active_watch_dirs: List[str] = Field(default_factory=lambda: [
        str(Path.home() / "Downloads"),
        str(Path.home() / "Desktop"),
        str(Path.home() / "Documents"),
    ])
    prompts: Dict[str, str] = Field(default_factory=dict)
    sampling: Dict[str, Any] = Field(default_factory=dict)


_cfg: DigestConfig = DigestConfig()
_thread_local = threading.local()


def _get_main_digest_cfg() -> Optional[DigestConfig]:
    try:
        from lumina.config import peek_config

        cfg = peek_config()
        if cfg is not None and hasattr(cfg, "digest"):
            return cfg.digest
    except Exception:
        return None
    return None


def get_cfg() -> DigestConfig:
    base = _get_main_digest_cfg() or _cfg
    override = getattr(_thread_local, "history_hours_override", None)
    if override is None:
        return base
    return base.model_copy(update={"history_hours": float(override)})


@contextmanager
def override_history_hours(hours: float):
    """在线程内临时覆盖 history_hours，避免与热重载互相覆盖。"""
    had_old = hasattr(_thread_local, "history_hours_override")
    old = getattr(_thread_local, "history_hours_override", None)
    _thread_local.history_hours_override = float(hours)
    try:
        yield
    finally:
        if had_old:
            _thread_local.history_hours_override = old
        else:
            delattr(_thread_local, "history_hours_override")


def configure(data: dict) -> None:
    """从 config.json 的 digest 节点初始化配置。"""
    global _cfg
    if isinstance(data, DigestConfig):
        d = data
    elif hasattr(data, "model_dump") and isinstance(data, DigestConfig):
        d = data
    elif isinstance(data, dict):
        d = data.get("digest", {})
    else:
        d = {}

    if isinstance(d, DigestConfig):
        _cfg = d
        main_cfg = _get_main_digest_cfg()
        if main_cfg is not None:
            from lumina.config import peek_config

            cfg = peek_config()
            if cfg is not None:
                cfg.digest = d
        return
    elif hasattr(d, "model_dump"):
        d = d.model_dump()
    elif hasattr(d, "__dict__"):
        d = d.__dict__

    if not isinstance(d, dict):
        d = {}

    normalized = dict(d)
    if "weekly_report_day" in normalized:
        normalized["weekly_report_day"] = max(0, min(6, int(normalized["weekly_report_day"])))
    if "monthly_report_day" in normalized:
        normalized["monthly_report_day"] = max(1, min(28, int(normalized["monthly_report_day"])))
    if "ai_queries_max_source_chars" in normalized:
        normalized["ai_queries_max_source_chars"] = max(1, int(normalized["ai_queries_max_source_chars"]))

    new_cfg = DigestConfig.model_validate(normalized)
    _cfg = new_cfg
    try:
        from lumina.config import peek_config

        cfg = peek_config()
        if cfg is not None:
            cfg.digest = new_cfg
    except Exception:
        pass


def set_enabled(enabled: bool) -> None:
    target = _get_main_digest_cfg() or _cfg
    target.enabled = bool(enabled)
