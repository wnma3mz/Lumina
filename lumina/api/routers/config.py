"""
lumina/api/routers/config.py — 配置读取与更新接口

GET  /v1/config  — 返回当前运行时配置（合并 config.json + 环境变量后的值）
PATCH /v1/config — 部分更新配置，写回当前活动配置文件；可热重载字段立即生效
"""
import asyncio
import logging

from fastapi import APIRouter, Request
from lumina.config import Config

from lumina.config_runtime import (
    read_mutable_config_data,
    serialize_runtime_config,
    update_runtime_config,
    write_config_atomic,
)

logger = logging.getLogger("lumina")

router = APIRouter(tags=["config"])

# 防止并发写
_write_lock = asyncio.Lock()

# ── Pydantic 请求体 ────────────────────────────────────────────────────────────

# ── 路由 ──────────────────────────────────────────────────────────────────────

@router.get("/v1/config")
async def get_config_api():
    """返回当前运行时配置（含环境变量覆盖后的值）。"""
    from lumina.config import get_config
    return serialize_runtime_config(get_config())


@router.patch("/v1/config")
async def patch_config_api(patch: Config, request: Request):
    """
    部分更新配置，写回 ~/.lumina/config.json。
    """
    restart_required = False

    async with _write_lock:
        data = await asyncio.to_thread(read_mutable_config_data)

        patch_dict = patch.model_dump(exclude_unset=True)

        for sec, sec_data in patch_dict.items():
            if sec_data is None:
                continue
                
            current_sec = data.get(sec, {})
            if not isinstance(current_sec, dict):
                current_sec = {}
                
            for k, v in sec_data.items():
                if isinstance(v, dict):
                    # Special nested updates (e.g. system.server, audio.ptt, prompts, sampling)
                    current_nested = current_sec.get(k, {})
                    if not isinstance(current_nested, dict):
                        current_nested = {}
                    current_nested.update(v)
                    current_sec[k] = current_nested
                else:
                    current_sec[k] = v
                    
            data[sec] = current_sec

        # Type checks and normalizations
        if "vision" in data and "enabled_modules" in data["vision"]:
            from lumina.config import normalize_image_modules
            data["vision"]["enabled_modules"] = normalize_image_modules(data["vision"]["enabled_modules"])
            
        if "ui" in data and "home" in data["ui"] and "enabled_tabs" in data["ui"]["home"]:
            from lumina.config import normalize_home_tabs
            data["ui"]["home"]["enabled_tabs"] = normalize_home_tabs(data["ui"]["home"]["enabled_tabs"])

        if "system" in data and "branding" in data["system"] and "username" in data["system"]["branding"]:
            data["system"]["branding"]["username"] = data["system"]["branding"]["username"].strip()

        # Mark restart_required fields
        restart_keys = {
            "provider": {"type", "model_path", "openai", "llama_cpp",
                         "offload_embedding", "offload_vision", "offload_audio"},
            "system": {"server", "desktop"},
            "audio": {"whisper_model"}
        }
        
        for sec, fields in restart_keys.items():
            if sec in patch_dict and patch_dict[sec] is not None:
                if any(f in patch_dict[sec] for f in fields):
                    restart_required = True

        await asyncio.to_thread(write_config_atomic, data)

    # ── 热重载 ────────────────────────────────────────────────────────────────
    from lumina.config import get_config

    cfg = get_config()

    if "digest" in patch_dict:
        try:
            from lumina.services.digest.config import configure as _digest_configure
            _digest_configure(data)
            update_runtime_config(cfg, data, sections={"digest"})
            scheduler = getattr(request.app.state, "digest_scheduler", None)
            if scheduler is not None:
                scheduler.reload(run_startup=True)
            logger.info("Config: digest config hot-reloaded")
        except Exception as e:
            logger.warning("Config: digest hot-reload failed: %s", e)

    if "document" in patch_dict:
        try:
            update_runtime_config(cfg, data, sections={"document"})
            logger.info("Config: document config hot-reloaded")
        except Exception as e:
            logger.warning("Config: document hot-reload failed: %s", e)

    if "vision" in patch_dict:
        try:
            update_runtime_config(cfg, data, sections={"vision"})
            logger.info("Config: vision config hot-reloaded")
        except Exception as e:
            logger.warning("Config: vision hot-reload failed: %s", e)

    if "audio" in patch_dict:
        try:
            update_runtime_config(cfg, data, sections={"audio"})
            from lumina.services.audio.transcriber import set_asr_prompts as _set_asr_prompts
            _set_asr_prompts(
                zh=cfg.system_prompts.get("asr_zh", ""),
                en=cfg.system_prompts.get("asr_en", ""),
            )
            logger.info("Config: audio config hot-reloaded")
        except Exception as e:
            logger.warning("Config: audio hot-reload failed: %s", e)

    if "provider" in patch_dict:
        try:
            update_runtime_config(cfg, data, sections={"provider"})
            logger.info("Config: provider config hot-reloaded")
        except Exception as e:
            logger.warning("Config: provider hot-reload failed: %s", e)
            
    # update LLMEngine system prompts after any domain prompts change
    try:
        llm = request.app.state.llm
        llm._system_prompts.clear()
        llm._system_prompts.update(cfg.system_prompts)
    except Exception as e:
        logger.warning("Config: failed to update LLM system_prompts: %s", e)

    if "provider" in patch_dict and "sampling" in patch_dict["provider"]:
        try:
            update_runtime_config(cfg, data, sections={"provider_sampling"})
            logger.info("Config: provider.sampling hot-reloaded")
        except Exception as e:
            logger.warning("Config: provider.sampling hot-reload failed: %s", e)

    if "system" in patch_dict:
        try:
            if "request_history" in patch_dict["system"]:
                from lumina.engine import request_history as _request_history
                _request_history.configure({"request_history": data.get("system", {}).get("request_history", {})})
            update_runtime_config(cfg, data, sections={"system"})
            logger.info("Config: system hot-reloaded")
        except Exception as e:
            logger.warning("Config: system hot-reload failed: %s", e)

    if "ui" in patch_dict and "home" in patch_dict["ui"]:
        try:
            update_runtime_config(cfg, data, sections={"ui"})
            logger.info("Config: ui.home hot-reloaded")
        except Exception as e:
            logger.warning("Config: ui.home hot-reload failed: %s", e)

    return {"ok": True, "restart_required": restart_required}


@router.post("/v1/config/request_history/prune")
async def prune_request_history_api():
    from lumina.engine import request_history as _request_history

    stats = await asyncio.to_thread(_request_history.prune_now)
    return {"ok": True, "stats": stats}
