"""
lumina/engine/token_counter.py — ContextVar 侧信道，用于 provider → LLMEngine 传递 token 计数。

用法：
  Provider 在生成完成后调用 set_token_counts(prompt_tokens, completion_tokens)。
  LLMEngine 在 finally 块里调用 get_token_counts() 读取后写入请求历史。

由于 contextvars 天然随 asyncio Task 继承，不需要显式传参，也不影响 provider 接口签名。
"""
from contextvars import ContextVar
from typing import Optional

_token_counts: ContextVar[Optional[tuple[int, int]]] = ContextVar(
    "lumina_token_counts", default=None
)


def set_token_counts(prompt_tokens: int, completion_tokens: int) -> None:
    _token_counts.set((prompt_tokens, completion_tokens))


def get_token_counts() -> Optional[tuple[int, int]]:
    return _token_counts.get()


def reset_token_counts() -> None:
    _token_counts.set(None)
