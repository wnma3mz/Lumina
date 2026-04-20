"""
配置加载：从 config.json 读取，支持环境变量覆盖。

config.json 字段说明
─────────────────────
provider.type        "local"（本地模型；macOS=MLX，Win/Linux=llama.cpp）
                     / "llama_cpp"（显式 llama.cpp）/ "openai"（远程）
provider.model_path  本地模型路径，null 时按平台使用内置默认路径
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from lumina.config_runtime import resolve_config_path as resolve_runtime_config_path
from lumina.platform_support.runtime import (
    DEFAULT_PROVIDER_TYPE,
    default_provider_model_path,
    normalize_provider_type,
    resolve_local_model_path,
    resolve_provider_backend,
    resolve_whisper_model,
)
from lumina.api.ui_meta import HOME_TAB_KEYS, IMAGE_TASK_KEYS, LEGACY_HOME_TAB_MAP

_CONFIG_PATH = Path(__file__).parent / "config.json"
_DEFAULT_MODEL = default_provider_model_path(DEFAULT_PROVIDER_TYPE)
_VALID_HOME_TABS = HOME_TAB_KEYS
_DEFAULT_HOME_TABS = list(HOME_TAB_KEYS)
_VALID_IMAGE_MODULES = IMAGE_TASK_KEYS
_DEFAULT_IMAGE_MODULES = list(IMAGE_TASK_KEYS)


def normalize_home_tabs(tabs: Optional[list[str]]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in tabs or []:
        tab = LEGACY_HOME_TAB_MAP.get(str(raw).strip(), str(raw).strip())
        if tab in _VALID_HOME_TABS and tab not in seen:
            normalized.append(tab)
            seen.add(tab)
    return normalized


def normalize_image_modules(modules: Optional[list[str]]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in modules or []:
        mod = str(raw).strip()
        if mod in _VALID_IMAGE_MODULES and mod not in seen:
            normalized.append(mod)
            seen.add(mod)
    return normalized


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
class DesktopConfig:
    menubar_enabled: bool = True


@dataclass
class DocumentConfig:
    pdf_translation_threads: int = 8


@dataclass
class RequestHistoryConfig:
    enabled: bool = True
    capture_full_body: bool = True
    retention_days: int = 14
    max_total_mb: int = 512
    compress_after_days: int = 1
    cleanup_on_startup: bool = True


@dataclass
class UIHomeConfig:
    enabled_tabs: list[str] = field(default_factory=lambda: list(_DEFAULT_HOME_TABS))
    digest_enabled: bool = True
    document_enabled: bool = True
    image_enabled: bool = True
    audio_enabled: bool = False
    image_modules: list[str] = field(default_factory=lambda: list(_DEFAULT_IMAGE_MODULES))
    allow_local_override: bool = True

    def __post_init__(self) -> None:
        self.enabled_tabs = normalize_home_tabs(self.enabled_tabs) or list(_DEFAULT_HOME_TABS)
        self.image_modules = normalize_image_modules(self.image_modules) or list(_DEFAULT_IMAGE_MODULES)


@dataclass
class UIConfig:
    home: UIHomeConfig = field(default_factory=UIHomeConfig)


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
    model_path: str = field(default_factory=lambda: default_provider_model_path("llama_cpp"))
    n_gpu_layers: int = -1      # -1 = 全部放 GPU；0 = 纯 CPU
    n_ctx: int = 4096


def _default_provider_type() -> str:
    return DEFAULT_PROVIDER_TYPE


@dataclass
class ProviderConfig:
    type: str = field(default_factory=_default_provider_type)  # "local" | "llama_cpp" | "openai"
    model_path: str = _DEFAULT_MODEL
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    openai: OpenAIProviderConfig = field(default_factory=OpenAIProviderConfig)
    llama_cpp: LlamaCppConfig = field(default_factory=LlamaCppConfig)
    backend: str = field(init=False)

    def __post_init__(self) -> None:
        self.type = normalize_provider_type(self.type)
        self.model_path = resolve_local_model_path(self.model_path, self.type)
        self.backend = resolve_provider_backend(self.type)
        if not self.llama_cpp.model_path:
            self.llama_cpp.model_path = default_provider_model_path("llama_cpp")


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
        requested_provider_type = os.environ.get("LUMINA_PROVIDER_TYPE") or p.get("type", _default_provider_type())
        requested_provider_type = normalize_provider_type(requested_provider_type)
        provider_model_path = resolve_local_model_path(
            os.environ.get("LUMINA_MODEL_PATH") or p.get("model_path"),
            requested_provider_type,
        )
        llama_cpp_model_path = resolve_local_model_path(
            lc.get("model_path") or provider_model_path,
            "llama_cpp",
        )

        self.provider = ProviderConfig(
            type=requested_provider_type,
            model_path=provider_model_path,
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
                model_path=llama_cpp_model_path,
                n_gpu_layers=int(lc.get("n_gpu_layers", -1)),
                n_ctx=int(lc.get("n_ctx", 4096)),
            ),
        )

        # ── ASR ───────────────────────────────────────────────────────────────
        self.whisper_model: str = resolve_whisper_model(
            os.environ.get("LUMINA_WHISPER_MODEL") or data.get("whisper_model")
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

        # ── Desktop ───────────────────────────────────────────────────────────
        desktop = data.get("desktop", {})
        if not isinstance(desktop, dict):
            desktop = {}
        self.desktop = DesktopConfig(
            menubar_enabled=bool(desktop.get("menubar_enabled", True)),
        )

        # ── Document ──────────────────────────────────────────────────────────
        doc = data.get("document", {})
        if not isinstance(doc, dict):
            doc = {}
        self.document = DocumentConfig(
            pdf_translation_threads=max(1, int(doc.get("pdf_translation_threads", 8))),
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

        # ── Branding ──────────────────────────────────────────────────────────
        branding = data.get("branding", {})
        if not isinstance(branding, dict):
            branding = {}
        slogans = branding.get("slogans", [])
        if not isinstance(slogans, list):
            slogans = []
        username = str(branding.get("username", "") or "").strip()
        self.branding: Dict = {
            "username": username,
            "slogans": [str(s).strip() for s in slogans if str(s).strip()],
        }

        # ── UI ────────────────────────────────────────────────────────────────
        ui = data.get("ui", {})
        if not isinstance(ui, dict):
            ui = {}
        home = ui.get("home", {})
        if not isinstance(home, dict):
            home = {}
        enabled_tabs = normalize_home_tabs(home.get("enabled_tabs", []))
        image_modules = normalize_image_modules(home.get("image_modules", home.get("lab_modules", [])))
        self.ui = UIConfig(
            home=UIHomeConfig(
                enabled_tabs=enabled_tabs or list(_DEFAULT_HOME_TABS),
                digest_enabled=bool(home.get("digest_enabled", True)),
                document_enabled=bool(home.get("document_enabled", True)),
                image_enabled=bool(home.get("image_enabled", home.get("lab_enabled", True))),
                audio_enabled=bool(home.get("audio_enabled", False)),
                image_modules=image_modules or list(_DEFAULT_IMAGE_MODULES),
                allow_local_override=bool(home.get("allow_local_override", True)),
            )
        )


# 全局单例
_instance: Optional[Config] = None
_instance_source_path: Optional[str] = None


def get_config(path: Optional[str] = None) -> Config:
    global _instance, _instance_source_path
    resolved_path = resolve_runtime_config_path(path)
    if _instance is None or (path is not None and resolved_path != _instance_source_path):
        _instance = Config(resolved_path)
        _instance_source_path = resolved_path
    return _instance


def reset_config() -> None:
    """重置全局配置单例，下次 get_config() 调用时重新加载。测试用。"""
    global _instance, _instance_source_path
    _instance = None
    _instance_source_path = None


# ── Lumina 数据目录路径常量 ────────────────────────────────────────────────
# 所有 ~/.lumina/ 子路径集中在此定义，避免各模块重复构造。
LUMINA_HOME = Path.home() / ".lumina"
DIGEST_PATH = LUMINA_HOME / "digest.md"
DIGEST_COLLECTOR_STATE_PATH = LUMINA_HOME / "digest_collectors.json"
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
