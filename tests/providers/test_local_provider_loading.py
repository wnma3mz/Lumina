from __future__ import annotations

import os

import lumina.providers.mlx.loader as mlx_loader_mod
import lumina.providers.mlx.vlm as local_vlm_mod

from lumina.providers.local import LocalProvider
from lumina.providers.mlx.loader import _DEFAULT_MODEL_REPO_ID
from tests.providers.local_provider_test_helpers import (
    FakeLoadedModel,
    FakeLoadedTokenizer,
    _fake_loader_load,
)


def test_load_binds_vlm_handles_to_loaded_model(monkeypatch):
    provider = LocalProvider(model_path="synthetic", enable_warmup=False)

    def _fake_vlm_loader(*, offload_embedding=True, offload_vision=True, offload_audio=True):
        provider._loader.loaded_as_vlm = True
        provider._loader.last_load_target = "synthetic-vlm"
        return FakeLoadedModel(), FakeLoadedTokenizer(), None, None

    monkeypatch.setattr(provider._loader, "load", _fake_vlm_loader)
    monkeypatch.setattr(local_vlm_mod, "vlm_load_config", lambda target: {"target": target})

    provider.load()

    assert provider._vlm.model is provider._model
    assert provider._vlm.processor is provider._tokenizer
    assert provider._vlm.config == {"target": "synthetic-vlm"}


def test_load_runs_warmup_by_default(monkeypatch):
    provider = LocalProvider(model_path="synthetic")
    warmup_calls = []

    monkeypatch.setattr(provider._loader, "load", _fake_loader_load)
    monkeypatch.setattr(provider, "_run_warmup", lambda: warmup_calls.append("warmup"))

    provider.load()

    assert warmup_calls == ["warmup"]


def test_load_skips_warmup_when_disabled(monkeypatch):
    provider = LocalProvider(model_path="synthetic", enable_warmup=False)
    warmup_calls = []

    monkeypatch.setattr(provider._loader, "load", _fake_loader_load)
    monkeypatch.setattr(provider, "_run_warmup", lambda: warmup_calls.append("warmup"))

    provider.load()

    assert warmup_calls == []


def test_load_keeps_provider_ready_when_warmup_fails(monkeypatch):
    provider = LocalProvider(model_path="synthetic")

    monkeypatch.setattr(provider._loader, "load", _fake_loader_load)
    monkeypatch.setattr(provider, "_run_warmup", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    provider.load()

    assert provider.is_ready


def test_load_falls_back_to_default_repo_when_default_local_dir_missing(monkeypatch, tmp_path):
    missing_default_dir = tmp_path / "models" / "qwen3.5-0.8b-4bit"
    provider = LocalProvider(model_path=str(missing_default_dir))
    load_calls = []

    monkeypatch.setattr(provider._loader, "_find_cached_repo_snapshot", lambda repo_id: None)
    monkeypatch.setattr(
        mlx_loader_mod,
        "mlx_load",
        lambda model_path: (load_calls.append(model_path) or (FakeLoadedModel(), FakeLoadedTokenizer())),
    )
    if getattr(mlx_loader_mod, "_MLX_VLM_AVAILABLE", False):
        monkeypatch.setattr(
            mlx_loader_mod,
            "vlm_load",
            lambda model_path, **kwargs: (load_calls.append(model_path) or (FakeLoadedModel(), FakeLoadedTokenizer())),
        )
    monkeypatch.setattr(provider._loader, "_detect_vlm_target", lambda load_target: False)
    monkeypatch.setattr(provider._loader, "_init_batch_engine", lambda model, tokenizer: (None, None))
    monkeypatch.setattr(provider, "_run_warmup", lambda: None)

    provider.load()

    assert load_calls == [_DEFAULT_MODEL_REPO_ID]


def test_load_uses_existing_local_model_dir(monkeypatch, tmp_path):
    local_model_dir = tmp_path / "models" / "qwen3.5-0.8b-4bit"
    local_model_dir.mkdir(parents=True)
    provider = LocalProvider(model_path=str(local_model_dir))
    load_calls = []

    monkeypatch.setattr(
        mlx_loader_mod,
        "mlx_load",
        lambda model_path: (load_calls.append(model_path) or (FakeLoadedModel(), FakeLoadedTokenizer())),
    )
    if getattr(mlx_loader_mod, "_MLX_VLM_AVAILABLE", False):
        monkeypatch.setattr(
            mlx_loader_mod,
            "vlm_load",
            lambda model_path, **kwargs: (load_calls.append(model_path) or (FakeLoadedModel(), FakeLoadedTokenizer())),
        )
    monkeypatch.setattr(provider._loader, "_detect_vlm_target", lambda load_target: False)
    monkeypatch.setattr(provider._loader, "_init_batch_engine", lambda model, tokenizer: (None, None))
    monkeypatch.setattr(provider, "_run_warmup", lambda: None)

    provider.load()

    assert load_calls == [str(local_model_dir)]


def test_resolve_load_target_uses_cached_snapshot_when_default_dir_missing(monkeypatch, tmp_path):
    missing_default_dir = tmp_path / "models" / "qwen3.5-0.8b-4bit"
    provider = LocalProvider(model_path=str(missing_default_dir))
    cached_snapshot = str(tmp_path / "cache" / "snapshots" / "abc")

    monkeypatch.setattr(provider._loader, "_find_cached_repo_snapshot", lambda repo_id: cached_snapshot)

    assert provider._loader.resolve_target() == cached_snapshot


def test_find_cached_repo_snapshot_prefers_latest(monkeypatch, tmp_path):
    provider = LocalProvider(model_path="synthetic")
    hub_dir = tmp_path / "hub"
    snapshots = hub_dir / "models--mlx-community--Qwen3.5-0.8B-4bit" / "snapshots"
    old_snapshot = snapshots / "old"
    new_snapshot = snapshots / "new"
    old_snapshot.mkdir(parents=True)
    new_snapshot.mkdir(parents=True)
    (old_snapshot / "model.safetensors").write_text("x")
    (new_snapshot / "model.safetensors").write_text("x")

    old_ts = 100
    new_ts = 200
    os.utime(old_snapshot, (old_ts, old_ts))
    os.utime(new_snapshot, (new_ts, new_ts))
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(hub_dir))

    assert provider._loader._find_cached_repo_snapshot(_DEFAULT_MODEL_REPO_ID) == str(new_snapshot)


def test_render_prompt_disables_thinking_when_tokenizer_supports_flag():
    from lumina.providers.mlx.prompt import MlxPromptBuilder

    provider = LocalProvider(model_path="synthetic")

    class FakeTokenizer:
        def apply_chat_template(
            self,
            messages,
            *,
            tokenize=False,
            add_generation_prompt=False,
            enable_thinking=True,
        ):
            assert tokenize is False
            assert add_generation_prompt is True
            assert enable_thinking is False
            return "prompt"

    provider._tokenizer = FakeTokenizer()
    provider._prompt_builder = MlxPromptBuilder(provider._tokenizer)

    prompt = provider._render_prompt_text("sys", "user")

    assert prompt == "prompt"


def test_render_prompt_falls_back_when_tokenizer_has_no_thinking_flag():
    from lumina.providers.mlx.prompt import MlxPromptBuilder

    provider = LocalProvider(model_path="synthetic")

    class FakeTokenizer:
        def apply_chat_template(
            self,
            messages,
            *,
            tokenize=False,
            add_generation_prompt=False,
        ):
            assert tokenize is False
            assert add_generation_prompt is True
            return "prompt"

    provider._tokenizer = FakeTokenizer()
    provider._prompt_builder = MlxPromptBuilder(provider._tokenizer)

    prompt = provider._render_prompt_text("sys", "user")

    assert prompt == "prompt"
