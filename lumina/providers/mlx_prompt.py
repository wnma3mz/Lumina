"""
lumina/providers/mlx_prompt.py — Prompt 构建与 tokenization

职责：chat_template 渲染（自动检测并禁用 thinking 模式）、
token 编码（返回 mx.array）、system prefix token 提取（供 SystemPromptCache 使用）。

无状态工具类，仅依赖 tokenizer。
LocalProvider 在 load() 后初始化：self._prompt_builder = MlxPromptBuilder(tokenizer)
"""
from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger("lumina")

_SYSTEM_PROMPT_SENTINEL = "<lumina_system_cache_user_probe_7a93d1e4>"


class MlxPromptBuilder:
    """Layer 1：chat_template 渲染、tokenize、system prefix token 提取。"""

    def __init__(self, tokenizer) -> None:
        self._tokenizer = tokenizer
        self._supports_enable_thinking: Optional[bool] = None

    # ── Prompt 渲染 ───────────────────────────────────────────────────────────

    def render(self, system: str, user_text: str) -> str:
        """渲染 chat_template，自动检测并禁用 thinking 模式。"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        kwargs = {
            "tokenize": False,
            "add_generation_prompt": True,
        }
        kwargs["enable_thinking"] = False

        return self._tokenizer.apply_chat_template(messages, **kwargs)

    def encode(self, system: str, user_text: str):
        """渲染后 tokenize，返回 mx.array。"""
        import mlx.core as mx
        return mx.array(self._tokenizer.encode(self.render(system, user_text)))

    # ── System prefix 提取 ────────────────────────────────────────────────────

    def derive_prefix_tokens(self, system_text: str) -> Optional[List[int]]:
        """提取 system prompt 对应的 prefix token ids，供 KV cache 复用。"""
        prompt_text = self.render(system_text, _SYSTEM_PROMPT_SENTINEL)
        sentinel_start = prompt_text.find(_SYSTEM_PROMPT_SENTINEL)
        if sentinel_start < 0 or prompt_text.find(_SYSTEM_PROMPT_SENTINEL, sentinel_start + 1) != -1:
            return None

        base_tokenizer = getattr(self._tokenizer, "_tokenizer", self._tokenizer)
        encoded = base_tokenizer(
            prompt_text,
            add_special_tokens=True,
            return_offsets_mapping=True,
        )
        input_ids = encoded["input_ids"]
        offsets = encoded["offset_mapping"]
        sentinel_end = sentinel_start + len(_SYSTEM_PROMPT_SENTINEL)

        sentinel_token_indices = []
        for idx, (start, end) in enumerate(offsets):
            if end <= sentinel_start or start >= sentinel_end:
                continue
            if start < sentinel_start or end > sentinel_end:
                return None
            sentinel_token_indices.append(idx)

        if not sentinel_token_indices:
            return None

        sentinel_tokens = base_tokenizer.encode(_SYSTEM_PROMPT_SENTINEL, add_special_tokens=False)
        span_tokens = input_ids[sentinel_token_indices[0] : sentinel_token_indices[-1] + 1]
        if span_tokens != sentinel_tokens:
            return None

        return input_ids[: sentinel_token_indices[0]]
