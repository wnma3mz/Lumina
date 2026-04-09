"""
配置加载：从 config.json 读取，支持环境变量覆盖。

config.json 字段说明
─────────────────────
provider.type        "local"（本地模型）或 "openai"（远程 OpenAI 兼容接口）
provider.model_path  本地模型目录，null 时使用内置默认路径
provider.openai      type=openai 时必填：
                       base_url  服务地址，如 http://192.168.1.10:8080/v1
                       api_key   服务 API Key
                       model     模型名称
whisper_model        Whisper ASR 模型 ID
host / port          HTTP 服务监听地址与端口（默认 127.0.0.1:31821）
log_level            日志级别：DEBUG / INFO / WARNING / ERROR
digest               日报采集配置（scan_dirs / history_hours / refresh_hours）
system_prompts       各任务的 system prompt，可按需覆盖
ptt                  PTT 热键配置（hotkey / language）

环境变量优先级高于 config.json，可用于临时覆盖：
  LUMINA_PROVIDER_TYPE / LUMINA_MODEL_PATH
  LUMINA_OPENAI_BASE_URL / LUMINA_OPENAI_API_KEY / LUMINA_OPENAI_MODEL
  LUMINA_WHISPER_MODEL / LUMINA_HOST / LUMINA_PORT / LUMINA_LOG_LEVEL
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
    base_url: str = ""
    api_key: str = ""
    model: str = ""


@dataclass
class PttConfig:
    hotkey: str = "f5"
    language: Optional[str] = "zh"   # None = Whisper 自动检测


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
                base_url=os.environ.get("LUMINA_OPENAI_BASE_URL") or oa.get("base_url", ""),
                api_key=os.environ.get("LUMINA_OPENAI_API_KEY") or oa.get("api_key", ""),
                model=os.environ.get("LUMINA_OPENAI_MODEL") or oa.get("model", ""),
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

        # ── Digest ────────────────────────────────────────────────────────────
        # 配置传给 digest 模块（不在这里解析，避免循环依赖）
        self.digest: Dict = data.get("digest", {})

        # ── PTT ───────────────────────────────────────────────────────────────
        pt = data.get("ptt", {})
        self.ptt = PttConfig(
            hotkey=pt.get("hotkey", "f5"),
            language=pt.get("language", "zh") or None,
        )


# 全局单例
_instance: Optional[Config] = None


def get_config(path: Optional[str] = None) -> Config:
    global _instance
    if _instance is None:
        _instance = Config(path)
    return _instance


def reset_config() -> None:
    """重置全局配置单例，下次 get_config() 调用时重新加载。测试用。"""
    global _instance
    _instance = None
