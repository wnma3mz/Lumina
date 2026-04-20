"""
lumina/request_context.py — 请求来源上下文

用 contextvars 在线程内传播轻量请求元数据，避免把 HTTP 细节耦合进 LLMEngine。
"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_origin_var: ContextVar[str] = ContextVar("lumina_request_origin", default="unknown")
_stream_var: ContextVar[Optional[bool]] = ContextVar("lumina_request_stream", default=None)
_client_model_var: ContextVar[Optional[str]] = ContextVar(
    "lumina_request_client_model",
    default=None,
)
_request_id_var: ContextVar[Optional[str]] = ContextVar("lumina_request_id", default=None)


@contextmanager
def request_context(
    *,
    origin: str,
    stream: Optional[bool] = None,
    client_model: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Iterator[None]:
    tokens = [
        (_origin_var, _origin_var.set(origin)),
        (_stream_var, _stream_var.set(stream)),
        (_client_model_var, _client_model_var.set(client_model)),
        (_request_id_var, _request_id_var.set(request_id)),
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def get_request_context() -> dict:
    return {
        "origin": _origin_var.get(),
        "stream": _stream_var.get(),
        "client_model": _client_model_var.get(),
        "request_id": _request_id_var.get(),
    }

