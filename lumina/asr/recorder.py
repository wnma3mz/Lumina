"""
按键触发的录音模块。

用法：
    recorder = AudioRecorder()
    wav_bytes = await recorder.record_until_release(key="f4")

支持两种模式：
    - push-to-talk：按住录音，松开停止
    - toggle：第一次按下开始，第二次按下停止
"""
import asyncio
import io
import queue
import threading
import wave
from typing import Optional

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000  # Whisper 要求 16kHz
CHANNELS = 1
DTYPE = "int16"
CHUNK = 1024  # 每次读取帧数


class AudioRecorder:
    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self._recording = False
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()

    def _audio_callback(self, indata: np.ndarray, frames: int, time, status):
        if self._recording:
            with self._lock:
                self._frames.append(indata.copy())

    def start(self):
        self._frames = []
        self._recording = True
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=CHUNK,
            callback=self._audio_callback,
        )
        self._stream.start()

    def stop(self) -> bytes:
        """停止录音，返回 WAV bytes。"""
        self._recording = False
        self._stream.stop()
        self._stream.close()

        with self._lock:
            frames = self._frames[:]

        if not frames:
            return b""

        audio = np.concatenate(frames, axis=0)
        return _to_wav_bytes(audio, self.sample_rate)

    async def record_until_release(self, stop_event: asyncio.Event) -> bytes:
        """
        异步录音：等待 stop_event 被 set 后停止并返回 WAV bytes。
        通常由 HTTP 请求携带 stop 信号，或由全局热键触发。
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.start)
        await stop_event.wait()
        return await loop.run_in_executor(None, self.stop)

    async def record_seconds(self, duration: float) -> bytes:
        """固定时长录音（测试/简单场景用）。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.start)
        await asyncio.sleep(duration)
        return await loop.run_in_executor(None, self.stop)


def _to_wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # int16 = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()
