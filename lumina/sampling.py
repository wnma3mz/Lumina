"""
共享采样默认值与 MLX sampler 构造。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional, Sequence

if TYPE_CHECKING:
    from lumina.config import SamplingConfig

DEFAULT_MAX_TOKENS = 512
DEFAULT_TEMPERATURE = 0.0
DEFAULT_TOP_P = 1.0
DEFAULT_TOP_K = 20
DEFAULT_MIN_P = 0.0
DEFAULT_PRESENCE_PENALTY = 2.0
DEFAULT_REPETITION_PENALTY = 1.0


def build_mlx_sampler(
    *,
    temperature: float,
    top_p: float,
    min_p: float,
    top_k: int,
    presence_penalty: float,
    repetition_penalty: float,
    token_ids: Sequence[int],
) -> Callable:
    """
    构造一个带采样过滤与惩罚项的 sampler。

    `mlx_lm.sample_utils.make_sampler()` 负责 temperature/top_p/min_p/top_k，
    presence/repetition penalty 通过 processor 在采样前叠加到 logprobs 上。
    """
    from mlx_lm.sample_utils import (
        make_presence_penalty,
        make_repetition_penalty,
        make_sampler,
    )

    base_sampler = make_sampler(
        temp=temperature,
        top_p=top_p,
        min_p=min_p,
        top_k=top_k,
    )
    processors = []
    if presence_penalty != 0.0:
        processors.append(make_presence_penalty(presence_penalty))
    if repetition_penalty != 1.0:
        processors.append(make_repetition_penalty(repetition_penalty))

    def sampler(logprobs):
        adjusted = logprobs
        if processors:
            context_tokens = list(token_ids)
            for processor in processors:
                adjusted = processor(context_tokens, adjusted)
        return base_sampler(adjusted)

    return sampler


def resolve_sampling(
    cfg_sampling: "SamplingConfig",
    *,
    temperature: Optional[float],
    top_p: Optional[float],
    top_k: Optional[int],
    min_p: Optional[float],
    presence_penalty: Optional[float],
    repetition_penalty: Optional[float],
    max_tokens: Optional[int],
) -> dict:
    """按优先级合并采样参数，返回所有字段均非 None 的完整 dict。

    优先级（高→低）：
      1. 调用方显式传入（非 None）
      2. config.json provider.sampling（非 None）
      3. sampling.py DEFAULT 常量
    """
    def _pick(api_val, cfg_val, default):
        if api_val is not None:
            return api_val
        if cfg_val is not None:
            return cfg_val
        return default

    return {
        "temperature": _pick(temperature, cfg_sampling.temperature, DEFAULT_TEMPERATURE),
        "top_p": _pick(top_p, cfg_sampling.top_p, DEFAULT_TOP_P),
        "top_k": _pick(top_k, cfg_sampling.top_k, DEFAULT_TOP_K),
        "min_p": _pick(min_p, cfg_sampling.min_p, DEFAULT_MIN_P),
        "presence_penalty": _pick(presence_penalty, cfg_sampling.presence_penalty, DEFAULT_PRESENCE_PENALTY),
        "repetition_penalty": _pick(repetition_penalty, cfg_sampling.repetition_penalty, DEFAULT_REPETITION_PENALTY),
        "max_tokens": _pick(max_tokens, cfg_sampling.max_tokens, DEFAULT_MAX_TOKENS),
    }
