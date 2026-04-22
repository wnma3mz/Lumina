"""
lumina/digest/config.py — DigestConfig 及全局配置单例
"""
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


def get_cfg() -> DigestConfig:
    return _cfg


@contextmanager
def override_history_hours(hours: float):
    """临时覆盖 history_hours（主线程调用，collector 线程启动前设置，完成后恢复）。"""
    old = _cfg.history_hours
    _cfg.history_hours = hours
    try:
        yield
    finally:
        _cfg.history_hours = old


def configure(data: dict) -> None:
    """从 config.json 的 digest 节点初始化配置。"""
    global _cfg
    d = data.get("digest", {})
    if isinstance(d, DigestConfig):
        _cfg = d
        return
    elif hasattr(d, "model_dump"):
        d = d.model_dump()
    elif hasattr(d, "__dict__"):
        d = d.__dict__
    
    # 用 key 存在性而非真值判断：scan_dirs=[] 是"不扫描任何目录"，不能回退到默认值
    _cfg = DigestConfig(
        scan_dirs=d["scan_dirs"] if "scan_dirs" in d else DigestConfig().scan_dirs,
        history_hours=float(d.get("history_hours", 24.0)),
        refresh_hours=float(d.get("refresh_hours", 1.0)),
        notify_time=str(d.get("notify_time", "20:00")),
        weekly_report_day=max(0, min(6, int(d.get("weekly_report_day", 0)))),
        monthly_report_day=max(1, min(28, int(d.get("monthly_report_day", 1)))),
        ai_queries_max_source_chars=max(1, int(d.get("ai_queries_max_source_chars", 4000))),
        enabled_collectors=d.get("enabled_collectors", None),
        enabled=bool(d.get("enabled", False)),
        active_watch_dirs=d["active_watch_dirs"] if "active_watch_dirs" in d else DigestConfig().active_watch_dirs,
        prompts=d.get("prompts", {}),
        sampling=d.get("sampling", {}),
    )


def set_enabled(enabled: bool) -> None:
    _cfg.enabled = bool(enabled)
