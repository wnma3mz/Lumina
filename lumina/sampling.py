"""
共享采样默认值与 MLX sampler 构造。
"""
from __future__ import annotations

from typing import Callable, Sequence

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

    # 调试阶段强制走贪心采样，忽略上传入的 temperature。
    base_sampler = make_sampler(
        temp=0.0,
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
