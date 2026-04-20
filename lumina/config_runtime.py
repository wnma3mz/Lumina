"""
lumina/config_runtime.py — 配置文件路径、持久化与运行时同步辅助。

目标：
- 统一 CLI / API / 菜单栏对“当前配置文件”的理解
- 提供原子写入、模板补字段、公开配置序列化
- 收口部分运行时热更新逻辑，避免各处手写同步代码
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("lumina")

USER_CONFIG_PATH = Path.home() / ".lumina" / "config.json"
PACKAGE_CONFIG_PATH = Path(__file__).parent / "config.json"

_active_config_path: Optional[Path] = None


def set_active_config_path(path: str | Path | None) -> None:
    global _active_config_path
    _active_config_path = Path(path) if path else None


def get_active_config_path() -> Optional[Path]:
    return _active_config_path


def deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def flatten_keys(data: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            keys |= flatten_keys(value, full)
        else:
            keys.add(full)
    return keys


def read_json_file(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve_config_path(preferred_path: str | Path | None = None) -> str | None:
    if preferred_path:
        return str(Path(preferred_path))
    if _active_config_path is not None:
        return str(_active_config_path)
    if USER_CONFIG_PATH.exists():
        return str(USER_CONFIG_PATH)
    return None


def read_config_data(preferred_path: str | Path | None = None) -> dict:
    candidate = resolve_config_path(preferred_path)
    if candidate:
        path = Path(candidate)
        if path.exists():
            return read_json_file(path)
    if PACKAGE_CONFIG_PATH.exists():
        return read_json_file(PACKAGE_CONFIG_PATH)
    return {}


def writable_config_path(preferred_path: str | Path | None = None) -> Path:
    candidate = resolve_config_path(preferred_path)
    if candidate:
        return Path(candidate)
    return USER_CONFIG_PATH


def read_mutable_config_data(preferred_path: str | Path | None = None) -> dict:
    target = writable_config_path(preferred_path)
    if target.exists():
        return read_json_file(target)
    if PACKAGE_CONFIG_PATH.exists():
        return read_json_file(PACKAGE_CONFIG_PATH)
    return {}


def write_config_atomic(data: dict, preferred_path: str | Path | None = None) -> Path:
    target = writable_config_path(preferred_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.stem}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        tmp.replace(target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return target


def sync_runtime_config(preferred_path: str | Path | None = None) -> list[str]:
    target = writable_config_path(preferred_path)
    if not target.exists() or not PACKAGE_CONFIG_PATH.exists():
        return []

    template = read_json_file(PACKAGE_CONFIG_PATH)
    current = read_json_file(target)
    merged = deep_merge(template, current)
    if merged == current:
        return []
    write_config_atomic(merged, target)
    return sorted(flatten_keys(merged) - flatten_keys(current))


def public_system_prompts(prompts: Optional[dict[str, Any]]) -> dict[str, str]:
    if not isinstance(prompts, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in prompts.items()
        if isinstance(key, str) and not key.startswith("_")
    }


def serialize_runtime_config(cfg: Any) -> dict[str, Any]:
    return {
        "provider": {
            "type": cfg.provider.type,
            "backend": cfg.provider.backend,
            "model_path": cfg.provider.model_path,
            "sampling": {
                "temperature": cfg.provider.sampling.temperature,
                "top_p": cfg.provider.sampling.top_p,
                "top_k": cfg.provider.sampling.top_k,
                "min_p": cfg.provider.sampling.min_p,
                "presence_penalty": cfg.provider.sampling.presence_penalty,
                "repetition_penalty": cfg.provider.sampling.repetition_penalty,
                "max_tokens": cfg.provider.sampling.max_tokens,
            },
            "openai": {
                "base_url": cfg.provider.openai.base_url,
                "api_key": cfg.provider.openai.api_key,
                "model": cfg.provider.openai.model,
            },
            "llama_cpp": {
                "model_path": cfg.provider.llama_cpp.model_path,
                "n_gpu_layers": cfg.provider.llama_cpp.n_gpu_layers,
                "n_ctx": cfg.provider.llama_cpp.n_ctx,
            },
        },
        "whisper_model": cfg.whisper_model,
        "host": cfg.host,
        "port": cfg.port,
        "log_level": cfg.log_level,
        "digest": {
            "enabled": cfg.digest.get("enabled", False) if isinstance(cfg.digest, dict) else False,
            "scan_dirs": cfg.digest.get("scan_dirs", []) if isinstance(cfg.digest, dict) else [],
            "history_hours": cfg.digest.get("history_hours", 24) if isinstance(cfg.digest, dict) else 24,
            "refresh_hours": cfg.digest.get("refresh_hours", 1) if isinstance(cfg.digest, dict) else 1,
            "notify_time": cfg.digest.get("notify_time", "20:00") if isinstance(cfg.digest, dict) else "20:00",
            "enabled_collectors": cfg.digest.get("enabled_collectors") if isinstance(cfg.digest, dict) else None,
            "weekly_report_day": cfg.digest.get("weekly_report_day", 0) if isinstance(cfg.digest, dict) else 0,
            "monthly_report_day": cfg.digest.get("monthly_report_day", 1) if isinstance(cfg.digest, dict) else 1,
            "ai_queries_max_source_chars": cfg.digest.get("ai_queries_max_source_chars", 4000) if isinstance(cfg.digest, dict) else 4000,
        },
        "ptt": {
            "enabled": cfg.ptt.enabled,
            "hotkey": cfg.ptt.hotkey,
            "language": cfg.ptt.language,
        },
        "desktop": {
            "menubar_enabled": cfg.desktop.menubar_enabled,
        },
        "document": {
            "pdf_translation_threads": cfg.document.pdf_translation_threads,
        },
        "request_history": {
            "enabled": cfg.request_history.enabled,
            "capture_full_body": cfg.request_history.capture_full_body,
            "retention_days": cfg.request_history.retention_days,
            "max_total_mb": cfg.request_history.max_total_mb,
            "compress_after_days": cfg.request_history.compress_after_days,
            "cleanup_on_startup": cfg.request_history.cleanup_on_startup,
        },
        "branding": {
            "username": cfg.branding.get("username", "") if isinstance(cfg.branding, dict) else "",
            "slogans": cfg.branding.get("slogans", []) if isinstance(cfg.branding, dict) else [],
        },
        "ui": {
            "home": {
                "enabled_tabs": cfg.ui.home.enabled_tabs,
                "digest_enabled": cfg.ui.home.digest_enabled,
                "document_enabled": cfg.ui.home.document_enabled,
                "image_enabled": cfg.ui.home.image_enabled,
                "image_modules": cfg.ui.home.image_modules,
                "allow_local_override": cfg.ui.home.allow_local_override,
            }
        },
        "system_prompts": public_system_prompts(cfg.system_prompts),
    }


def update_runtime_config(cfg: Any, data: dict, *, sections: set[str]) -> None:
    if "digest" in sections:
        cfg.digest = data.get("digest", {}) if isinstance(data.get("digest"), dict) else {}

    if "desktop" in sections:
        desktop = data.get("desktop", {})
        if not isinstance(desktop, dict):
            desktop = {}
        cfg.desktop.menubar_enabled = bool(desktop.get("menubar_enabled", True))

    if "document" in sections:
        doc = data.get("document", {})
        if not isinstance(doc, dict):
            doc = {}
        cfg.document.pdf_translation_threads = max(1, int(doc.get("pdf_translation_threads", 8)))

    if "request_history" in sections:
        request_history = data.get("request_history", {})
        if not isinstance(request_history, dict):
            request_history = {}
        cfg.request_history.enabled = bool(request_history.get("enabled", True))
        cfg.request_history.capture_full_body = bool(request_history.get("capture_full_body", True))
        cfg.request_history.retention_days = max(0, int(request_history.get("retention_days", 14)))
        cfg.request_history.max_total_mb = max(1, int(request_history.get("max_total_mb", 512)))
        cfg.request_history.compress_after_days = max(0, int(request_history.get("compress_after_days", 1)))
        cfg.request_history.cleanup_on_startup = bool(request_history.get("cleanup_on_startup", True))

    if "branding" in sections:
        branding = data.get("branding", {})
        if not isinstance(branding, dict):
            branding = {}
        slogans = branding.get("slogans", [])
        if not isinstance(slogans, list):
            slogans = []
        cfg.branding = {
            "username": str(branding.get("username", "") or "").strip(),
            "slogans": [str(item).strip() for item in slogans if str(item).strip()],
        }

    if "ui" in sections:
        from lumina.config import UIConfig, UIHomeConfig

        ui = data.get("ui", {})
        if not isinstance(ui, dict):
            ui = {}
        home = ui.get("home", {})
        if not isinstance(home, dict):
            home = {}
        cfg.ui = UIConfig(
            home=UIHomeConfig(
                enabled_tabs=list(home.get("enabled_tabs", cfg.ui.home.enabled_tabs)),
                digest_enabled=bool(home.get("digest_enabled", cfg.ui.home.digest_enabled)),
                document_enabled=bool(home.get("document_enabled", cfg.ui.home.document_enabled)),
                image_enabled=bool(home.get("image_enabled", cfg.ui.home.image_enabled)),
                image_modules=list(home.get("image_modules", cfg.ui.home.image_modules)),
                allow_local_override=bool(home.get("allow_local_override", cfg.ui.home.allow_local_override)),
            )
        )

    if "provider_sampling" in sections:
        from lumina.config import SamplingConfig

        sampling = data.get("provider", {}).get("sampling", {})
        if not isinstance(sampling, dict):
            sampling = {}
        cfg.provider.sampling = SamplingConfig(
            temperature=float(sampling["temperature"]) if "temperature" in sampling else None,
            top_p=float(sampling["top_p"]) if "top_p" in sampling else None,
            top_k=int(sampling["top_k"]) if "top_k" in sampling else None,
            min_p=float(sampling["min_p"]) if "min_p" in sampling else None,
            presence_penalty=float(sampling["presence_penalty"]) if "presence_penalty" in sampling else None,
            repetition_penalty=float(sampling["repetition_penalty"]) if "repetition_penalty" in sampling else None,
            max_tokens=int(sampling["max_tokens"]) if "max_tokens" in sampling else None,
        )
