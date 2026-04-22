"""测试配置辅助：生成持久化层使用的 legacy config payload。"""

import json
from copy import deepcopy

from lumina.config import reset_config
from lumina.config_runtime import set_active_config_path


def reset_config_state() -> None:
    reset_config()
    set_active_config_path(None)


def write_config(tmp_path, data: dict) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def legacy_minimal_config_payload() -> dict:
    return {
        "provider": {
            "type": "local",
            "model_path": "/tmp/model",
            "openai": {"base_url": "", "api_key": "", "model": ""},
        },
        "whisper_model": "whisper-tiny",
        "host": "127.0.0.1",
        "port": 31821,
        "log_level": "INFO",
        "system_prompts": {},
    }


def legacy_app_config_payload() -> dict:
    data = deepcopy(legacy_minimal_config_payload())
    data["provider"]["sampling"] = {
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
        "max_tokens": 512,
    }
    data["system_prompts"] = {"_readme": "internal", "chat": "You are helpful."}
    data["digest"] = {"enabled": False}
    data["ptt"] = {"enabled": False, "hotkey": "f5", "language": "zh"}
    data["desktop"] = {"menubar_enabled": True}
    data["request_history"] = {"enabled": True}
    data["branding"] = {"username": "", "slogans": ["让 AI 留在本地"]}
    data["ui"] = {
        "home": {
            "enabled_tabs": ["digest", "document", "image", "settings"],
            "image_enabled": True,
            "image_modules": ["image_ocr"],
            "allow_local_override": True,
        }
    }
    return data
