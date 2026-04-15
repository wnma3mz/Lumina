"""
语音转文本：macOS 用 mlx-whisper，其他平台用 faster-whisper。

Whisper 模型按需下载，首次使用时自动拉取。
支持的模型（从小到大）：
    tiny      ~40 MB
    base      ~74 MB
    small     ~244 MB

默认：
  macOS   mlx-community/whisper-tiny-mlx-4bit（mlx-whisper）
  Windows tiny（faster-whisper，HuggingFace model name）
"""
import asyncio
import io
import os
import sys
from typing import Optional

import numpy as np

# ── 后端选择 ──────────────────────────────────────────────────────────────────

if sys.platform == "darwin":
    # mlx-whisper 的 transcribe 接口与 openai-whisper 兼容
    import mlx_whisper as _mlx_whisper  # type: ignore[import]
    _BACKEND = "mlx"
    _DEFAULT_WHISPER_MODEL = os.environ.get(
        "LUMINA_WHISPER_MODEL",
        "mlx-community/whisper-tiny-mlx-4bit",
    )
else:
    # faster-whisper 跨平台（CUDA GPU + CPU fallback）
    _BACKEND = "faster_whisper"
    _DEFAULT_WHISPER_MODEL = os.environ.get(
        "LUMINA_WHISPER_MODEL",
        "tiny",
    )

# faster-whisper 模块级缓存，避免每次转写重新加载
_fw_model_cache: dict = {}

# 运行时由 set_asr_prompts() 注入（由 cli/server.py / 配置热更新调用）
_asr_prompt_zh: Optional[str] = None
_asr_prompt_en: Optional[str] = None


def set_asr_prompts(zh: str, en: str) -> None:
    """从 config.system_prompts 注入自定义 ASR initial prompt。"""
    global _asr_prompt_zh, _asr_prompt_en
    _asr_prompt_zh = zh or None
    _asr_prompt_en = en or None


def _make_initial_prompt(language: Optional[str]) -> Optional[str]:
    if language == "zh":
        return _asr_prompt_zh
    if language == "en":
        return _asr_prompt_en
    return None


def _get_faster_whisper_model(model_id: str):
    if model_id not in _fw_model_cache:
        from faster_whisper import WhisperModel  # type: ignore[import]
        try:
            import torch  # type: ignore[import]
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
        compute = "float16" if device == "cuda" else "int8"
        _fw_model_cache[model_id] = WhisperModel(model_id, device=device, compute_type=compute)
    return _fw_model_cache[model_id]


class Transcriber:
    def __init__(self, model: Optional[str] = None):
        self.model = model or _DEFAULT_WHISPER_MODEL
        self._lock = asyncio.Lock()

    def transcribe_sync(self, wav_bytes: bytes, language: Optional[str] = None) -> str:
        """同步转写，在 executor 内调用。"""
        audio = _wav_bytes_to_float32(wav_bytes)
        initial_prompt = _make_initial_prompt(language)

        if _BACKEND == "mlx":
            result = _mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self.model,
                language=language,
                fp16=False,
                condition_on_previous_text=False,  # PTT 每次独立录音，不应带上次上下文
                initial_prompt=initial_prompt,
            )
            return result.get("text", "").strip()

        # faster_whisper
        fw_model = _get_faster_whisper_model(self.model)
        segments, _ = fw_model.transcribe(
            audio,
            language=language,
            initial_prompt=initial_prompt,
            condition_on_previous_text=False,
        )
        return "".join(s.text for s in segments).strip()

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
