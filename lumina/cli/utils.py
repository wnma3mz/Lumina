"""
lumina/cli/utils.py — 启动共用工具函数

包含：日志配置、配置路径解析、config 模板同步、
持久化写入、PID 管理、系统通知、端口检测、就绪横幅等。
"""
import logging
import os
import shutil
from pathlib import Path

from lumina.config_runtime import (
    read_mutable_config_data,
    resolve_config_path,
    sync_runtime_config,
    write_config_atomic,
)
from lumina.platform_support.desktop import get_desktop_services

logger = logging.getLogger("lumina")

# 打包时注入的版本标记
_EDITION = os.environ.get("LUMINA_EDITION")

# PID 文件
_PID_FILE = Path.home() / ".lumina" / "lumina.pid"


# ── 日志 ──────────────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def uvicorn_log_config(level: str = "INFO") -> dict:
    """返回带时间戳的 uvicorn 日志配置。"""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(asctime)s  %(levelprefix)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "use_colors": None,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(asctime)s  %(levelprefix)s %(client_addr)s  "%(request_line)s"  %(status_code)s',
                "datefmt": "%Y-%m-%d %H:%M:%S",
                "use_colors": None,
            },
        },
        "handlers": {
            "default": {"class": "logging.StreamHandler", "formatter": "default", "stream": "ext://sys.stderr"},
            "access":  {"class": "logging.StreamHandler", "formatter": "access",  "stream": "ext://sys.stdout"},
        },
        "loggers": {
            "uvicorn":        {"handlers": ["default"], "level": level.upper(), "propagate": False},
            "uvicorn.error":  {"handlers": ["default"], "level": level.upper(), "propagate": False},
            "uvicorn.access": {"handlers": ["access"],  "level": "INFO",        "propagate": False},
        },
    }


# ── Config 工具 ───────────────────────────────────────────────────────────────


def sync_user_config(config_path: str | None = None) -> None:
    """
    启动时将项目模板 config.json 里新增的字段补入用户配置，
    用户已有的值一律不覆盖。
    """
    resolved = resolve_config_path(config_path)
    if not resolved:
        return
    target = Path(resolved)
    if not target.exists():
        return

    try:
        new_keys = sync_runtime_config(target)
        if not new_keys:
            return
        logger.info("Config sync: added %d new key(s): %s", len(new_keys), new_keys)
    except Exception as e:
        logger.warning("Config sync: failed to write user config: %s", e)


def sync_static() -> None:
    """
    启动时将 bundle/源码内的 static 文件夹同步到 ~/.lumina/static/。
    server.py 固定从 ~/.lumina/static/ serve，保证命令行和 .app 两种
    启动方式都使用同一份最新文件，无需手动 cp。
    """
    import sys as _sys
    # 确定 bundle 内的 source 目录
    if hasattr(_sys, "_MEIPASS"):
        src = Path(_sys._MEIPASS) / "lumina" / "api" / "static"
    else:
        src = Path(__file__).parent.parent / "api" / "static"

    if not src.is_dir():
        logger.warning("sync_static: source dir not found: %s", src)
        return

    dest = Path.home() / ".lumina" / "static"
    dest.mkdir(parents=True, exist_ok=True)

    updated = []
    for src_file in src.rglob("*"):
        if not src_file.is_file():
            continue
        rel_path = src_file.relative_to(src)
        dst_file = dest / rel_path
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        # 只在内容变化时覆盖（比较文件大小+mtime，避免无谓 IO）
        if dst_file.exists():
            ss, ds = src_file.stat(), dst_file.stat()
            if ss.st_size == ds.st_size and ss.st_mtime <= ds.st_mtime:
                continue
        shutil.copy2(str(src_file), str(dst_file))
        updated.append(str(rel_path))

    if updated:
        logger.info("sync_static: updated %s", updated)

# ── 持久化写入 ────────────────────────────────────────────────────────────────

def _read_or_init_config(config_path: str | None) -> dict:
    return read_mutable_config_data(config_path)


def _write_user_config(data: dict) -> None:
    write_config_atomic(data)


def persist_ptt_enabled(enabled: bool, config_path: str | None = None) -> None:
    data = _read_or_init_config(config_path)
    ptt_cfg = data.get("ptt")
    if not isinstance(ptt_cfg, dict):
        ptt_cfg = {}
    ptt_cfg["enabled"] = bool(enabled)
    data["ptt"] = ptt_cfg
    write_config_atomic(data, config_path)


def persist_host(host: str, config_path: str | None = None) -> None:
    data = _read_or_init_config(config_path)
    data["host"] = host
    write_config_atomic(data, config_path)


def persist_digest_enabled(enabled: bool, config_path: str | None = None) -> None:
    data = _read_or_init_config(config_path)
    digest_cfg = data.get("digest")
    if not isinstance(digest_cfg, dict):
        digest_cfg = {}
    digest_cfg["enabled"] = bool(enabled)
    data["digest"] = digest_cfg
    write_config_atomic(data, config_path)


def persist_menubar_enabled(enabled: bool, config_path: str | None = None) -> None:
    data = _read_or_init_config(config_path)
    desktop_cfg = data.get("desktop")
    if not isinstance(desktop_cfg, dict):
        desktop_cfg = {}
    desktop_cfg["menubar_enabled"] = bool(enabled)
    data["desktop"] = desktop_cfg
    write_config_atomic(data, config_path)


# ── PID 管理 ──────────────────────────────────────────────────────────────────

def write_pid():
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))


def read_pid() -> int | None:
    try:
        return int(_PID_FILE.read_text().strip())
    except Exception:
        return None


def remove_pid():
    _PID_FILE.unlink(missing_ok=True)


# ── 系统工具 ──────────────────────────────────────────────────────────────────

def notify(title: str, message: str):
    """发送系统通知。非打包开发模式也允许通知，便于跨平台一致性验证。"""
    get_desktop_services(enable_notifications=True).notify(title, message)


def is_port_in_use(host: str, port: int) -> bool:
    import socket
    check_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((check_host, port)) == 0


def get_lan_ip() -> str | None:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return None


def print_ready_banner(host: str, port: int):
    edition_label = {"full": "Full", "lite": "Lite"}.get(_EDITION, "Dev")
    print()
    print("=" * 55)
    print(f"  Lumina {edition_label} 已就绪")
    print(f"  本机访问：http://127.0.0.1:{port}")

    if host in ("0.0.0.0", ""):
        lan_ip = get_lan_ip()
        if lan_ip:
            print(f"  局域网访问：http://{lan_ip}:{port}")
            print("  手机扫码或在 Safari 打开上方地址")
            print("  添加到主屏幕即可像 App 一样使用")

    print("=" * 55)
    print()

    notify("Lumina 已就绪", f"服务运行于 http://127.0.0.1:{port}")


def is_digest_enabled() -> bool:
    from lumina.services.digest.config import get_cfg
    return bool(get_cfg().enabled)
