"""
lumina/platform_utils.py — 跨平台工具函数：剪贴板读写、粘贴模拟。
"""
from lumina.platform_support.desktop import get_desktop_services

_desktop = get_desktop_services()


def clipboard_get() -> str:
    """读取系统剪贴板内容。"""
    return _desktop.clipboard_get()


def clipboard_set(text: str) -> None:
    """写入系统剪贴板。"""
    _desktop.clipboard_set(text)


def paste_to_foreground() -> None:
    """模拟粘贴快捷键到当前最前窗口（需要辅助功能权限）。"""
    _desktop.paste_to_foreground()


def open_url(url: str) -> bool:
    """用系统默认浏览器打开 URL。"""
    return _desktop.open_url(url)
