"""
lumina/api/routers/config.py — 配置读取与更新接口

GET  /v1/config  — 返回当前运行时配置（合并 config.json + 环境变量后的值）
PATCH /v1/config — 部分更新配置，写回当前活动配置文件；可热重载字段立即生效
"""
import asyncio
import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

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

class OpenAIPatch(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None


class SamplingPatch(BaseModel):
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    min_p: Optional[float] = None
    presence_penalty: Optional[float] = None
    repetition_penalty: Optional[float] = None
    max_tokens: Optional[int] = None


class ProviderPatch(BaseModel):
    type: Optional[str] = None
    model_path: Optional[str] = None
    sampling: Optional[SamplingPatch] = None
    openai: Optional[OpenAIPatch] = None
    llama_cpp: Optional[dict] = None


class DigestPatch(BaseModel):
    enabled: Optional[bool] = None
    scan_dirs: Optional[List[str]] = None
    history_hours: Optional[float] = None
    refresh_hours: Optional[float] = None
    notify_time: Optional[str] = None
    enabled_collectors: Optional[List[str]] = None
    weekly_report_day: Optional[int] = None
    monthly_report_day: Optional[int] = None
    ai_queries_max_source_chars: Optional[int] = None


class PttPatch(BaseModel):
    enabled: Optional[bool] = None
    hotkey: Optional[str] = None
    language: Optional[str] = None


class DesktopPatch(BaseModel):
    menubar_enabled: Optional[bool] = None


class RequestHistoryPatch(BaseModel):
    enabled: Optional[bool] = None
    capture_full_body: Optional[bool] = None
    retention_days: Optional[int] = None
    max_total_mb: Optional[int] = None
    compress_after_days: Optional[int] = None
    cleanup_on_startup: Optional[bool] = None


class BrandingPatch(BaseModel):
    username: Optional[str] = None


class UIHomePatch(BaseModel):
    enabled_tabs: Optional[List[str]] = None
    digest_enabled: Optional[bool] = None
    document_enabled: Optional[bool] = None
    image_enabled: Optional[bool] = None
    audio_enabled: Optional[bool] = None
    image_modules: Optional[List[str]] = None
    allow_local_override: Optional[bool] = None


class UIPatch(BaseModel):
    home: Optional[UIHomePatch] = None


class ConfigPatch(BaseModel):
    provider: Optional[ProviderPatch] = None
    whisper_model: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    log_level: Optional[str] = None
    digest: Optional[DigestPatch] = None
    ptt: Optional[PttPatch] = None
    desktop: Optional[DesktopPatch] = None
    request_history: Optional[RequestHistoryPatch] = None
    branding: Optional[BrandingPatch] = None
    ui: Optional[UIPatch] = None
    system_prompts: Optional[Dict[str, str]] = None


# ── 路由 ──────────────────────────────────────────────────────────────────────

@router.get("/v1/config")
async def get_config_api():
    """返回当前运行时配置（含环境变量覆盖后的值）。"""
    from lumina.config import get_config
    return serialize_runtime_config(get_config())


@router.patch("/v1/config")
async def patch_config_api(patch: ConfigPatch, request: Request):
    """
    部分更新配置，写回 ~/.lumina/config.json。

    可热重载（立即生效）：digest、system_prompts
    需要重启：provider、whisper_model、host、port、log_level、desktop
      （ptt 通过文件 mtime 监听，约 1s 内自动重载）
    """
    restart_required = False

    async with _write_lock:
        data = await asyncio.to_thread(read_mutable_config_data)

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
            if p.sampling is not None:
                sc = prov.get("sampling", {})
                if not isinstance(sc, dict):
                    sc = {}
                for field in ("temperature", "top_p", "min_p", "presence_penalty", "repetition_penalty"):
                    val = getattr(p.sampling, field)
                    if val is not None:
                        sc[field] = val
                for field in ("top_k", "max_tokens"):
                    val = getattr(p.sampling, field)
                    if val is not None:
                        sc[field] = val
                prov["sampling"] = sc
                # sampling 参数热重载：更新 config singleton 即可，无需重启
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
            if p.llama_cpp is not None:
                lc = prov.get("llama_cpp", {})
                if not isinstance(lc, dict):
                    lc = {}
                for field in ("model_path", "n_gpu_layers", "n_ctx"):
                    val = p.llama_cpp.get(field)
                    if val is not None:
                        lc[field] = val
                        restart_required = True
                prov["llama_cpp"] = lc
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
            if d.weekly_report_day is not None:
                dc["weekly_report_day"] = max(0, min(6, int(d.weekly_report_day)))
            if d.monthly_report_day is not None:
                dc["monthly_report_day"] = max(1, min(28, int(d.monthly_report_day)))
            if d.ai_queries_max_source_chars is not None:
                dc["ai_queries_max_source_chars"] = max(1, int(d.ai_queries_max_source_chars))
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

        if patch.desktop is not None:
            desktop = data.get("desktop", {})
            if not isinstance(desktop, dict):
                desktop = {}
            if patch.desktop.menubar_enabled is not None:
                desktop["menubar_enabled"] = patch.desktop.menubar_enabled
                restart_required = True
            data["desktop"] = desktop

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

        # ── branding ───────────────────────────────────────────────────────────
        if patch.branding is not None:
            branding = data.get("branding", {})
            if not isinstance(branding, dict):
                branding = {}
            if patch.branding.username is not None:
                branding["username"] = patch.branding.username.strip()
            data["branding"] = branding

        # ── ui ────────────────────────────────────────────────────────────────
        if patch.ui is not None and patch.ui.home is not None:
            ui = data.get("ui", {})
            if not isinstance(ui, dict):
                ui = {}
            home = ui.get("home", {})
            if not isinstance(home, dict):
                home = {}
            if patch.ui.home.enabled_tabs is not None:
                from lumina.config import normalize_home_tabs

                home["enabled_tabs"] = normalize_home_tabs(patch.ui.home.enabled_tabs)
            if patch.ui.home.digest_enabled is not None:
                home["digest_enabled"] = patch.ui.home.digest_enabled
            if patch.ui.home.document_enabled is not None:
                home["document_enabled"] = patch.ui.home.document_enabled
            if patch.ui.home.image_enabled is not None:
                home["image_enabled"] = patch.ui.home.image_enabled
            if patch.ui.home.audio_enabled is not None:
                home["audio_enabled"] = patch.ui.home.audio_enabled
            if patch.ui.home.image_modules is not None:
                from lumina.config import normalize_image_modules

                home["image_modules"] = normalize_image_modules(patch.ui.home.image_modules)
            if patch.ui.home.allow_local_override is not None:
                home["allow_local_override"] = patch.ui.home.allow_local_override
            ui["home"] = home
            data["ui"] = ui

        # ── system_prompts ────────────────────────────────────────────────────
        if patch.system_prompts is not None:
            sp = data.get("system_prompts", {})
            if not isinstance(sp, dict):
                sp = {}
            sp.update(patch.system_prompts)
            data["system_prompts"] = sp

        await asyncio.to_thread(write_config_atomic, data)

    # ── 热重载 ────────────────────────────────────────────────────────────────
    from lumina.config import get_config

    cfg = get_config()

    # digest：重新初始化 DigestConfig 单例
    if patch.digest is not None:
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

    # system_prompts：原地 mutate LLMEngine._system_prompts
    if patch.system_prompts is not None:
        try:
            from lumina.services.audio.transcriber import set_asr_prompts as _set_asr_prompts

            llm = request.app.state.llm
            llm._system_prompts.update(patch.system_prompts)
            cfg.system_prompts.update(patch.system_prompts)
            _set_asr_prompts(
                zh=cfg.system_prompts.get("asr_zh", ""),
                en=cfg.system_prompts.get("asr_en", ""),
            )
            logger.info("Config: system_prompts hot-reloaded (%d keys)", len(patch.system_prompts))
        except Exception as e:
            logger.warning("Config: system_prompts hot-reload failed: %s", e)

    # provider.sampling：热重载 config singleton 中的 sampling 字段
    if patch.provider is not None and patch.provider.sampling is not None:
        try:
            update_runtime_config(cfg, data, sections={"provider_sampling"})
            logger.info("Config: provider.sampling hot-reloaded")
        except Exception as e:
            logger.warning("Config: provider.sampling hot-reload failed: %s", e)

    if patch.request_history is not None:
        try:
            from lumina.engine import request_history as _request_history

            _request_history.configure({"request_history": data.get("request_history", {})})
            update_runtime_config(cfg, data, sections={"request_history"})
            logger.info("Config: request_history hot-reloaded")
        except Exception as e:
            logger.warning("Config: request_history hot-reload failed: %s", e)

    if patch.branding is not None:
        try:
            update_runtime_config(cfg, data, sections={"branding"})
            logger.info("Config: branding hot-reloaded")
        except Exception as e:
            logger.warning("Config: branding hot-reload failed: %s", e)

    if patch.ui is not None and patch.ui.home is not None:
        try:
            update_runtime_config(cfg, data, sections={"ui"})
            logger.info("Config: ui.home hot-reloaded")
        except Exception as e:
            logger.warning("Config: ui.home hot-reload failed: %s", e)

    if patch.desktop is not None:
        try:
            update_runtime_config(cfg, data, sections={"desktop"})
            logger.info("Config: desktop hot-reloaded")
        except Exception as e:
            logger.warning("Config: desktop hot-reload failed: %s", e)

    return {"ok": True, "restart_required": restart_required}


@router.post("/v1/config/request_history/prune")
async def prune_request_history_api():
    from lumina.engine import request_history as _request_history

    stats = await asyncio.to_thread(_request_history.prune_now)
    return {"ok": True, "stats": stats}
