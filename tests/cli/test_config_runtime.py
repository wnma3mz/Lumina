import json

from lumina.config import Config
from lumina.config_runtime import patch_requires_restart


def _write_config(tmp_path, data: dict) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def _base_config() -> dict:
    return {
        "provider": {
            "type": "local",
            "model_path": "/tmp/model",
            "openai": {"base_url": "", "api_key": "", "model": ""},
            "sampling": {"temperature": 0.6},
        },
        "whisper_model": "whisper-tiny",
        "host": "127.0.0.1",
        "port": 31821,
        "log_level": "INFO",
        "system_prompts": {},
    }


def test_patch_requires_restart_for_host_change(tmp_path):
    old_cfg = Config.load(_write_config(tmp_path, _base_config()))
    new_data = _base_config()
    new_data["host"] = "0.0.0.0"
    new_cfg = Config.load(_write_config(tmp_path, new_data))

    assert patch_requires_restart(
        old_cfg,
        new_cfg,
        {"system": {"server": {"host": "0.0.0.0"}}},
    ) is True


def test_patch_requires_restart_for_mlx_offload_change(tmp_path):
    old_cfg = Config.load(_write_config(tmp_path, _base_config()))
    new_data = _base_config()
    new_data["provider"]["offload_embedding"] = False
    new_cfg = Config.load(_write_config(tmp_path, new_data))

    assert patch_requires_restart(
        old_cfg,
        new_cfg,
        {"provider": {"offload_embedding": False}},
    ) is True


def test_patch_requires_restart_for_mlx_memory_patch(tmp_path):
    old_cfg = Config.load(_write_config(tmp_path, _base_config()))
    new_cfg = Config.load(_write_config(tmp_path, _base_config()))

    assert patch_requires_restart(
        old_cfg,
        new_cfg,
        {"provider": {"mlx_memory": {"offload_audio": False}}},
    ) is True


def test_patch_requires_restart_false_for_openai_subconfig_on_local_backend(tmp_path):
    old_cfg = Config.load(_write_config(tmp_path, _base_config()))
    new_data = _base_config()
    new_data["provider"]["openai"] = {
        "base_url": "http://remote/v1",
        "api_key": "k",
        "model": "m",
    }
    new_cfg = Config.load(_write_config(tmp_path, new_data))

    assert patch_requires_restart(
        old_cfg,
        new_cfg,
        {"provider": {"openai": {"base_url": "http://remote/v1"}}},
    ) is False


def test_patch_requires_restart_false_for_openai_subconfig_on_openai_backend(tmp_path):
    old_data = _base_config()
    old_data["provider"]["type"] = "openai"
    old_data["provider"]["openai"] = {
        "base_url": "http://old/v1",
        "api_key": "old",
        "model": "old-model",
    }
    old_cfg = Config.load(_write_config(tmp_path, old_data))

    new_data = _base_config()
    new_data["provider"]["type"] = "openai"
    new_data["provider"]["openai"] = {
        "base_url": "http://new/v1",
        "api_key": "new",
        "model": "new-model",
    }
    new_cfg = Config.load(_write_config(tmp_path, new_data))

    assert patch_requires_restart(
        old_cfg,
        new_cfg,
        {"provider": {"openai": {"base_url": "http://new/v1", "api_key": "new", "model": "new-model"}}},
    ) is False
