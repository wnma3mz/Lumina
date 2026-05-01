"""
OpenAIProvider SSE 解析测试。

不发真实 HTTP 请求，使用 mock 替换 aiohttp.ClientSession。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_sse_bytes(events: list[dict | str]) -> list[bytes]:
    """把事件列表转成 SSE bytes 分片列表（每个元素模拟一次 iter_any() 返回）。"""
    lines = []
    for ev in events:
        if ev == "[DONE]":
            lines.append(b"data: [DONE]\n\n")
        else:
            lines.append(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
    return lines


def _delta_event(content: str) -> dict:
    return {"choices": [{"delta": {"content": content}, "finish_reason": None}]}


def _finish_event() -> dict:
    return {"choices": [{"delta": {}, "finish_reason": "stop"}]}


async def _mock_stream(chunks: list[bytes]):
    """模拟 resp.content.iter_any() 的异步生成器。"""
    for chunk in chunks:
        yield chunk


def _make_mock_session(chunks: list[bytes]):
    """构造 aiohttp.ClientSession mock。"""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content.iter_any = lambda: _mock_stream(chunks)

    mock_cm_resp = MagicMock()
    mock_cm_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.close = AsyncMock()
    mock_session.post = MagicMock(return_value=mock_cm_resp)

    return mock_session


async def _collect(provider, text: str) -> list[str]:
    tokens = []
    async for tok in provider.generate_stream(text, system=None, max_tokens=100, temperature=0.0):
        tokens.append(tok)
    return tokens


# ── 正常路径 ─────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_sse_basic_two_tokens():
    """两个 token，各自一行，正常返回。"""
    from lumina.providers.openai import OpenAIProvider

    chunks = _make_sse_bytes([
        _delta_event("Hello"),
        _delta_event(" world"),
        "[DONE]",
    ])
    provider = OpenAIProvider(base_url="http://fake")
    with patch("aiohttp.ClientSession", return_value=_make_mock_session(chunks)):
        tokens = await _collect(provider, "hi")
    assert tokens == ["Hello", " world"]


@pytest.mark.anyio
async def test_client_session_reused_across_requests():
    """同一个 provider 多次请求复用同一个 ClientSession。"""
    from lumina.providers.openai import OpenAIProvider

    chunks = _make_sse_bytes([_delta_event("ok"), "[DONE]"])
    session = _make_mock_session(chunks)
    provider = OpenAIProvider(base_url="http://fake")
    with patch("aiohttp.ClientSession", return_value=session) as session_cls:
        assert await _collect(provider, "one") == ["ok"]
        assert await _collect(provider, "two") == ["ok"]

    assert session_cls.call_count == 1
    assert session.post.call_count == 2


@pytest.mark.anyio
async def test_client_session_close_closes_cached_session():
    """Provider close 会关闭已创建的 ClientSession。"""
    from lumina.providers.openai import OpenAIProvider

    chunks = _make_sse_bytes([_delta_event("ok"), "[DONE]"])
    session = _make_mock_session(chunks)
    provider = OpenAIProvider(base_url="http://fake")
    with patch("aiohttp.ClientSession", return_value=session):
        assert await _collect(provider, "hi") == ["ok"]
        await provider.close()

    session.close.assert_awaited_once()
    assert provider._session is None


# ── Fix #1：SSE 跨 chunk 解析 ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_sse_json_split_across_chunks():
    """Fix #1：单个 JSON 对象被 HTTP 层切成两个 chunk，
    不应解析失败或丢 token——line-buffer 逻辑必须等到完整行才解析。"""
    from lumina.providers.openai import OpenAIProvider

    # 构造完整 SSE 行，然后在 JSON 中间截断
    full_line = b'data: ' + json.dumps(_delta_event("split")).encode() + b'\n\n'
    mid = len(full_line) // 2
    chunks = [full_line[:mid], full_line[mid:], b"data: [DONE]\n\n"]

    provider = OpenAIProvider(base_url="http://fake")
    with patch("aiohttp.ClientSession", return_value=_make_mock_session(chunks)):
        tokens = await _collect(provider, "hi")
    assert tokens == ["split"]


@pytest.mark.anyio
async def test_sse_multiple_events_in_one_chunk():
    """多个事件合并在一个 chunk 里（服务端批量 flush），全部应被解析。"""
    from lumina.providers.openai import OpenAIProvider

    combined = (
        b"data: " + json.dumps(_delta_event("A")).encode() + b"\n\n"
        + b"data: " + json.dumps(_delta_event("B")).encode() + b"\n\n"
        + b"data: [DONE]\n\n"
    )
    chunks = [combined]

    provider = OpenAIProvider(base_url="http://fake")
    with patch("aiohttp.ClientSession", return_value=_make_mock_session(chunks)):
        tokens = await _collect(provider, "hi")
    assert tokens == ["A", "B"]


@pytest.mark.anyio
async def test_sse_crlf_events_are_parsed():
    """兼容使用 CRLF 分隔的标准 SSE 响应。"""
    from lumina.providers.openai import OpenAIProvider

    chunks = [
        b"data: " + json.dumps(_delta_event("crlf")).encode() + b"\r\n\r\n",
        b"data: [DONE]\r\n\r\n",
    ]

    provider = OpenAIProvider(base_url="http://fake")
    with patch("aiohttp.ClientSession", return_value=_make_mock_session(chunks)):
        tokens = await _collect(provider, "hi")
    assert tokens == ["crlf"]


@pytest.mark.anyio
async def test_sse_utf8_character_split_across_chunks():
    """UTF-8 字符跨 chunk 时不应被 replacement char 破坏。"""
    from lumina.providers.openai import OpenAIProvider

    line = b"data: " + json.dumps(_delta_event("你好"), ensure_ascii=False).encode() + b"\n\n"
    split_at = line.index("你".encode()[:1]) + 1
    chunks = [line[:split_at], line[split_at:], b"data: [DONE]\n\n"]

    provider = OpenAIProvider(base_url="http://fake")
    with patch("aiohttp.ClientSession", return_value=_make_mock_session(chunks)):
        tokens = await _collect(provider, "hi")
    assert tokens == ["你好"]


@pytest.mark.anyio
async def test_sse_multiline_data_event_parser():
    """SSE 同一事件内多个 data 行按规范用换行拼接。"""
    from lumina.providers.openai import OpenAIProvider

    chunks = [b"data: hello\r\ndata: world\r\n\r\n"]
    provider = OpenAIProvider(base_url="http://fake")
    session = _make_mock_session(chunks)
    resp_cm = session.post.return_value
    resp = await resp_cm.__aenter__()

    items = [item async for item in provider._iter_sse_data(resp)]
    assert items == ["hello\nworld"]


@pytest.mark.anyio
async def test_sse_empty_delta_skipped():
    """finish_reason 事件的 delta.content 为空，不应 yield 空字符串。"""
    from lumina.providers.openai import OpenAIProvider

    chunks = _make_sse_bytes([
        _delta_event("last token"),
        _finish_event(),
        "[DONE]",
    ])

    provider = OpenAIProvider(base_url="http://fake")
    with patch("aiohttp.ClientSession", return_value=_make_mock_session(chunks)):
        tokens = await _collect(provider, "hi")
    assert tokens == ["last token"]


@pytest.mark.anyio
async def test_sse_malformed_json_line_skipped():
    """格式错误的行不应导致异常，直接跳过。"""
    from lumina.providers.openai import OpenAIProvider

    chunks = [
        b"data: {broken json}\n\n",
        b"data: " + json.dumps(_delta_event("ok")).encode() + b"\n\n",
        b"data: [DONE]\n\n",
    ]

    provider = OpenAIProvider(base_url="http://fake")
    with patch("aiohttp.ClientSession", return_value=_make_mock_session(chunks)):
        tokens = await _collect(provider, "hi")
    assert tokens == ["ok"]
