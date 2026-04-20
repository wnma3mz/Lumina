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
    if hasattr(cfg, "model_dump"):
        res = cfg.model_dump(include={"provider", "system", "digest", "document", "vision", "audio", "ui"})
    else:
        res = {}
        for sec in ["provider", "system", "digest", "document", "vision", "audio", "ui"]:
            obj = getattr(cfg, sec, None)
            if hasattr(obj, "model_dump"):
                res[sec] = obj.model_dump()
                
    for sec in res:
        if isinstance(res[sec], dict) and "prompts" in res[sec]:
            res[sec]["prompts"] = public_system_prompts(res[sec]["prompts"])
            
    return res


def update_runtime_config(cfg: Any, data: dict, *, sections: set[str]) -> None:
    current = cfg.model_dump()
    for sec in sections:
        actual_sec = "provider" if sec == "provider_sampling" else sec
        if actual_sec not in current:
            continue
            
        sec_data = data.get(actual_sec)
        if not isinstance(sec_data, dict):
            continue
            
        if sec == "provider_sampling":
            if "sampling" in sec_data:
                current["provider"]["sampling"] = deep_merge(current["provider"]["sampling"], sec_data["sampling"])
        else:
            current[actual_sec] = deep_merge(current[actual_sec], sec_data)
            if "prompts" in sec_data:
                cfg.system_prompts.update(sec_data["prompts"])

    # Re-validate with deep merged dict
    from lumina.config import Config
    new_cfg = Config.model_validate(current)
    
    # Mutate in-place
    for k in current.keys():
        if hasattr(cfg, k):
            setattr(cfg, k, getattr(new_cfg, k))
