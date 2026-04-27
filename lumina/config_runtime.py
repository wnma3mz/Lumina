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
from dataclasses import dataclass
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


def _mask_api_key(key: str) -> str:
    """将 API key 掩码，仅保留前 4 位供识别。"""
    if not key or len(key) <= 4:
        return "****"
    return key[:4] + "****"


def serialize_runtime_config(cfg: Any) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for section in ("provider", "system", "digest", "document", "vision", "audio"):
        obj = getattr(cfg, section, None)
        if hasattr(obj, "model_dump"):
            res[section] = obj.model_dump()

    for value in res.values():
        if isinstance(value, dict) and "prompts" in value:
            value["prompts"] = public_system_prompts(value["prompts"])

    # 掩码敏感字段：provider.openai.api_key
    provider = res.get("provider", {})
    openai_cfg = provider.get("openai")
    if isinstance(openai_cfg, dict) and openai_cfg.get("api_key"):
        openai_cfg["api_key"] = _mask_api_key(openai_cfg["api_key"])

    return res


@dataclass
class ConfigPatchResult:
    old_cfg: Any
    new_cfg: Any
    data: dict[str, Any]
    patch_dict: dict[str, Any]
    restart_required: bool


def replace_runtime_config(cfg: Any, new_cfg: Any) -> None:
    """将 new_cfg 的字段原地写回已发布的全局 Config 单例。"""
    for key in new_cfg.__class__.model_fields:
        setattr(cfg, key, getattr(new_cfg, key))


def patch_requires_restart(old_cfg: Any, new_cfg: Any, patch_dict: dict[str, Any]) -> bool:
    provider_patch = patch_dict.get("provider")
    if isinstance(provider_patch, dict):
        if "type" in provider_patch:
            return True

        old_backend = getattr(old_cfg.provider, "backend", None)
        new_backend = getattr(new_cfg.provider, "backend", None)
        if old_backend != new_backend:
            return True

        if "llama_cpp" in provider_patch:
            return True
        if "model_path" in provider_patch:
            return True
        if any(k in provider_patch for k in {"offload_embedding", "offload_vision", "offload_audio", "mlx_memory"}):
            return True

    system_patch = patch_dict.get("system")
    if isinstance(system_patch, dict):
        if any(field in system_patch for field in {"desktop"}):
            return True
        server_patch = system_patch.get("server")
        if isinstance(server_patch, dict) and any(field in server_patch for field in {"host", "port"}):
            return True

    return False


def _merge_patch_into_data(data: dict[str, Any], patch_dict: dict[str, Any]) -> dict[str, Any]:
    merged = dict(data)
    for sec, sec_data in patch_dict.items():
        if sec_data is None:
            continue
        current_sec = merged.get(sec, {})
        if not isinstance(current_sec, dict):
            current_sec = {}
        if isinstance(sec_data, dict):
            merged[sec] = deep_merge(current_sec, sec_data)
        else:
            merged[sec] = sec_data
    return merged


def _normalize_persisted_config_data(data: dict[str, Any]) -> dict[str, Any]:
    from lumina.config import normalize_home_tabs, normalize_image_modules

    provider = data.get("provider")
    if isinstance(provider, dict):
        provider.pop("backend", None)
        mlx_memory = provider.get("mlx_memory")
        if not isinstance(mlx_memory, dict):
            mlx_memory = {}
        for key in ("offload_embedding", "offload_vision", "offload_audio"):
            if key in provider:
                mlx_memory[key] = provider.pop(key)
        if mlx_memory:
            provider["mlx_memory"] = mlx_memory

    if isinstance(data.get("vision"), dict) and "enabled_modules" in data["vision"]:
        data["vision"]["enabled_modules"] = normalize_image_modules(data["vision"]["enabled_modules"])

    if isinstance(data.get("ui"), dict):
        home = data["ui"].get("home")
        if isinstance(home, dict) and "enabled_tabs" in home:
            home["enabled_tabs"] = normalize_home_tabs(home["enabled_tabs"])

    if isinstance(data.get("system"), dict):
        branding = data["system"].get("branding")
        if isinstance(branding, dict) and "username" in branding:
            branding["username"] = str(branding["username"] or "").strip()

    return data


class ConfigStore:
    def __init__(self, preferred_path: str | Path | None = None) -> None:
        self._preferred_path = preferred_path

    def apply_patch(self, patch_dict: dict[str, Any], *, cfg: Any) -> ConfigPatchResult:
        from lumina.config import Config

        old_cfg = cfg.model_copy(deep=True)
        data = read_mutable_config_data(self._preferred_path)
        if not isinstance(data, dict):
            data = {}

        merged_data = _merge_patch_into_data(data, patch_dict)
        _normalize_persisted_config_data(merged_data)

        new_cfg = Config.from_data(merged_data)

        write_config_atomic(merged_data, self._preferred_path)
        replace_runtime_config(cfg, new_cfg)

        return ConfigPatchResult(
            old_cfg=old_cfg,
            new_cfg=new_cfg,
            data=merged_data,
            patch_dict=patch_dict,
            restart_required=patch_requires_restart(old_cfg, new_cfg, patch_dict),
        )

