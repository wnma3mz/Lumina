"""
lumina/api/routers/config.py — 配置读取与更新接口

GET  /v1/config  — 返回当前运行时配置（合并 config.json + 环境变量后的值）
PATCH /v1/config — 部分更新配置，写回当前活动配置文件；可热重载字段立即生效
"""
import asyncio
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from lumina.config import (
    AudioConfig,
    DocumentConfig,
    ProviderConfig,
    SystemConfig,
    UIConfig,
    VisionConfig,
)
from lumina.config_apply import ConfigApplier
from lumina.services.digest.config import DigestConfig

from lumina.config_runtime import (
    ConfigStore,
    serialize_runtime_config,
)

logger = logging.getLogger("lumina")

router = APIRouter(tags=["config"])

# 防止并发写
_write_lock = asyncio.Lock()
_config_store = ConfigStore()
_config_applier = ConfigApplier()

# ── Pydantic 请求体 ────────────────────────────────────────────────────────────


class ConfigPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: ProviderConfig | None = None
    system: SystemConfig | None = None
    digest: DigestConfig | None = None
    document: DocumentConfig | None = None
    vision: VisionConfig | None = None
    audio: AudioConfig | None = None
    ui: UIConfig | None = None

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
    """
    from lumina.config import get_config

    async with _write_lock:
        patch_dict = patch.model_dump(exclude_unset=True)
        provider_patch = patch_dict.get("provider")
        if isinstance(provider_patch, dict):
            provider_patch.pop("backend", None)
        result = await asyncio.to_thread(_config_store.apply_patch, patch_dict, cfg=get_config())

    _config_applier.apply(
        request.app,
        old_cfg=result.old_cfg,
        new_cfg=result.new_cfg,
        patch_dict=result.patch_dict,
    )

    return {"ok": True, "restart_required": result.restart_required}


@router.post("/v1/config/request_history/prune")
async def prune_request_history_api():
    from lumina.engine import request_history as _request_history

    stats = await asyncio.to_thread(_request_history.prune_now)
    return {"ok": True, "stats": stats}
