from __future__ import annotations

import types

import pytest

from lumina.engine.llm import LLMEngine
from lumina.providers.base import BaseProvider
from lumina.providers.llama_cpp import LlamaCppProvider
from lumina.providers.openai import OpenAIProvider


class DummyProvider(BaseProvider):
    async def generate_stream(
        self,
        user_text: str,
        system: str | None,
        max_tokens: int,
        temperature: float = 0.7,
        top_p: float = 0.8,
        *,
        top_k: int = 20,
        min_p: float = 0.0,
        presence_penalty: float = 1.5,
        repetition_penalty: float = 1.0,
    ):
        yield user_text


@pytest.mark.anyio
async def test_base_provider_rejects_image_messages_without_capability():
    provider = DummyProvider()

    with pytest.raises(NotImplementedError, match="图片输入"):
        await provider.generate_messages(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/demo.png"}},
                    ],
                }
            ],
            system=None,
            max_tokens=32,
        )


def test_openai_provider_capabilities_support_image_messages():
    provider = OpenAIProvider("https://example.com/v1")
    assert provider.capabilities.supports_messages is True
    assert provider.capabilities.supports_image_input is True


def test_local_provider_capabilities_follow_vlm_availability(monkeypatch):
    import lumina.providers.local as local_mod

    provider = local_mod.LocalProvider.__new__(local_mod.LocalProvider)
    provider._loader = types.SimpleNamespace(loaded_as_vlm=False)

    monkeypatch.setattr(local_mod, "_MLX_VLM_AVAILABLE", False)
    assert provider.capabilities.supports_image_input is False

    monkeypatch.setattr(local_mod, "_MLX_VLM_AVAILABLE", True)
    assert provider.capabilities.supports_image_input is False

    provider._loader.loaded_as_vlm = True
    assert provider.capabilities.supports_image_input is True


@pytest.mark.anyio
async def test_llama_cpp_provider_forwards_sampling_parameters():
    captured = {}

    class FakeLlama:
        def create_chat_completion(self, **kwargs):
            captured.update(kwargs)
            yield {"choices": [{"delta": {"content": "ok"}}]}

    provider = LlamaCppProvider("/tmp/model.gguf")
    provider._llm = FakeLlama()

    tokens = []
    async for token in provider.generate_stream(
        user_text="hello",
        system="system",
        max_tokens=64,
        temperature=0.3,
        top_p=0.7,
        top_k=11,
        min_p=0.2,
        presence_penalty=0.4,
        repetition_penalty=1.1,
    ):
        tokens.append(token)

    assert "".join(tokens) == "ok"
    assert captured["messages"][0]["role"] == "system"
    assert captured["top_k"] == 11
    assert captured["min_p"] == 0.2
    assert captured["presence_penalty"] == 0.4
    assert captured["repeat_penalty"] == 1.1


def test_llm_engine_exposes_provider_capabilities():
    engine = LLMEngine(DummyProvider(), system_prompts={"chat": "hello"})
    assert engine.provider_capabilities.supports_messages is True
    assert engine.provider_capabilities.supports_image_input is False


def test_messages_to_history_text_omits_data_urls():
    history = LLMEngine._messages_to_history_text(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    {"type": "image_url", "image_url": {"url": "https://example.com/demo.png"}},
                ],
            }
        ]
    )

    assert "[image:data-url omitted]" in history
    assert "[image:https://example.com/demo.png]" in history
