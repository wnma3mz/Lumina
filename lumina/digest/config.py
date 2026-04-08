"""
lumina/digest/config.py — DigestConfig 及全局配置单例
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class DigestConfig:
    """从 config.json["digest"] 读取，或使用默认值。"""
    scan_dirs: List[str] = field(default_factory=lambda: [
        str(Path.home() / "Documents"),
        str(Path.home() / "Desktop"),
        str(Path.home() / "Projects"),
        str(Path.home() / "code"),
        str(Path.home() / "dev"),
    ])
    history_hours: float = 24.0   # 采集窗口（小时）
    refresh_hours: float = 1.0    # 每隔多久检查一次增量


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
    _cfg = DigestConfig(
        scan_dirs=d.get("scan_dirs") or DigestConfig().scan_dirs,
        history_hours=float(d.get("history_hours", 24.0)),
        refresh_hours=float(d.get("refresh_hours", 1.0)),
    )
