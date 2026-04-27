import asyncio
import logging
import time
import numpy as np
from typing import AsyncIterator

from lumina.services.audio.recorder import AudioRecorder, SAMPLE_RATE
from lumina.services.audio.transcriber import Transcriber
from lumina.engine.llm import LLMEngine

logger = logging.getLogger("lumina.services.audio")

class LiveTranslator:
    def __init__(self, llm: LLMEngine, transcriber: Transcriber, lang_in="auto", lang_out="zh"):
        self.llm = llm
        self.transcriber = transcriber
        self.lang_in = lang_in
        self.lang_out = lang_out
        self.recorder = AudioRecorder(device_index=AudioRecorder.find_loopback_device())
        self._running = False

    async def stream_translate(self) -> AsyncIterator[dict]:
        """流式转写并翻译系统音频。"""
        self._running = True
        self.recorder.start()
        logger.info("Live translation started...")

        try:
            while self._running:
                await asyncio.sleep(2.5) # 每 2.5s 处理一个切片
                audio_slice = self.recorder.get_buffered_audio()
                if audio_slice is None or len(audio_slice) < (SAMPLE_RATE * 0.1):
                    continue

                # 静音检测：计算 RMS 振幅
                rms = np.sqrt(np.mean(audio_slice.astype(np.float64)**2))
                if rms < 50: # 经验阈值，适合普通环境音
                    continue

                # 1. 转写 (使用 float32 数组)
                # 展平为 1D 数组，防止 (N, 1) 导致 whisper 报错
                float_audio = audio_slice.flatten().astype(np.float32) / 32768.0

                lang_param = None if self.lang_in == "auto" else self.lang_in
                raw_text = await self.transcriber.transcribe_audio(float_audio, language=lang_param)

                if not raw_text or not raw_text.strip():
                    continue

                # 2. LLM 实时润色/翻译
                prompt = f"请将以下实时转写的音频片段翻译或润色为流畅的{self.lang_out}。如果是中文则润色，如果是英文则翻译。只需返回结果，不要解释。内容：\n{raw_text}"
                translated = await self.llm.generate(prompt, task="live_translate", max_tokens=100)

                yield {
                    "raw": raw_text,
                    "translated": translated.strip(),
                    "ts": time.time()
                }
        finally:
            self._running = False
            if self.recorder:
                try:
                    self.recorder.stop()
                except Exception as e:
                    logger.debug("Stop recorder error: %s", e)
            logger.info("Live translation stopped.")

    def stop(self):
        self._running = False
