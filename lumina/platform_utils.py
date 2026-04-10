"""
lumina/platform_utils.py — 跨平台工具函数：剪贴板读写、粘贴模拟。
"""
import sys

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"


def clipboard_get() -> str:
    """读取系统剪贴板内容。"""
    if IS_MACOS:
        import subprocess
        return subprocess.check_output(["pbpaste"], timeout=3, text=True)
    elif IS_WINDOWS:
        import subprocess
        result = subprocess.run(
            ["powershell", "-Command", "Get-Clipboard"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    return ""


def clipboard_set(text: str) -> None:
    """写入系统剪贴板。"""
    if IS_MACOS:
        import subprocess
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
    elif IS_WINDOWS:
        import subprocess
        # 用 here-string 避免特殊字符转义问题
        ps_script = f"Set-Clipboard -Value @\"\n{text}\n\"@"
        subprocess.run(
            ["powershell", "-Command", ps_script],
            check=True, timeout=5,
        )


def paste_to_foreground() -> None:
    """模拟粘贴快捷键到当前最前窗口（需要辅助功能权限）。"""
    if IS_MACOS:
        import subprocess
        script = 'tell application "System Events" to keystroke "v" using command down'
        subprocess.run(["osascript", "-e", script], check=False)
    elif IS_WINDOWS:
        import subprocess
        subprocess.run(
            [
                "powershell", "-Command",
                "Add-Type -AssemblyName System.Windows.Forms; "
                "[System.Windows.Forms.SendKeys]::SendWait('^v')",
            ],
            check=False, timeout=5,
        )
