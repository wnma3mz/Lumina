from lumina.config import Config
from lumina.config_runtime import (
    _build_runtime_candidate,
    patch_requires_restart,
    serialize_runtime_config,
)
from tests.config_helpers import minimal_config, write_config


def test_patch_requires_restart_for_host_change(tmp_path):
    old_cfg = Config.load(write_config(tmp_path, minimal_config()))
    new_data = minimal_config()
    new_data["host"] = "0.0.0.0"
    new_cfg = Config.load(write_config(tmp_path, new_data))

    assert patch_requires_restart(
        old_cfg,
        new_cfg,
        {"system": {"server": {"host": "0.0.0.0"}}},
    ) is True


def test_patch_requires_restart_for_mlx_offload_change(tmp_path):
    old_cfg = Config.load(write_config(tmp_path, minimal_config()))
    new_data = minimal_config()
    new_data["provider"]["offload_embedding"] = False
    new_cfg = Config.load(write_config(tmp_path, new_data))

    assert patch_requires_restart(
        old_cfg,
        new_cfg,
        {"provider": {"offload_embedding": False}},
    ) is True


def test_patch_requires_restart_for_mlx_memory_patch(tmp_path):
    old_cfg = Config.load(write_config(tmp_path, minimal_config()))
    new_cfg = Config.load(write_config(tmp_path, minimal_config()))

    assert patch_requires_restart(
        old_cfg,
        new_cfg,
        {"provider": {"mlx_memory": {"offload_audio": False}}},
    ) is True


def test_patch_requires_restart_false_for_openai_subconfig_on_local_backend(tmp_path):
    old_cfg = Config.load(write_config(tmp_path, minimal_config()))
    new_data = minimal_config()
    new_data["provider"]["openai"] = {
        "base_url": "http://remote/v1",
        "api_key": "k",
        "model": "m",
    }
    new_cfg = Config.load(write_config(tmp_path, new_data))

    assert patch_requires_restart(
        old_cfg,
        new_cfg,
        {"provider": {"openai": {"base_url": "http://remote/v1"}}},
    ) is False


def test_patch_requires_restart_false_for_openai_subconfig_on_openai_backend(tmp_path):
    old_data = minimal_config()
    old_data["provider"]["type"] = "openai"
    old_data["provider"]["openai"] = {
        "base_url": "http://old/v1",
        "api_key": "old",
        "model": "old-model",
    }
    old_cfg = Config.load(write_config(tmp_path, old_data))

    new_data = minimal_config()
    new_data["provider"]["type"] = "openai"
    new_data["provider"]["openai"] = {
        "base_url": "http://new/v1",
        "api_key": "new",
        "model": "new-model",
    }
    new_cfg = Config.load(write_config(tmp_path, new_data))

    assert patch_requires_restart(
        old_cfg,
        new_cfg,
        {"provider": {"openai": {"base_url": "http://new/v1", "api_key": "new", "model": "new-model"}}},
    ) is False


def test_build_runtime_candidate_maps_legacy_ui_to_system_ui(tmp_path):
    cfg = Config.load(write_config(tmp_path, minimal_config()))
    persisted = minimal_config()
    persisted["ui"] = {"home": {"enabled_tabs": ["document", "settings"]}}

    candidate = _build_runtime_candidate(
        cfg,
        patch_dict={"ui": {"home": {"enabled_tabs": ["settings", "document"]}}},
        persisted_data=persisted,
    )

    assert "ui" not in candidate
    assert candidate["system"]["ui"]["home"]["enabled_tabs"] == ["document", "settings"]


def test_serialize_runtime_config_uses_runtime_sections_only(tmp_path):
    data = minimal_config()
    data["system_prompts"] = {"chat": "hello", "_private": "hidden"}
    cfg = Config.load(write_config(tmp_path, data))

    serialized = serialize_runtime_config(cfg)

    assert "ui" not in serialized
    assert serialized["system"]["ui"]["home"]["enabled_tabs"]
    assert serialized["provider"]["prompts"]["chat"] == "hello"
    assert "_private" not in serialized["provider"]["prompts"]
