from __future__ import annotations

import pytest

pytest.importorskip("mlx.core", reason="mlx not available on this platform")

import lumina.providers.local_vlm as local_vlm_mod

from lumina.providers.local import LocalProvider


@pytest.mark.anyio
async def test_generate_messages_uses_vlm_for_image_inputs(monkeypatch):
    provider = LocalProvider(model_path="synthetic", enable_warmup=False)
    provider._model = object()
    provider._tokenizer = object()
    provider._loader.loaded_as_vlm = True
    provider._vlm._vlm_config = {}

    monkeypatch.setattr(provider, "_ensure_vlm_loaded", lambda: None)

    captured = {}

    def _fake_template(processor, config, messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "vlm-prompt"

    class _FakeResult:
        text = "vision result"

    def _fake_generate(model, processor, prompt, image=None, verbose=False, **kwargs):
        captured["prompt"] = prompt
        captured["image"] = image
        captured["generate_kwargs"] = kwargs
        return _FakeResult()

    monkeypatch.setattr(local_vlm_mod, "vlm_apply_chat_template", _fake_template)
    monkeypatch.setattr(local_vlm_mod, "vlm_generate", _fake_generate)

    result = await provider.generate_messages(
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "请描述"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+yF9kAAAAASUVORK5CYII="}},
            ],
        }],
        system="system prompt",
        max_tokens=16,
    )

    assert result == "vision result"
    assert captured["messages"][0] == {"role": "system", "content": "system prompt"}
    assert captured["messages"][1]["content"] == "请描述"
    assert captured["kwargs"]["num_images"] == 1
    assert captured["prompt"] == "vlm-prompt"
    assert len(captured["image"]) == 1
    assert getattr(captured["image"][0], "size", None) == (1, 1)


def test_ensure_vlm_loaded_reuses_existing_model(monkeypatch):
    provider = LocalProvider(model_path="synthetic", enable_warmup=False)
    provider._model = object()
    provider._tokenizer = object()
    provider._loader.loaded_as_vlm = True
    provider._loader.last_load_target = "synthetic-vlm"

    def _should_not_reload(*args, **kwargs):
        raise AssertionError("should not call vlm_load")

    monkeypatch.setattr(local_vlm_mod, "vlm_load_config", lambda target: {"target": target})
    monkeypatch.setattr(local_vlm_mod, "vlm_generate", _should_not_reload)

    provider._ensure_vlm_loaded()

    assert provider._vlm.model is provider._model
    assert provider._vlm.processor is provider._tokenizer
    assert provider._vlm.config == {"target": "synthetic-vlm"}


@pytest.mark.anyio
async def test_generate_messages_rejects_images_for_text_only_model():
    provider = LocalProvider(model_path="synthetic", enable_warmup=False)
    provider._model = object()

    with pytest.raises(NotImplementedError, match="不支持图片输入"):
        await provider.generate_messages(
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "请描述"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/demo.png"}},
                ],
            }],
            system="system prompt",
            max_tokens=16,
        )


@pytest.mark.anyio
async def test_generate_messages_stream_uses_vlm_for_image_inputs(monkeypatch):
    provider = LocalProvider(model_path="synthetic", enable_warmup=False)
    provider._model = object()
    provider._tokenizer = object()
    provider._loader.loaded_as_vlm = True
    provider._vlm._vlm_config = {}

    monkeypatch.setattr(provider, "_ensure_vlm_loaded", lambda: None)
    monkeypatch.setattr(local_vlm_mod, "vlm_apply_chat_template", lambda processor, config, messages, **kwargs: "vlm-prompt")

    class _Chunk:
        def __init__(self, text):
            self.text = text

    monkeypatch.setattr(
        local_vlm_mod,
        "vlm_stream_generate",
        lambda model, processor, prompt, image=None, **kwargs: [_Chunk("图"), _Chunk("像")],
    )

    chunks = []
    async for token in provider.generate_messages_stream(
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "读图"},
                {"type": "image_url", "image_url": {"url": "https://example.com/demo.png"}},
            ],
        }],
        system="system prompt",
        max_tokens=16,
    ):
        chunks.append(token)

    assert chunks == ["图", "像"]
