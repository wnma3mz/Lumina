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

server               HTTP 服务绑定与日志
digest               回顾域配置（后台自动化及提取）
document             文档域配置（PDF 翻译、摘要等）
vision               视觉域配置（图像解析）
audio                音频域配置（ASR、PTT 等）

环境变量优先级高于 config.json，可用于临时覆盖：
  LUMINA_PROVIDER_TYPE / LUMINA_MODEL_PATH
  LUMINA_OPENAI_BASE_URL / LUMINA_OPENAI_API_KEY / LUMINA_OPENAI_MODEL
  LUMINA_WHISPER_MODEL / LUMINA_HOST / LUMINA_PORT / LUMINA_LOG_LEVEL
"""
import json
import os
import threading
from pydantic import BaseModel, Field, ConfigDict, model_validator, computed_field
from typing import Any, Dict, Optional
from pathlib import Path

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

from lumina.config_runtime import resolve_config_path as resolve_runtime_config_path  # noqa: E402
from lumina.platform_support.runtime import (  # noqa: E402
    DEFAULT_PROVIDER_TYPE,
    default_provider_model_path,
    normalize_provider_type,
    resolve_local_model_path,
    resolve_provider_backend,
    resolve_whisper_model,
)
from lumina.api.ui_meta import HOME_TAB_KEYS, IMAGE_TASK_KEYS, LEGACY_HOME_TAB_MAP  # noqa: E402

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


class OpenAIProviderConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class PttConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = False
    hotkey: str = "f5"
    language: Optional[str] = "zh"   # None = Whisper 自动检测


class AudioConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = False
    whisper_model: str = ""
    ptt: PttConfig = Field(default_factory=PttConfig)
    prompts: Dict[str, str] = Field(default_factory=dict)


class DesktopConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    menubar_enabled: bool = True


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    host: str = "127.0.0.1"
    port: int = 31821
    log_level: str = "INFO"


class VisionConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = True
    max_image_mb: int = 12
    enabled_modules: list[str] = Field(default_factory=lambda: list(_DEFAULT_IMAGE_MODULES))
    prompts: Dict[str, str] = Field(default_factory=dict)


class RequestHistoryConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = True
    capture_full_body: bool = True
    retention_days: int = 14
    max_total_mb: int = 512
    compress_after_days: int = 1
    cleanup_on_startup: bool = True

    @model_validator(mode="after")
    def clamp_values(self) -> "RequestHistoryConfig":
        self.retention_days = max(0, self.retention_days)
        self.max_total_mb = max(1, self.max_total_mb)
        self.compress_after_days = max(0, self.compress_after_days)
        return self


class BrandingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    username: str = ""
    slogans: list[str] = Field(default_factory=list)


class UIHomeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled_tabs: list[str] = Field(default_factory=lambda: list(_DEFAULT_HOME_TABS))
    allow_local_override: bool = True
    
    def model_post_init(self, __context: Any) -> None:
        self.enabled_tabs = normalize_home_tabs(self.enabled_tabs) or list(_DEFAULT_HOME_TABS)


class UIConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    home: UIHomeConfig = Field(default_factory=UIHomeConfig)


class SystemConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    server: ServerConfig = Field(default_factory=ServerConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    request_history: RequestHistoryConfig = Field(default_factory=RequestHistoryConfig)
    branding: BrandingConfig = Field(default_factory=BrandingConfig)
    ui: UIConfig = Field(default_factory=UIConfig)


class SamplingConfig(BaseModel):
    """Provider 级采样参数默认值。None 表示未配置，运行时 fallback 到 sampling.py DEFAULT 常量。"""
    model_config = ConfigDict(extra="ignore")
    temperature: float = 0.7
    top_p: float = 0.8
    top_k: int = 20
    min_p: float = 0.0
    presence_penalty: float = 1.5
    repetition_penalty: float = 1.0
    max_tokens: int = 512
    
    @model_validator(mode="before")
    @classmethod
    def allow_empty_strings(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        res = {}
        for k, v in data.items():
            if str(v).strip() == "":
                if k in cls.model_fields:
                    res[k] = cls.model_fields[k].default
            else:
                res[k] = v
        return res


class DocumentConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = True
    pdf_translation_threads: int = 8
    prompts: Dict[str, str] = Field(default_factory=dict)
    sampling: SamplingConfig = Field(default_factory=SamplingConfig)


class LlamaCppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model_path: str = Field(default_factory=lambda: default_provider_model_path("llama_cpp"))
    n_gpu_layers: int = -1      # -1 = 全部放 GPU；0 = 纯 CPU
    n_ctx: int = 4096


def _default_provider_type() -> str:
    return DEFAULT_PROVIDER_TYPE


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str = Field(default_factory=_default_provider_type)  # "local" | "llama_cpp" | "openai"
    model_path: str = _DEFAULT_MODEL
    # MLX 内存分层参数（扁平化存储，从 config.json 的 mlx_memory 子对象或顶层读取）
    offload_embedding: bool = True
    offload_vision: bool = True
    offload_audio: bool = True
    sampling: SamplingConfig = Field(default_factory=SamplingConfig)
    openai: OpenAIProviderConfig = Field(default_factory=OpenAIProviderConfig)
    llama_cpp: LlamaCppConfig = Field(default_factory=LlamaCppConfig)
    prompts: Dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _unpack_mlx_memory(cls, data: Any) -> Any:
        """将 config.json 中的 mlx_memory 子对象展开为顶层字段。

        支持两种写法（后者向后兼容）：
          1. provider.mlx_memory.offload_embedding = true  （推荐，文档格式）
          2. provider.offload_embedding = true             （旧格式，直接平铺）
        """
        if not isinstance(data, dict):
            return data
        mem = data.pop("mlx_memory", None)
        if isinstance(mem, dict):
            for key in ("offload_embedding", "offload_vision", "offload_audio"):
                if key not in data and key in mem:
                    data[key] = mem[key]
        return data

    @computed_field
    @property
    def backend(self) -> str:
        return resolve_provider_backend(self.type)


from lumina.services.digest.config import DigestConfig  # noqa: E402

class Config(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    system: SystemConfig = Field(default_factory=SystemConfig)
    digest: DigestConfig = Field(default_factory=DigestConfig)
    document: DocumentConfig = Field(default_factory=DocumentConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    
    system_prompts: Dict[str, str] = Field(default_factory=dict)
    
    def model_post_init(self, __context: Any) -> None:
        # 聚合所有的 system_prompts 以供底层 Engine 使用
        self.system_prompts.update(self.provider.prompts)
        self.system_prompts.update(self.digest.prompts)
        self.system_prompts.update(self.document.prompts)
        self.system_prompts.update(self.vision.prompts)
        self.system_prompts.update(self.audio.prompts)

    @property
    def ui(self): return self.system.ui
    @property
    def server(self): return self.system.server
    @property
    def desktop(self): return self.system.desktop
    @property
    def request_history(self): return self.system.request_history
    @property
    def branding(self): return {"username": self.system.branding.username, "slogans": self.system.branding.slogans}
    @property
    def host(self): return self.system.server.host
    @property
    def port(self): return self.system.server.port
    @property
    def log_level(self): return self.system.server.log_level
    @property
    def ptt(self): return self.audio.ptt
    @property
    def whisper_model(self): return self.audio.whisper_model
    @property
    def backend(self): return self.provider.backend

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        cfg_path = Path(path) if path else _CONFIG_PATH
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data: dict = json.load(f)
        except Exception:
            data = {}
            
        if not isinstance(data, dict):
            data = {}

        # ── Migration from old config format ──────────────────────────────────
        p = data.setdefault("provider", {})
        if not isinstance(p, dict):
            p = {}
            data["provider"] = p
        oa = p.setdefault("openai", {})
        if not isinstance(oa, dict):
            oa = {}
            p["openai"] = oa
        lc = p.setdefault("llama_cpp", {})
        if not isinstance(lc, dict):
            lc = {}
            p["llama_cpp"] = lc
        sc = p.get("sampling", {}) if isinstance(p.get("sampling"), dict) else {}
        
        req_type = os.environ.get("LUMINA_PROVIDER_TYPE") or p.get("type", _default_provider_type())
        p["type"] = normalize_provider_type(req_type)
        
        p["model_path"] = resolve_local_model_path(
            os.environ.get("LUMINA_MODEL_PATH") or p.get("model_path"),
            p["type"],
        )
        lc["model_path"] = resolve_local_model_path(
            lc.get("model_path") or p["model_path"],
            "llama_cpp",
        )
        
        p["sampling"] = SamplingConfig.model_validate(sc).model_dump()
        
        oa["base_url"] = os.environ.get("LUMINA_OPENAI_BASE_URL") or oa.get("base_url", "")
        oa["api_key"] = os.environ.get("LUMINA_OPENAI_API_KEY") or oa.get("api_key", "")
        oa["model"] = os.environ.get("LUMINA_OPENAI_MODEL") or oa.get("model", "")
        
        # BUG-14: 用户填写非数字字符串时 int() 直接抛 ValueError，服务无法启动且报错不友好
        try:
            lc["n_gpu_layers"] = int(lc.get("n_gpu_layers", -1))
            lc["n_ctx"] = int(lc.get("n_ctx", 4096))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"config.json provider.llama_cpp 字段类型错误（期望整数）: {exc}"
            ) from exc
        
        sys_p = data.get("system_prompts", {})
        if "prompts" not in p or not isinstance(p["prompts"], dict):
            p["prompts"] = {}
        p["prompts"]["chat"] = p["prompts"].get("chat", sys_p.get("chat", "You are a helpful assistant."))
        
        # ── System ────────────────────────────────────────────────────────────
        sys_cfg = data.setdefault("system", {})
        if not isinstance(sys_cfg, dict):
            sys_cfg = {}
            data["system"] = sys_cfg
        srv = sys_cfg.setdefault("server", {})
        if not isinstance(srv, dict):
            srv = {}
            sys_cfg["server"] = srv
        
        srv_old = data.get("server", {})
        if isinstance(srv_old, dict) and not srv:
            srv.update(srv_old)
        
        srv["host"] = os.environ.get("LUMINA_HOST") or srv.get("host") or data.get("host", "127.0.0.1")
        srv["port"] = int(os.environ.get("LUMINA_PORT") or srv.get("port") or data.get("port", 31821))
        srv["log_level"] = os.environ.get("LUMINA_LOG_LEVEL") or srv.get("log_level") or data.get("log_level", "INFO")
        
        desktop = sys_cfg.setdefault("desktop", {})
        if not isinstance(desktop, dict):
            desktop = {}
            sys_cfg["desktop"] = desktop
        if not desktop:
            d_old = data.get("desktop", {})
            if isinstance(d_old, dict):
                desktop.update(d_old)
        if "menubar_enabled" not in desktop:
            desktop["menubar_enabled"] = True
            
        rh = sys_cfg.setdefault("request_history", {})
        if not isinstance(rh, dict):
            rh = {}
            sys_cfg["request_history"] = rh
        if not rh:
            rh_old = data.get("request_history", {})
            if isinstance(rh_old, dict):
                rh.update(rh_old)
            
        branding = sys_cfg.setdefault("branding", {})
        if not isinstance(branding, dict):
            branding = {}
            sys_cfg["branding"] = branding
        if not branding:
            branding_old = data.get("branding", {})
            if isinstance(branding_old, dict):
                branding.update(branding_old)
        
        branding["username"] = str(branding.get("username", "") or "").strip()
        branding["slogans"] = [str(item).strip() for item in branding.get("slogans", []) if str(item).strip()]
        
        ui = sys_cfg.setdefault("ui", {})
        if not isinstance(ui, dict):
            ui = {}
            sys_cfg["ui"] = ui
        if not ui:
            ui_old = data.get("ui", {})
            if isinstance(ui_old, dict):
                ui.update(ui_old)
            
        # ── Digest ────────────────────────────────────────────────────────────
        d = data.setdefault("digest", {})
        if not isinstance(d, dict):
            d = {}
            data["digest"] = d
        home = ui.get("home", {})
        if not isinstance(home, dict):
            home = {}
        
        if "enabled" not in d:
            if "digest_enabled" in home:
                d["enabled"] = bool(home.get("digest_enabled"))
            else:
                # BUG-13: 全新安装时（无 digest 段且无 digest_enabled 键）不应默认启用日报
                # DigestConfig.enabled = False，此处保持语义一致
                d["enabled"] = False
                
        d["history_hours"] = float(d.get("history_hours", 24.0))
        d["refresh_hours"] = float(d.get("refresh_hours", 1.0))
        d["notify_time"] = str(d.get("notify_time", "20:00"))
        d["weekly_report_day"] = max(0, min(6, int(d.get("weekly_report_day", 0))))
        d["monthly_report_day"] = max(1, min(28, int(d.get("monthly_report_day", 1))))
        d["ai_queries_max_source_chars"] = max(1, int(d.get("ai_queries_max_source_chars", 4000)))
        
        if "prompts" not in d or not isinstance(d["prompts"], dict):
            d["prompts"] = {}
        dp = d["prompts"]
        dp["digest"] = dp.get("digest", sys_p.get("digest", ""))
        dp["daily_report"] = dp.get("daily_report", sys_p.get("daily_report", ""))
        dp["weekly_report"] = dp.get("weekly_report", sys_p.get("weekly_report", ""))
        dp["monthly_report"] = dp.get("monthly_report", sys_p.get("monthly_report", ""))
        
        ds = d.get("sampling", {})
        d["sampling"] = SamplingConfig.model_validate(ds).model_dump()
        
        # ── Document ──────────────────────────────────────────────────────────
        doc = data.setdefault("document", {})
        if not isinstance(doc, dict):
            doc = {}
            data["document"] = doc
        if "enabled" not in doc:
            doc["enabled"] = bool(doc.get("enabled", home.get("document_enabled", True)))
        doc["pdf_translation_threads"] = max(1, int(doc.get("pdf_translation_threads", 8)))
        
        if "prompts" not in doc or not isinstance(doc["prompts"], dict):
            doc["prompts"] = {}
        d_p = doc["prompts"]
        d_p["translate_to_zh"] = d_p.get("translate_to_zh", sys_p.get("translate_to_zh", ""))
        d_p["translate_to_en"] = d_p.get("translate_to_en", sys_p.get("translate_to_en", ""))
        d_p["summarize"] = d_p.get("summarize", sys_p.get("summarize", ""))
        d_p["polish_zh"] = d_p.get("polish_zh", sys_p.get("polish_zh", ""))
        d_p["polish_en"] = d_p.get("polish_en", sys_p.get("polish_en", ""))
        
        doc_s = doc.get("sampling", {})
        doc["sampling"] = SamplingConfig.model_validate(doc_s).model_dump()
        
        # ── Vision ────────────────────────────────────────────────────────────
        vis = data.setdefault("vision", {})
        if not isinstance(vis, dict):
            vis = {}
            data["vision"] = vis
        if "enabled" not in vis:
            vis["enabled"] = bool(vis.get("enabled", home.get("image_enabled", home.get("lab_enabled", True))))
        vis["max_image_mb"] = max(1, int(vis.get("max_image_mb", 12)))
        vis["enabled_modules"] = normalize_image_modules(vis.get("enabled_modules", home.get("image_modules", home.get("lab_modules", []))))
        
        if "prompts" not in vis or not isinstance(vis["prompts"], dict):
            vis["prompts"] = {}
        v_p = vis["prompts"]
        v_p["image_ocr"] = v_p.get("image_ocr", sys_p.get("image_ocr", ""))
        v_p["image_caption"] = v_p.get("image_caption", sys_p.get("image_caption", ""))
        
        # ── Audio ─────────────────────────────────────────────────────────────
        aud = data.setdefault("audio", {})
        if not isinstance(aud, dict):
            aud = {}
            data["audio"] = aud
        if "enabled" not in aud:
            aud["enabled"] = bool(aud.get("enabled", home.get("audio_enabled", False)))
            
        aud["whisper_model"] = resolve_whisper_model(
            os.environ.get("LUMINA_WHISPER_MODEL") or aud.get("whisper_model") or data.get("whisper_model")
        )
        
        pt = aud.setdefault("ptt", {})
        if not isinstance(pt, dict):
            pt = {}
            aud["ptt"] = pt
        if not pt:
            pt_old = data.get("ptt", {})
            if isinstance(pt_old, dict):
                pt.update(pt_old)
            
        if "prompts" not in aud or not isinstance(aud["prompts"], dict):
            aud["prompts"] = {}
        a_p = aud["prompts"]
        a_p["asr_zh"] = a_p.get("asr_zh", sys_p.get("asr_zh", ""))
        a_p["asr_en"] = a_p.get("asr_en", sys_p.get("asr_en", ""))
        a_p["live_translate"] = a_p.get("live_translate", sys_p.get("live_translate", ""))
        
        return cls.model_validate(data)

# 全局单例
_instance: Optional[Config] = None
_instance_source_path: Optional[str] = None
_instance_lock = threading.Lock()  # BUG-12: 防止多线程并发时重复初始化单例


def get_config(path: Optional[str] = None) -> Config:
    global _instance, _instance_source_path
    resolved_path = resolve_runtime_config_path(path)
    # BUG-12: double-checked locking，先快速检查再加锁，避免每次调用都获取锁
    if _instance is None or (path is not None and resolved_path != _instance_source_path):
        with _instance_lock:
            if _instance is None or (path is not None and resolved_path != _instance_source_path):
                _instance = Config.load(resolved_path)
                _instance_source_path = resolved_path
    return _instance


def peek_config() -> Optional[Config]:
    """返回当前已加载的配置单例；若尚未初始化则返回 None。"""
    return _instance


def reset_config() -> None:
    """重置全局配置单例，下次 get_config() 调用时重新加载。测试用。"""
    global _instance, _instance_source_path
    _instance = None
    _instance_source_path = None
