"""
lumina/api/routers/digest.py — Digest / Daily Dashboard 路由

活动摘要端点：
  GET  /v1/digest           当前摘要内容 + 状态
  POST /v1/digest/refresh   触发重新生成
  GET  /v1/digest/debug     采集调试信息
  GET  /v1/digest/export    下载 digest.md

报告端点：
  GET  /v1/digest/reports/{type}        列出已生成的报告 key
  GET  /v1/digest/reports/{type}/{key}  读取指定报告
  POST /v1/digest/reports/{type}/{key}  触发生成指定报告（后台执行）
"""
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

router = APIRouter(prefix="/v1/digest", tags=["digest"])


@router.get("")
async def get_digest_api(raw: Request):
    from lumina.services.digest import load_digest, get_status
    status = get_status()
    return {
        "content": load_digest(),
        "generating": status["generating"],
        "generated_at": status["generated_at"],
        "server_start": raw.app.state.server_start_time,
    }


@router.post("/refresh")
async def refresh_digest_api(background_tasks: BackgroundTasks, raw: Request):
    from lumina.services.digest import maybe_generate_digest

    llm = raw.app.state.llm

    async def _run():
        await maybe_generate_digest(llm, force_full=True)

    background_tasks.add_task(_run)
    return HTMLResponse('<span class="text-indigo-500 font-bold text-sm">⏳ 已触发，稍后自动刷新…</span>')


@router.get("/debug")
async def digest_debug_api():
    """本地调试接口（非面向最终用户）：返回上次采集的缓存数据，立即返回，不触发新的采集。

    响应包含 scan_dirs、collector 详情、cursor 时间戳及 md_files 路径列表，
    属于本机敏感信息。仅供本地诊断使用；若 Lumina 绑定 0.0.0.0 对局域网开放，
    需自行评估此接口的暴露风险（考虑添加鉴权或在生产环境禁用）。
    """
    from lumina.services.digest.core import get_debug_info
    return get_debug_info()


@router.get("/export")
async def digest_export_api():
    """下载完整 digest.md 文件。"""
    from lumina.services.digest import load_digest
    content = load_digest() or ""
    filename = f"lumina-digest-{datetime.now().strftime('%Y%m%d')}.md"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── 报告端点 ──────────────────────────────────────────────────────────────────

_VALID_TYPES = {"daily", "weekly", "monthly"}


@router.get("/reports/{report_type}")
async def list_reports_api(report_type: str):
    """列出已生成的报告 key（降序）。"""
    if report_type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid report_type: {report_type}")
    from lumina.services.digest.reports import list_report_keys, daily_key, weekly_key, monthly_key
    from datetime import date
    keys = list_report_keys(report_type)
    key_fn = {"daily": daily_key, "weekly": weekly_key, "monthly": monthly_key}[report_type]
    return {"keys": keys, "current_key": key_fn(date.today())}


@router.get("/reports/{report_type}/{key}")
async def get_report_api(report_type: str, key: str):
    """读取指定报告内容（不触发生成）。"""
    if report_type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid report_type: {report_type}")
    from lumina.services.digest.reports import load_report, adjacent_keys
    content = load_report(report_type, key)
    prev_key, next_key = adjacent_keys(report_type, key)
    return {
        "key": key,
        "content": content,
        "exists": content is not None,
        "prev_key": prev_key,
        "next_key": next_key,
    }


@router.post("/reports/{report_type}/{key}")
async def generate_report_api(report_type: str, key: str, background_tasks: BackgroundTasks, raw: Request):
    """触发生成指定报告（后台异步执行）。"""
    if report_type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid report_type: {report_type}")
    from lumina.services.digest.core import generate_report
    llm = raw.app.state.llm

    async def _run():
        await generate_report(llm, report_type, key)

    background_tasks.add_task(_run)
    return {"status": "generating", "report_type": report_type, "key": key}
