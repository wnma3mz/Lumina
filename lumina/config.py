"""
配置加载：从 config.json 读取，支持环境变量覆盖。
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

_CONFIG_PATH = Path(__file__).parent / "config.json"
_DEFAULT_MODEL = str(Path(__file__).parent.parent / "models" / "qwen3.5-0.8b-4bit")


@dataclass
class OpenAIProviderConfig:
    base_url: str = "http://127.0.0.1:31821/v1"
    api_key: str = "lumina"
    model: str = "lumina"


@dataclass
class ProviderConfig:
    type: str = "local"           # "local" | "openai"
    model_path: str = _DEFAULT_MODEL
    openai: OpenAIProviderConfig = field(default_factory=OpenAIProviderConfig)


class Config:
    def __init__(self, path: Optional[str] = None):
        cfg_path = Path(path) if path else _CONFIG_PATH
        with open(cfg_path, "r", encoding="utf-8") as f:
            data: dict = json.load(f)

        # ── Provider ──────────────────────────────────────────────────────────
        p = data.get("provider", {})
        oa = p.get("openai", {})
        self.provider = ProviderConfig(
            type=os.environ.get("LUMINA_PROVIDER_TYPE") or p.get("type", "local"),
            model_path=(
                os.environ.get("LUMINA_MODEL_PATH")
                or p.get("model_path")
                or _DEFAULT_MODEL
            ),
            openai=OpenAIProviderConfig(
                base_url=os.environ.get("LUMINA_OPENAI_BASE_URL") or oa.get("base_url", "http://127.0.0.1:31821/v1"),
                api_key=os.environ.get("LUMINA_OPENAI_API_KEY") or oa.get("api_key", "lumina"),
                model=os.environ.get("LUMINA_OPENAI_MODEL") or oa.get("model", "lumina"),
            ),
        )

        # ── ASR ───────────────────────────────────────────────────────────────
        self.whisper_model: str = (
            os.environ.get("LUMINA_WHISPER_MODEL")
            or data.get("whisper_model", "mlx-community/whisper-tiny-mlx-4bit")
        )

        # ── Server ────────────────────────────────────────────────────────────
        self.host: str = os.environ.get("LUMINA_HOST") or data.get("host", "127.0.0.1")
        self.port: int = int(os.environ.get("LUMINA_PORT") or data.get("port", 31821))
        self.log_level: str = os.environ.get("LUMINA_LOG_LEVEL") or data.get("log_level", "INFO")

        # ── System Prompts ────────────────────────────────────────────────────
        self.system_prompts: Dict[str, str] = data.get("system_prompts", {})


# 全局单例
_instance: Optional[Config] = None


def get_config(path: Optional[str] = None) -> Config:
    global _instance
    if _instance is None:
        _instance = Config(path)
    return _instance
