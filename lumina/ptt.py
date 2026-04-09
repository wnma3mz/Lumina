"""
lumina/ptt.py — 全局热键 PTT 守护（toggle 模式）

按一次热键开始录音，再按一次停止，自动转写并粘贴到当前窗口。
随 lumina server 以后台线程启动。
"""
import io
import json
import subprocess
import threading
import time
import urllib.request
import wave
from typing import Optional

import numpy as np
import sounddevice as sd
from pynput import keyboard as kb

SAMPLE_RATE = 16000


# ── 系统剪贴板 / 粘贴 ──────────────────────────────────────────────────────────

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
    单键：'f5' → Key.f5，'caps_lock' → Key.caps_lock，'r' → KeyCode.from_char('r')
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
        if p in ("ctrl", "control"):    mapped.append("<ctrl>")
        elif p in ("alt", "option"):    mapped.append("<alt>")
        elif p in ("cmd", "command"):   mapped.append("<cmd>")
        elif p == "shift":              mapped.append("<shift>")
        elif p.startswith("f") and p[1:].isdigit(): mapped.append(f"<{p}>")
        else:                           mapped.append(p)
    return "+".join(mapped)


# ── PTT 守护 ───────────────────────────────────────────────────────────────────

class PTTDaemon:
    """
    Toggle 热键录音：按一次开始，再按一次停止，自动转写并粘贴。

    Args:
        base_url:      Lumina 服务地址，如 http://127.0.0.1:31821
        hotkey_str:    热键字符串，如 'f5'、'caps_lock'、'ctrl+alt+r'
        language:      语言代码（'zh'、'en'），None 时 Whisper 自动检测
        menubar_app:   rumps.App 实例（可选），录音时更新菜单栏标题
        menubar_title: 菜单栏正常状态标题（默认 "Lumina"）
    """

    def __init__(
        self,
        base_url: str,
        hotkey_str: str = "f5",
        language: Optional[str] = "zh",
        menubar_app=None,
        menubar_title: str = "Lumina",
    ):
        self.base_url = base_url.rstrip("/")
        self.hotkey_str = hotkey_str
        self._language = language
        self._menubar_app = menubar_app
        self._menubar_title = menubar_title

        self._recording = False
        self._frames: list = []
        self._stream = None
        self._lock = threading.Lock()

    # ── 菜单栏状态 ────────────────────────────────────────────────────────────

    def _set_menubar(self, title: str):
        if self._menubar_app is not None:
            try:
                self._menubar_app.title = title
            except Exception:
                pass

    # ── 录音 ──────────────────────────────────────────────────────────────────

    def _start(self):
        """开始本地录音。"""
        with self._lock:
            if self._recording:
                return
            self._recording = True
            self._frames = []

        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                callback=self._audio_callback,
            )
            stream.start()
            with self._lock:
                self._stream = stream
        except Exception as e:
            print(f"✗ 开始录音失败：{e}", flush=True)
            with self._lock:
                self._recording = False
            return

        self._set_menubar("● " + self._menubar_title)
        print("● 录音中… 再按停止", flush=True)
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _audio_callback(self, indata, frames, time_info, status):
        with self._lock:
            if self._recording:
                self._frames.append(indata.copy())

    def _stop(self):
        """停止录音，转写，粘贴。"""
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            stream = self._stream
            self._stream = None
            frames = self._frames[:]

        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

        self._set_menubar("◌ " + self._menubar_title)
        print("■ 转写中…", flush=True)

        if not frames:
            print("（未录到音频）", flush=True)
            self._set_menubar(self._menubar_title)
            return

        wav_bytes = self._frames_to_wav(frames)
        threading.Thread(target=self._transcribe_and_paste, args=(wav_bytes,), daemon=True).start()

    def _watchdog(self, timeout: int = 30):
        """超时保护：录音超过 timeout 秒自动停止。"""
        for _ in range(timeout * 10):
            time.sleep(0.1)
            with self._lock:
                if not self._recording:
                    return
        print(f"⚠ 录音超过 {timeout}s，自动停止", flush=True)
        threading.Thread(target=self._stop, daemon=True).start()

    # ── 转写 ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _frames_to_wav(frames: list) -> bytes:
        audio = np.concatenate(frames, axis=0)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)   # int16 = 2 bytes
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        return buf.getvalue()

    def _transcribe_and_paste(self, wav_bytes: bytes):
        try:
            text = self._call_transcriptions(wav_bytes)
        except Exception as e:
            print(f"✗ 转写失败：{e}", flush=True)
            self._set_menubar(self._menubar_title)
            return

        self._set_menubar(self._menubar_title)
        if not text:
            print("（未识别到语音）", flush=True)
            return

        print(f"✓ {text}", flush=True)
        _pbcopy(text)
        time.sleep(0.15)   # 等修饰键完全松开，避免 Cmd+V 被系统拦截
        _paste()
        print("✓ 已粘贴", flush=True)

    def _call_transcriptions(self, wav_bytes: bytes) -> str:
        """multipart/form-data POST 到 /v1/audio/transcriptions。"""
        boundary = "LuminaPTTBoundary"
        parts = []

        if self._language:
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="language"\r\n\r\n'
                f"{self._language}\r\n"
            )

        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        )
        body = "".join(parts).encode() + wav_bytes + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            f"{self.base_url}/v1/audio/transcriptions",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        # 显式绕过系统代理，避免 http_proxy 环境变量把本地请求转发出去
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=120) as resp:
            result = json.loads(resp.read())
        return result.get("text", "").strip()

    # ── 主循环 ────────────────────────────────────────────────────────────────

    def run(self):
        """阻塞运行，放在 daemon 线程里即可随主进程退出。"""
        parsed = _parse_key(self.hotkey_str)
        print(f"PTT 已启动  热键：{self.hotkey_str.upper()}（按一次开始录音，再按一次停止）", flush=True)

        if isinstance(parsed, str):
            # 组合键：使用 GlobalHotKeys
            self._run_toggle_combo(parsed)
        else:
            # 单键：使用 Listener，on_press 触发 toggle
            self._run_toggle_single(parsed)

    def _run_toggle_single(self, target_key):
        """单键 toggle：on_press 切换状态，含消抖和权限检查。"""
        _received_event = threading.Event()
        _last_press_time = [0.0]

        def on_press(key):
            _received_event.set()
            if key != target_key:
                return
            now = time.time()
            if now - _last_press_time[0] < 0.3:   # 消抖，过滤 auto-repeat
                return
            _last_press_time[0] = now
            with self._lock:
                recording = self._recording
            if recording:
                threading.Thread(target=self._stop, daemon=True).start()
            else:
                threading.Thread(target=self._start, daemon=True).start()

        def _permission_check():
            if not _received_event.wait(timeout=30):
                print(
                    "\n⚠️  PTT 未收到任何按键事件，可能缺少辅助功能权限。\n"
                    "   请前往：系统设置 → 隐私与安全性 → 辅助功能\n"
                    "   找到当前终端并打开开关，然后重启终端重新运行。\n",
                    flush=True,
                )

        threading.Thread(target=_permission_check, daemon=True).start()

        with kb.Listener(on_press=on_press) as listener:
            listener.join()

    def _run_toggle_combo(self, hotkey_str: str):
        """组合键 toggle：使用 GlobalHotKeys。"""
        def on_activate():
            with self._lock:
                recording = self._recording
            if recording:
                threading.Thread(target=self._stop, daemon=True).start()
            else:
                threading.Thread(target=self._start, daemon=True).start()

        with kb.GlobalHotKeys({hotkey_str: on_activate}) as h:
            h.join()
