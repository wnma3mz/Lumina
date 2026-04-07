"""
lumina/ptt.py — 全局热键 PTT 守护（长按录音，松开转写并粘贴）

被 lumina server 以后台线程调用，也可通过 scripts/ptt.py 独立运行。
"""
import json
import subprocess
import threading
import time
import urllib.request
from typing import Optional

from pynput import keyboard as kb


# ── HTTP 工具 ──────────────────────────────────────────────────────────────────

def _http(method: str, url: str, body: Optional[dict] = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _pbcopy(text: str):
    subprocess.run(["pbcopy"], input=text.encode(), check=True)


def _paste():
    """模拟 Cmd+V，粘贴到当前最前面的窗口。需要辅助功能权限。"""
    script = 'tell application "System Events" to keystroke "v" using command down'
    subprocess.run(["osascript", "-e", script], check=False)


# ── 热键解析 ───────────────────────────────────────────────────────────────────

def _parse_key(key_str: str):
    """
    将字符串解析为 pynput Key / KeyCode，或组合键格式字符串。
    单键：'f5' → Key.f5，'alt' → Key.alt，'r' → KeyCode.from_char('r')
    组合键：'ctrl+alt+r' → '<ctrl>+<alt>+r'（用于 GlobalHotKeys）
    """
    parts = [p.strip().lower() for p in key_str.split("+")]

    if len(parts) == 1:
        k = parts[0]
        if k.startswith("f") and k[1:].isdigit():
            return getattr(kb.Key, k, None) or kb.Key.f5
        _modifier_map = {
            "alt": kb.Key.alt, "option": kb.Key.alt,
            "alt_r": kb.Key.alt_r, "option_r": kb.Key.alt_r,
            "alt_l": kb.Key.alt_l, "option_l": kb.Key.alt_l,
            "ctrl": kb.Key.ctrl, "control": kb.Key.ctrl,
            "ctrl_r": kb.Key.ctrl_r, "ctrl_l": kb.Key.ctrl_l,
            "cmd": kb.Key.cmd, "command": kb.Key.cmd,
            "cmd_r": kb.Key.cmd_r, "cmd_l": kb.Key.cmd_l,
            "shift": kb.Key.shift,
            "caps": kb.Key.caps_lock, "caps_lock": kb.Key.caps_lock, "capslock": kb.Key.caps_lock,
        }
        if k in _modifier_map:
            return _modifier_map[k]
        return kb.KeyCode.from_char(k)

    mapped = []
    for p in parts:
        if p in ("ctrl", "control"):   mapped.append("<ctrl>")
        elif p in ("alt", "option"):   mapped.append("<alt>")
        elif p in ("cmd", "command"):  mapped.append("<cmd>")
        elif p == "shift":             mapped.append("<shift>")
        elif p.startswith("f") and p[1:].isdigit(): mapped.append(f"<{p}>")
        else:                          mapped.append(p)
    return "+".join(mapped)


# ── PTT 守护 ───────────────────────────────────────────────────────────────────

class PTTDaemon:
    """
    长按热键录音，松开自动转写并粘贴到当前窗口。

    Args:
        base_url:      Lumina 服务地址，如 http://127.0.0.1:31821
        hotkey_str:    热键字符串，如 'f5'、'alt'、'ctrl+alt+r'
        menubar_app:   rumps.App 实例（可选）；提供时录音状态会反映到菜单栏标题
        menubar_title: 菜单栏正常状态的标题（默认 "Lumina"）
    """

    def __init__(
        self,
        base_url: str,
        hotkey_str: str = "f5",
        menubar_app=None,
        menubar_title: str = "Lumina",
    ):
        self.base_url = base_url.rstrip("/")
        self.hotkey_str = hotkey_str
        self._menubar_app = menubar_app
        self._menubar_title = menubar_title
        self._session_id: Optional[str] = None
        self._lock = threading.Lock()

    # ── 菜单栏状态 ────────────────────────────────────────────────────────────

    def _set_menubar(self, title: str):
        if self._menubar_app is not None:
            try:
                self._menubar_app.title = title
            except Exception:
                pass

    # ── 状态机 ────────────────────────────────────────────────────────────────

    def _start(self):
        """按下热键：开始录音。"""
        if not self._check_server():
            print(f"✗ 服务未响应（{self.base_url}），请确认 lumina server 已启动", flush=True)
            return
        try:
            resp = _http("POST", f"{self.base_url}/v1/audio/record/start")
            with self._lock:
                self._session_id = resp["session_id"]
            self._set_menubar("● " + self._menubar_title)
            print("● 录音中… 松开停止", flush=True)
            # 超时保护：修饰键 release 可能被 macOS 吞掉，30s 后自动停止
            threading.Thread(target=self._watchdog, daemon=True).start()
        except Exception as e:
            print(f"✗ 开始录音失败：{e}", flush=True)

    def _watchdog(self, timeout: int = 30):
        """等待 timeout 秒，若仍在录音则强制停止。"""
        for _ in range(timeout * 10):
            time.sleep(0.1)
            with self._lock:
                if self._session_id is None:
                    return   # 已正常停止
        with self._lock:
            has_session = self._session_id is not None
        if has_session:
            print(f"⚠ 录音超过 {timeout}s，自动停止", flush=True)
            threading.Thread(target=self._stop, daemon=True).start()

    def _stop(self):
        """松开热键：停止录音、转写、粘贴。"""
        with self._lock:
            session_id = self._session_id
            self._session_id = None

        if not session_id:
            return

        self._set_menubar("◌ " + self._menubar_title)   # 转写中
        print("■ 停止录音，转写中…", flush=True)
        try:
            resp = _http(
                "POST",
                f"{self.base_url}/v1/audio/record/stop",
                body={"session_id": session_id},
                timeout=120,
            )
            print(f"  API 返回：{resp}", flush=True)
            text = resp.get("text", "").strip()
        except Exception as e:
            print(f"✗ 转写 API 调用失败：{e}", flush=True)
            self._set_menubar(self._menubar_title)
            return

        self._set_menubar(self._menubar_title)           # 恢复正常

        if not text:
            print("（未识别到语音）", flush=True)
            return

        print(f"✓ {text}", flush=True)
        _pbcopy(text)
        time.sleep(0.3)   # 等修饰键完全松开，避免 Cmd+V 被系统拦截
        _paste()
        print("✓ 已粘贴", flush=True)

    # ── 服务检查 ──────────────────────────────────────────────────────────────

    def _check_server(self) -> bool:
        try:
            r = _http("GET", f"{self.base_url}/health", timeout=2)
            return r.get("status") == "ok"
        except Exception:
            return False

    # ── 主循环 ────────────────────────────────────────────────────────────────

    def run(self):
        """阻塞运行，放在 daemon 线程里即可随主进程退出。"""
        parsed = _parse_key(self.hotkey_str)
        print(f"语音输入已启动  热键：{self.hotkey_str.upper()}（长按录音，松开自动转写并粘贴）", flush=True)

        if isinstance(parsed, str):
            print("  提示：组合键为 toggle 模式（按一次开始，再按停止）", flush=True)
            self._run_toggle(parsed)
        else:
            self._run_hold(parsed)

    def _run_hold(self, target_key):
        _received_event = threading.Event()

        def on_press(key):
            _received_event.set()
            if key == target_key:
                with self._lock:
                    already = self._session_id is not None
                if not already:
                    threading.Thread(target=self._start, daemon=True).start()

        def on_release(key):
            _received_event.set()
            if key == target_key:
                with self._lock:
                    has_session = self._session_id is not None
                if has_session:
                    threading.Thread(target=self._stop, daemon=True).start()

        def _check_permission():
            if not _received_event.wait(timeout=30):
                print(
                    "\n⚠️  PTT 未收到任何按键事件，可能缺少辅助功能权限。\n"
                    "   请前往：系统设置 → 隐私与安全性 → 辅助功能\n"
                    "   找到当前终端并打开开关，然后重启终端重新运行。\n",
                    flush=True,
                )

        threading.Thread(target=_check_permission, daemon=True).start()

        with kb.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()

    def _run_toggle(self, hotkey_str: str):
        def on_activate():
            with self._lock:
                has_session = self._session_id is not None
            if has_session:
                threading.Thread(target=self._stop, daemon=True).start()
            else:
                threading.Thread(target=self._start, daemon=True).start()

        with kb.GlobalHotKeys({hotkey_str: on_activate}) as h:
            h.join()
