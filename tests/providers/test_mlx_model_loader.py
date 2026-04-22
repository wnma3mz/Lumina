from __future__ import annotations

import sys
import types

import pytest

from tests.providers.local_provider_test_helpers import _make_loader, mlx_loader_mod


def test_mlx_loader_vlm_config_detection_handles_missing_model_type():
    loader = _make_loader()
    assert loader._is_vlm_config({"vision_config": {}}) is True
    assert loader._is_vlm_config({"model_type": "qwen2_vl"}) is True
    assert loader._is_vlm_config({"model_type": None}) is False
    assert loader._is_vlm_config({}) is False


def test_mlx_loader_vlm_probe_logs_and_falls_back(monkeypatch, caplog):
    loader = _make_loader()
    fake_pkg = types.ModuleType("mlx_vlm")
    fake_utils = types.ModuleType("mlx_vlm.utils")

    def _boom(_target):
        raise RuntimeError("bad config")

    fake_utils.load_config = _boom
    fake_pkg.utils = fake_utils
    monkeypatch.setattr(mlx_loader_mod, "_MLX_VLM_AVAILABLE", True)

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "mlx_vlm", fake_pkg)
        mp.setitem(sys.modules, "mlx_vlm.utils", fake_utils)
        with caplog.at_level("INFO", logger="lumina"):
            assert loader._detect_vlm_target("broken-model") is False

    assert "VLM probe failed for broken-model" in caplog.text


def test_mlx_loader_offload_keyword_matching_is_stable():
    loader = _make_loader()
    keywords = loader._build_offload_keywords(
        offload_embedding=True,
        offload_vision=True,
        offload_audio=False,
    )

    assert "embed_tokens" in keywords
    assert "vision_tower" in keywords
    assert loader._should_eval_param("language_model.model.layers.0.mlp.gate.weight", keywords) is True
    assert loader._should_eval_param("model.layers.0.self_attn.q_proj.weight", keywords) is True
    assert loader._should_eval_param("model.embed_tokens.weight", keywords) is False
    assert loader._should_eval_param("model.vision_tower.blocks.0.weight", keywords) is False
    assert loader._should_eval_param("lm_head.weight", keywords) is True
