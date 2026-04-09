"""
语音转文本：基于 mlx-whisper。

Whisper 模型按需下载，首次使用时自动拉取。
支持的模型（从小到大）：
    tiny      ~40 MB
    base      ~74 MB
    small     ~244 MB

默认使用 mlx-community/whisper-tiny（最小，够用于普通语音输入）。
"""
import asyncio
import io
import os
from typing import Optional

import numpy as np

# mlx-whisper 的 transcribe 接口与 openai-whisper 兼容
import mlx_whisper

_DEFAULT_WHISPER_MODEL = os.environ.get(
    "LUMINA_WHISPER_MODEL",
    "mlx-community/whisper-tiny-mlx-4bit",
)


class Transcriber:
    def __init__(self, model: Optional[str] = None):
        self.model = model or _DEFAULT_WHISPER_MODEL
        self._lock = asyncio.Lock()

    def transcribe_sync(self, wav_bytes: bytes, language: Optional[str] = None) -> str:
        """同步转写，在 executor 内调用。"""
        audio = _wav_bytes_to_float32(wav_bytes)
        # initial_prompt 给模型提示标点和语言风格，显著提升中文识别质量
        initial_prompt = None
        if language == "zh":
            initial_prompt = "以下是普通话的语音识别结果，包含标准的中文标点符号。"
        elif language == "en":
            initial_prompt = "The following is a transcription in English."

        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.model,
            language=language,
            fp16=False,
            condition_on_previous_text=False,  # PTT 每次独立录音，不应带上次上下文
            initial_prompt=initial_prompt,
        )
        return result.get("text", "").strip()

    async def transcribe(self, wav_bytes: bytes, language: Optional[str] = None) -> str:
        """异步转写。"""
        if not wav_bytes:
            return ""
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(
                None, self.transcribe_sync, wav_bytes, language
            )


def _wav_bytes_to_float32(wav_bytes: bytes) -> np.ndarray:
    """将 WAV bytes 解码为 float32 numpy 数组（Whisper 要求）。"""
    import wave

    buf = io.BytesIO(wav_bytes)
    with wave.open(buf, "rb") as wf:
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
        sample_width = wf.getsampwidth()
        n_channels = wf.getnchannels()

    if sample_width == 2:
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        audio = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported sample width: {sample_width}")

    # 多声道转单声道
    if n_channels > 1:
        audio = audio.reshape(-1, n_channels).mean(axis=1)

    return audio
