from __future__ import annotations

import base64
import io
import threading
from typing import Any, Optional

from lumina.providers.message_parts import to_vlm_messages_and_images

try:
    from mlx_vlm import generate as vlm_generate
    from mlx_vlm.generate import stream_generate as vlm_stream_generate
    from mlx_vlm.prompt_utils import apply_chat_template as vlm_apply_chat_template
    from mlx_vlm.utils import load_config as vlm_load_config

    _MLX_VLM_AVAILABLE = True
except ImportError:
    vlm_generate = None  # type: ignore[assignment]
    vlm_stream_generate = None  # type: ignore[assignment]
    vlm_apply_chat_template = None  # type: ignore[assignment]
    vlm_load_config = None  # type: ignore[assignment]
    _MLX_VLM_AVAILABLE = False


class LocalVlmAdapter:
    """管理 LocalProvider 的 VLM 配置与图片消息适配。"""

    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self._vlm_config = None
        self._lock = threading.Lock()

    @property
    def supports_image_input(self) -> bool:
        return _MLX_VLM_AVAILABLE and bool(getattr(self._provider._loader, "loaded_as_vlm", False))

    def bind_loaded_model(self) -> None:
        if not self.supports_image_input:
            self._vlm_config = None
            return
        load_target = self._provider._loader.last_load_target or self._provider._loader.resolve_target()
        self._vlm_config = vlm_load_config(load_target)

    def ensure_loaded(self) -> None:
        if self._vlm_config is not None:
            return
        if not _MLX_VLM_AVAILABLE:
            raise ImportError("mlx-vlm 未安装，无法使用本地视觉模型")
        with self._lock:
            if self._vlm_config is not None:
                return
            if not self._provider.is_ready:
                raise RuntimeError("LocalProvider not loaded. Call load() first.")
            if not self.supports_image_input:
                raise NotImplementedError("当前已加载的本地模型不支持图片输入")
            load_target = self._provider._loader.last_load_target or self._provider._loader.resolve_target()
            self._vlm_config = vlm_load_config(load_target)

    @property
    def model(self) -> Any:
        if not self.supports_image_input:
            return None
        return self._provider._model

    @property
    def processor(self) -> Any:
        if not self.supports_image_input:
            return None
        return self._provider._tokenizer

    @property
    def config(self) -> Any:
        return self._vlm_config

    @staticmethod
    def _decode_data_url_image(image_ref: str):
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("Pillow 未安装，无法解析图片输入") from exc

        try:
            _, payload = image_ref.split(",", 1)
        except ValueError as exc:
            raise ValueError("无效的 data URL 图片输入") from exc
        try:
            image_bytes = base64.b64decode(payload)
        except Exception as exc:
            raise ValueError("无法解码 data URL 图片内容") from exc
        with Image.open(io.BytesIO(image_bytes)) as img:
            return img.convert("RGB")

    @classmethod
    def normalize_image_input(cls, image_ref: str):
        image_ref = (image_ref or "").strip()
        if not image_ref:
            raise ValueError("图片输入为空")
        if image_ref.startswith("data:"):
            return cls._decode_data_url_image(image_ref)
        if image_ref.startswith("file://"):
            return image_ref[len("file://") :]
        return image_ref

    def build_messages_and_images(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str],
    ) -> tuple[list[dict[str, str]], list[Any]]:
        return to_vlm_messages_and_images(
            messages,
            system=system,
            normalize_image_input=self.normalize_image_input,
        )

    def prepare_prompt(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str],
    ) -> tuple[str, list[Any]]:
        self.ensure_loaded()
        vlm_messages, image_inputs = self.build_messages_and_images(messages, system)
        prompt = vlm_apply_chat_template(
            self.processor,
            self.config,
            vlm_messages,
            add_generation_prompt=True,
            enable_thinking=False,
            num_images=len(image_inputs),
        )
        return prompt, image_inputs

    def generate_text(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str],
        *,
        max_tokens: int,
        temperature: float,
        top_p: float,
        repetition_penalty: float,
    ) -> str:
        prompt, image_inputs = self.prepare_prompt(messages, system)
        result = vlm_generate(
            self.model,
            self.processor,
            prompt,
            image=image_inputs,
            verbose=False,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
        )
        return str(getattr(result, "text", result or "")).strip()

    def stream_responses(
        self,
        messages: list[dict[str, Any]],
        system: Optional[str],
        *,
        max_tokens: int,
        temperature: float,
        top_p: float,
        repetition_penalty: float,
    ):
        prompt, image_inputs = self.prepare_prompt(messages, system)
        return vlm_stream_generate(
            self.model,
            self.processor,
            prompt,
            image=image_inputs,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
        )
