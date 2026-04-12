"""
lumina/api/routers/digest.py — Digest / Daily Dashboard 路由
"""
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import Response

router = APIRouter(prefix="/v1/digest", tags=["digest"])


@router.get("")
async def get_digest_api(raw: Request):
    from lumina.digest import load_digest, get_status
    status = get_status()
    return {
        "content": load_digest(),
        "generating": status["generating"],
        "generated_at": status["generated_at"],
        "server_start": raw.app.state.server_start_time,
    }


@router.post("/refresh")
async def refresh_digest_api(background_tasks: BackgroundTasks, raw: Request):
    from lumina.digest import maybe_generate_digest

    llm = raw.app.state.llm

    async def _run():
        await maybe_generate_digest(llm, force_full=True)

    background_tasks.add_task(_run)
    return {"status": "refreshing"}


@router.get("/debug")
async def digest_debug_api():
    """本地调试接口（非面向最终用户）：返回上次采集的缓存数据，立即返回，不触发新的采集。

    响应包含 scan_dirs、collector 详情、cursor 时间戳及 md_files 路径列表，
    属于本机敏感信息。仅供本地诊断使用；若 Lumina 绑定 0.0.0.0 对局域网开放，
    需自行评估此接口的暴露风险（考虑添加鉴权或在生产环境禁用）。
    """
    from lumina.digest.core import get_debug_info
    return get_debug_info()


@router.get("/export")
async def digest_export_api():
    """下载完整 digest.md 文件。"""
    from lumina.digest import load_digest
    content = load_digest() or ""
    filename = f"lumina-digest-{datetime.now().strftime('%Y%m%d')}.md"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
