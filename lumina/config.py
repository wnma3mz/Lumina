"""
配置加载：从 config.json 读取，支持环境变量覆盖。

config.json 字段说明
─────────────────────
provider.type        "local"（mlx，macOS）/ "llama_cpp"（Windows/CPU）/ "openai"（远程）
provider.model_path  本地模型目录，null 时使用内置默认路径
provider.sampling    采样参数默认值（与模型绑定）：
                       temperature / top_p / top_k / min_p
                       presence_penalty / repetition_penalty / max_tokens
                       字段缺失时 fallback 到 sampling.py 的 DEFAULT 常量
provider.openai      type=openai 时必填：
                       base_url  服务地址，如 http://192.168.1.10:8080/v1
                       api_key   服务 API Key
                       model     模型名称
whisper_model        Whisper ASR 模型 ID
host / port          HTTP 服务监听地址与端口（默认 127.0.0.1:31821）
log_level            日志级别：DEBUG / INFO / WARNING / ERROR
digest               日报配置（enabled / scan_dirs / history_hours / refresh_hours）
system_prompts       各任务的 system prompt，可按需覆盖
ptt                  PTT 配置（enabled / hotkey / language）
request_history      LLM 请求历史记录配置（启用 / 保留 / 压缩 / 清理）

环境变量优先级高于 config.json，可用于临时覆盖：
  LUMINA_PROVIDER_TYPE / LUMINA_MODEL_PATH
  LUMINA_OPENAI_BASE_URL / LUMINA_OPENAI_API_KEY / LUMINA_OPENAI_MODEL
  LUMINA_WHISPER_MODEL / LUMINA_HOST / LUMINA_PORT / LUMINA_LOG_LEVEL
"""
import json
import os
import sys as _sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

_CONFIG_PATH = Path(__file__).parent / "config.json"
_DEFAULT_MODEL = str(Path.home() / ".lumina" / "models" / "qwen3.5-0.8b-4bit")


@dataclass
class OpenAIProviderConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""


@dataclass
class PttConfig:
    enabled: bool = False
    hotkey: str = "f5"
    language: Optional[str] = "zh"   # None = Whisper 自动检测


@dataclass
class RequestHistoryConfig:
    enabled: bool = True
    capture_full_body: bool = True
    retention_days: int = 14
    max_total_mb: int = 512
    compress_after_days: int = 1
    cleanup_on_startup: bool = True


@dataclass
class SamplingConfig:
    """Provider 级采样参数默认值。None 表示未配置，运行时 fallback 到 sampling.py DEFAULT 常量。"""
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    min_p: Optional[float] = None
    presence_penalty: Optional[float] = None
    repetition_penalty: Optional[float] = None
    max_tokens: Optional[int] = None


@dataclass
class LlamaCppConfig:
    model_path: str = ""        # GGUF 模型文件路径（本地绝对路径）
    n_gpu_layers: int = -1      # -1 = 全部放 GPU；0 = 纯 CPU
    n_ctx: int = 4096


def _default_provider_type() -> str:
    return "llama_cpp" if _sys.platform == "win32" else "local"


@dataclass
class ProviderConfig:
    type: str = field(default_factory=_default_provider_type)  # "local" | "llama_cpp" | "openai"
    model_path: str = _DEFAULT_MODEL
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    openai: OpenAIProviderConfig = field(default_factory=OpenAIProviderConfig)
    llama_cpp: LlamaCppConfig = field(default_factory=LlamaCppConfig)


class Config:
    def __init__(self, path: Optional[str] = None):
        cfg_path = Path(path) if path else _CONFIG_PATH
        with open(cfg_path, "r", encoding="utf-8") as f:
            data: dict = json.load(f)

        # ── Provider ──────────────────────────────────────────────────────────
        p = data.get("provider", {})
        oa = p.get("openai", {})
        lc = p.get("llama_cpp", {})
        sc = p.get("sampling", {}) if isinstance(p.get("sampling"), dict) else {}
        self.provider = ProviderConfig(
            type=os.environ.get("LUMINA_PROVIDER_TYPE") or p.get("type", _default_provider_type()),
            model_path=(
                os.environ.get("LUMINA_MODEL_PATH")
                or p.get("model_path")
                or _DEFAULT_MODEL
            ),
            sampling=SamplingConfig(
                temperature=float(sc["temperature"]) if "temperature" in sc else 0.7,
                top_p=float(sc["top_p"]) if "top_p" in sc else 0.8,
                top_k=int(sc["top_k"]) if "top_k" in sc else 20,
                min_p=float(sc["min_p"]) if "min_p" in sc else 0.0,
                presence_penalty=float(sc["presence_penalty"]) if "presence_penalty" in sc else 1.5,
                repetition_penalty=float(sc["repetition_penalty"]) if "repetition_penalty" in sc else 1.0,
                max_tokens=int(sc["max_tokens"]) if "max_tokens" in sc else 512,
            ),
            openai=OpenAIProviderConfig(
                base_url=os.environ.get("LUMINA_OPENAI_BASE_URL") or oa.get("base_url", ""),
                api_key=os.environ.get("LUMINA_OPENAI_API_KEY") or oa.get("api_key", ""),
                model=os.environ.get("LUMINA_OPENAI_MODEL") or oa.get("model", ""),
            ),
            llama_cpp=LlamaCppConfig(
                model_path=lc.get("model_path", ""),
                n_gpu_layers=int(lc.get("n_gpu_layers", -1)),
                n_ctx=int(lc.get("n_ctx", 4096)),
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
        # 所有 system prompt 默认值统一来自 bundle 内置 config.json；
        # cli/server.py 启动时会将 bundle 默认值与用户配置合并后注入运行时。
        self.system_prompts: Dict[str, str] = data.get("system_prompts", {})

        # ── Digest ────────────────────────────────────────────────────────────
        # 配置传给 digest 模块（不在这里解析，避免循环依赖）
        self.digest: Dict = data.get("digest", {})

        # ── PTT ───────────────────────────────────────────────────────────────
        pt = data.get("ptt", {})
        self.ptt = PttConfig(
            enabled=bool(pt.get("enabled", False)),
            hotkey=pt.get("hotkey", "f5"),
            language=pt.get("language", "zh") or None,
        )

        # ── Request History ───────────────────────────────────────────────────
        rh = data.get("request_history", {})
        if not isinstance(rh, dict):
            rh = {}
        self.request_history = RequestHistoryConfig(
            enabled=bool(rh.get("enabled", True)),
            capture_full_body=bool(rh.get("capture_full_body", True)),
            retention_days=max(0, int(rh.get("retention_days", 14))),
            max_total_mb=max(1, int(rh.get("max_total_mb", 512))),
            compress_after_days=max(0, int(rh.get("compress_after_days", 1))),
            cleanup_on_startup=bool(rh.get("cleanup_on_startup", True)),
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


# ── Lumina 数据目录路径常量 ────────────────────────────────────────────────
# 所有 ~/.lumina/ 子路径集中在此定义，避免各模块重复构造。
LUMINA_HOME = Path.home() / ".lumina"
DIGEST_PATH = LUMINA_HOME / "digest.md"
DIGEST_CONTEXT_LOG_DIR = LUMINA_HOME / "digest_context_log"
DIGEST_SNAPSHOTS_DIR = LUMINA_HOME / "snapshots"
REPORTS_DAILY_DIR = LUMINA_HOME / "reports" / "daily"
REPORTS_WEEKLY_DIR = LUMINA_HOME / "reports" / "weekly"
REPORTS_MONTHLY_DIR = LUMINA_HOME / "reports" / "monthly"
PDF_CACHE_DIR = LUMINA_HOME / "cache" / "pdf"
REQUEST_HISTORY_DIR = LUMINA_HOME / "request_history"

# ── API 客户端默认常量 ────────────────────────────────────────────────────
# CLI 子命令 / 业务模块调用本地服务时的统一默认值。
_DEFAULT_PORT = 31821
DEFAULT_API_BASE_URL = f"http://127.0.0.1:{_DEFAULT_PORT}"
DEFAULT_API_BASE_URL_V1 = f"{DEFAULT_API_BASE_URL}/v1"
DEFAULT_API_KEY = "lumina"
DEFAULT_MODEL = "lumina"
