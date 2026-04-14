"""
lumina/api/routers/config.py — 配置读取与更新接口

GET  /v1/config  — 返回当前运行时配置（合并 config.json + 环境变量后的值）
PATCH /v1/config — 部分更新配置，写回 ~/.lumina/config.json；可热重载字段立即生效
"""
import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger("lumina")

router = APIRouter(tags=["config"])

# 防止并发写
_write_lock = asyncio.Lock()

# 用户配置文件路径（与 cli/utils.py 一致）
_USER_CONFIG_PATH = Path.home() / ".lumina" / "config.json"
_PKG_CONFIG_PATH = Path(__file__).parent.parent.parent / "config.py"


# ── Pydantic 请求体 ────────────────────────────────────────────────────────────

class OpenAIPatch(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None


class ProviderPatch(BaseModel):
    type: Optional[str] = None
    model_path: Optional[str] = None
    openai: Optional[OpenAIPatch] = None


class DigestPatch(BaseModel):
    enabled: Optional[bool] = None
    scan_dirs: Optional[List[str]] = None
    history_hours: Optional[float] = None
    refresh_hours: Optional[float] = None
    notify_time: Optional[str] = None
    enabled_collectors: Optional[List[str]] = None


class PttPatch(BaseModel):
    enabled: Optional[bool] = None
    hotkey: Optional[str] = None
    language: Optional[str] = None


class RequestHistoryPatch(BaseModel):
    enabled: Optional[bool] = None
    capture_full_body: Optional[bool] = None
    retention_days: Optional[int] = None
    max_total_mb: Optional[int] = None
    compress_after_days: Optional[int] = None
    cleanup_on_startup: Optional[bool] = None


class ConfigPatch(BaseModel):
    provider: Optional[ProviderPatch] = None
    whisper_model: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    log_level: Optional[str] = None
    digest: Optional[DigestPatch] = None
    ptt: Optional[PttPatch] = None
    request_history: Optional[RequestHistoryPatch] = None
    system_prompts: Optional[Dict[str, str]] = None


# ── 辅助 ──────────────────────────────────────────────────────────────────────

def _read_user_config() -> dict:
    """读取 ~/.lumina/config.json，不存在则读取包内 config.json。"""
    if _USER_CONFIG_PATH.exists():
        with open(_USER_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    pkg = Path(__file__).parent.parent.parent / "config.json"
    if pkg.exists():
        with open(pkg, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _write_user_config_atomic(data: dict) -> None:
    """原子写回 ~/.lumina/config.json（临时文件 + rename）。"""
    _USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _USER_CONFIG_PATH.with_name(f"config.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(_USER_CONFIG_PATH)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _deep_set(base: dict, override: dict) -> dict:
    """深度合并：override 存在的 key 覆盖 base，不删除 base 其余 key。"""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_set(result[k], v)
        else:
            result[k] = v
    return result


# ── 路由 ──────────────────────────────────────────────────────────────────────

@router.get("/v1/config")
async def get_config_api():
    """返回当前运行时配置（含环境变量覆盖后的值）。"""
    from lumina.config import get_config
    cfg = get_config()
    return {
        "provider": {
            "type": cfg.provider.type,
            "model_path": cfg.provider.model_path,
            "openai": {
                "base_url": cfg.provider.openai.base_url,
                "api_key": cfg.provider.openai.api_key,
                "model": cfg.provider.openai.model,
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
        },
        "ptt": {
            "enabled": cfg.ptt.enabled,
            "hotkey": cfg.ptt.hotkey,
            "language": cfg.ptt.language,
        },
        "request_history": {
            "enabled": cfg.request_history.enabled,
            "capture_full_body": cfg.request_history.capture_full_body,
            "retention_days": cfg.request_history.retention_days,
            "max_total_mb": cfg.request_history.max_total_mb,
            "compress_after_days": cfg.request_history.compress_after_days,
            "cleanup_on_startup": cfg.request_history.cleanup_on_startup,
        },
        "system_prompts": dict(cfg.system_prompts),
    }


@router.patch("/v1/config")
async def patch_config_api(patch: ConfigPatch, request: Request):
    """
    部分更新配置，写回 ~/.lumina/config.json。

    可热重载（立即生效）：digest、system_prompts
    需要重启：provider、whisper_model、host、port、log_level、ptt
      （ptt 通过文件 mtime 监听，约 1s 内自动重载）
    """
    restart_required = False

    async with _write_lock:
        data = await asyncio.to_thread(_read_user_config)

        # ── provider ─────────────────────────────────────────────────────────
        if patch.provider is not None:
            p = patch.provider
            prov = data.get("provider", {})
            if not isinstance(prov, dict):
                prov = {}
            if p.type is not None:
                prov["type"] = p.type
                restart_required = True
            if p.model_path is not None:
                prov["model_path"] = p.model_path
                restart_required = True
            if p.openai is not None:
                oa = prov.get("openai", {})
                if not isinstance(oa, dict):
                    oa = {}
                if p.openai.base_url is not None:
                    oa["base_url"] = p.openai.base_url
                if p.openai.api_key is not None:
                    oa["api_key"] = p.openai.api_key
                if p.openai.model is not None:
                    oa["model"] = p.openai.model
                prov["openai"] = oa
                restart_required = True
            data["provider"] = prov

        # ── whisper_model ─────────────────────────────────────────────────────
        if patch.whisper_model is not None:
            data["whisper_model"] = patch.whisper_model
            restart_required = True

        # ── host / port / log_level ───────────────────────────────────────────
        if patch.host is not None:
            data["host"] = patch.host
            restart_required = True
        if patch.port is not None:
            data["port"] = patch.port
            restart_required = True
        if patch.log_level is not None:
            data["log_level"] = patch.log_level
            restart_required = True

        # ── digest ────────────────────────────────────────────────────────────
        if patch.digest is not None:
            d = patch.digest
            dc = data.get("digest", {})
            if not isinstance(dc, dict):
                dc = {}
            if d.enabled is not None:
                dc["enabled"] = d.enabled
            if d.scan_dirs is not None:
                dc["scan_dirs"] = d.scan_dirs
            if d.history_hours is not None:
                dc["history_hours"] = d.history_hours
            if d.refresh_hours is not None:
                dc["refresh_hours"] = d.refresh_hours
            if d.notify_time is not None:
                dc["notify_time"] = d.notify_time
            if d.enabled_collectors is not None:
                dc["enabled_collectors"] = d.enabled_collectors
            data["digest"] = dc

        # ── ptt ───────────────────────────────────────────────────────────────
        if patch.ptt is not None:
            p = patch.ptt
            pc = data.get("ptt", {})
            if not isinstance(pc, dict):
                pc = {}
            if p.enabled is not None:
                pc["enabled"] = p.enabled
            if p.hotkey is not None:
                pc["hotkey"] = p.hotkey
            if p.language is not None:
                pc["language"] = p.language
            data["ptt"] = pc
            # PTT 通过文件 mtime watcher 自动重载，不需要标 restart_required

        # ── request_history ────────────────────────────────────────────────────
        if patch.request_history is not None:
            rh = patch.request_history
            rc = data.get("request_history", {})
            if not isinstance(rc, dict):
                rc = {}
            if rh.enabled is not None:
                rc["enabled"] = rh.enabled
            if rh.capture_full_body is not None:
                rc["capture_full_body"] = rh.capture_full_body
            if rh.retention_days is not None:
                rc["retention_days"] = rh.retention_days
            if rh.max_total_mb is not None:
                rc["max_total_mb"] = rh.max_total_mb
            if rh.compress_after_days is not None:
                rc["compress_after_days"] = rh.compress_after_days
            if rh.cleanup_on_startup is not None:
                rc["cleanup_on_startup"] = rh.cleanup_on_startup
            data["request_history"] = rc

        # ── system_prompts ────────────────────────────────────────────────────
        if patch.system_prompts is not None:
            sp = data.get("system_prompts", {})
            if not isinstance(sp, dict):
                sp = {}
            sp.update(patch.system_prompts)
            data["system_prompts"] = sp

        await asyncio.to_thread(_write_user_config_atomic, data)

    # ── 热重载 ────────────────────────────────────────────────────────────────

    # digest：重新初始化 DigestConfig 单例
    if patch.digest is not None:
        try:
            from lumina.digest.config import configure as _digest_configure
            _digest_configure(data)
            logger.info("Config: digest config hot-reloaded")
        except Exception as e:
            logger.warning("Config: digest hot-reload failed: %s", e)

    # system_prompts：原地 mutate LLMEngine._system_prompts
    if patch.system_prompts is not None:
        try:
            llm = request.app.state.llm
            llm._system_prompts.update(patch.system_prompts)
            logger.info("Config: system_prompts hot-reloaded (%d keys)", len(patch.system_prompts))
        except Exception as e:
            logger.warning("Config: system_prompts hot-reload failed: %s", e)

    if patch.request_history is not None:
        try:
            from lumina import request_history as _request_history
            from lumina.config import get_config

            _request_history.configure({"request_history": data.get("request_history", {})})
            cfg = get_config()
            cfg.request_history.enabled = bool(data["request_history"].get("enabled", True))
            cfg.request_history.capture_full_body = bool(
                data["request_history"].get("capture_full_body", True)
            )
            cfg.request_history.retention_days = max(
                0,
                int(data["request_history"].get("retention_days", 14)),
            )
            cfg.request_history.max_total_mb = max(
                1,
                int(data["request_history"].get("max_total_mb", 512)),
            )
            cfg.request_history.compress_after_days = max(
                0,
                int(data["request_history"].get("compress_after_days", 1)),
            )
            cfg.request_history.cleanup_on_startup = bool(
                data["request_history"].get("cleanup_on_startup", True)
            )
            logger.info("Config: request_history hot-reloaded")
        except Exception as e:
            logger.warning("Config: request_history hot-reload failed: %s", e)

    return {"ok": True, "restart_required": restart_required}


@router.post("/v1/config/request_history/prune")
async def prune_request_history_api():
    from lumina import request_history as _request_history

    stats = await asyncio.to_thread(_request_history.prune_now)
    return {"ok": True, "stats": stats}
