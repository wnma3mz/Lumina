"""
lumina/api/routers/fragments.py — HTMX HTML 片段路由

为前端 HTMX 请求返回可直接插入 DOM 的 HTML 片段，使用 Jinja2 模板渲染。

端点：
  GET /fragments/digest              日报内容区（时间轴 + Hero Card）
  GET /fragments/digest/sources      数据来源图标行
  GET /fragments/pdf/status/{job_id} 翻译任务进度（完成后自动停止轮询）
  GET /fragments/config              设置表单（含当前配置值）
  GET /fragments/report/{type}/{key} 日报/周报/月报内容
"""
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from lumina.api.rendering import render_markdown_html
from lumina.api.ui_meta import collector_sources, digest_icon_for_text, system_prompt_items

router = APIRouter(prefix="/fragments", tags=["fragments"])

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
_SNAPSHOT_SECTION_LIMIT = 20


def _parse_sections(content: str) -> list[dict]:
    """将 digest.md 内容按 '---' 分隔解析为 section 列表。"""
    raw_sections = [s.strip() for s in content.split("\n---\n") if s.strip()]
    sections = []
    for i, s in enumerate(raw_sections):
        # 提取标题
        m = re.search(r"^#\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", s, re.MULTILINE)
        if m:
            title = m.group(1)
            s = s.replace(m.group(0), "", 1).lstrip()
        else:
            first_line = s.split("\n")[0]
            title = re.sub(r"^#+\s*", "", first_line).strip() or f"条目 {i + 1}"
            s = s.replace(first_line, "", 1).lstrip()
        # 来源图标
        icon, filter_key = digest_icon_for_text(f"{title} {s}")
        # 去掉 HTML 注释再渲染 Markdown
        cleaned = re.sub(r"<!--.*?-->", "", s, flags=re.DOTALL)
        html_body = _render_markdown(cleaned)
        sections.append(
            {
                "title": title,
                "html": html_body,
                "icon": icon,
                "filter_key": filter_key,
                "open": i == 0,
            }
        )
    return sections


def _system_prompt_items(prompts: Optional[dict]) -> list[dict[str, str]]:
    return system_prompt_items(prompts)


def _render_markdown(content: str) -> str:
    return render_markdown_html(content)


def _load_recent_snapshot_content(now: Optional[datetime] = None) -> str:
    from lumina.services.digest.reports import load_snapshots_for_date

    target_day = (now or datetime.now()).date()
    snapshots = [item.strip() for item in load_snapshots_for_date(target_day) if item.strip()]
    if not snapshots:
        return ""
    recent = list(reversed(snapshots[-_SNAPSHOT_SECTION_LIMIT:]))
    return "\n\n---\n\n".join(recent)


def _format_generated_at_label(generated_at) -> str:
    if not generated_at:
        return "尚未生成"

    try:
        if isinstance(generated_at, (int, float)):
            d = datetime.fromtimestamp(generated_at)
        else:
            generated_text = str(generated_at).strip()
            if re.fullmatch(r"\d+(?:\.\d+)?", generated_text):
                d = datetime.fromtimestamp(float(generated_text))
            else:
                d = datetime.fromisoformat(generated_text)
        return "生成于 " + d.strftime("%-m月%-d日 %H:%M")
    except Exception:
        return "已生成"


def _format_chars_label(chars: int) -> str:
    value = max(0, int(chars))
    if value >= 10000:
        text = f"{value / 10000:.1f}".rstrip("0").rstrip(".")
        return f"{text} 万字符"
    return f"{value:,} 字符"


def _report_key_label(report_type: str, key: str) -> str:
    try:
        if report_type == "daily":
            d = datetime.strptime(key, "%Y-%m-%d")
            return f"{d.year}年{d.month}月{d.day}日"
        if report_type == "weekly":
            year, week = key.split("-W")
            return f"{int(year)} 第{int(week)}周"
        if report_type == "monthly":
            d = datetime.strptime(key, "%Y-%m")
            return f"{d.year}年{d.month}月"
    except ValueError:
        return key
    return key


def _report_date_header(report_type: str, key: str) -> str:
    label = {"daily": "日报", "weekly": "周报", "monthly": "月报"}[report_type]
    try:
        if report_type == "daily":
            d = datetime.strptime(key, "%Y-%m-%d")
            return f"{d.year}年{d.month}月{d.day}日 {label}"
        if report_type == "weekly":
            year, week = key.split("-W")
            return f"{int(year)} 第{int(week)}周 {label}"
        if report_type == "monthly":
            d = datetime.strptime(key, "%Y-%m")
            return f"{d.year}年{d.month}月 {label}"
    except ValueError:
        return f"{key} {label}"
    return f"{key} {label}"


def _load_report_fragment_context(report_type: str, key: str) -> dict:
    from lumina.config import REPORTS_DAILY_DIR, REPORTS_WEEKLY_DIR, REPORTS_MONTHLY_DIR

    dirs = {
        "daily": REPORTS_DAILY_DIR,
        "weekly": REPORTS_WEEKLY_DIR,
        "monthly": REPORTS_MONTHLY_DIR,
    }
    report_dir = dirs[report_type]
    report_keys = sorted(
        [path.stem for path in report_dir.glob("*.md")],
        reverse=True,
    ) if report_dir.is_dir() else []

    if not report_keys:
        return {
            "html_content": "",
            "date_header": "",
            "selected_key": "",
            "report_type": report_type,
            "report_options": [],
            "empty": True,
        }

    selected_key = report_keys[0] if key == "latest" else key
    report_path = report_dir / f"{selected_key}.md"
    if not report_path.exists():
        raise HTTPException(404, "Report not found")

    content = report_path.read_text(encoding="utf-8")
    return {
        "html_content": _render_markdown(content),
        "date_header": _report_date_header(report_type, selected_key),
        "selected_key": selected_key,
        "report_type": report_type,
        "report_options": [
            {"key": item, "label": _report_key_label(report_type, item)}
            for item in report_keys
        ],
        "empty": False,
    }


# ── Digest 内容区 ──────────────────────────────────────────────────────────────

@router.get("/digest", response_class=HTMLResponse)
async def fragment_digest(request: Request):
    """返回日报时间轴 HTML 片段，包含 Hero Card 和各 section。"""
    from lumina.services.digest import get_status

    status = get_status()
    content = _load_recent_snapshot_content()

    generating = status.get("generating", False)
    time_label = _format_generated_at_label(status.get("generated_at"))

    sections = _parse_sections(content) if content and not generating else []

    # Hero text：取第一个 section 第一个 <p> 文字
    hero_text = ""
    if sections:
        m = re.search(r"<p>(.*?)</p>", sections[0]["html"])
        if m:
            hero_text = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    return templates.TemplateResponse(
        "digest_content.html",
        {
            "request": request,
            "generating": generating,
            "time_label": time_label,
            "sections": sections,
            "hero_text": hero_text,
        },
    )


# ── 数据来源图标行 ──────────────────────────────────────────────────────────────

@router.get("/digest/storage", response_class=HTMLResponse)
async def fragment_digest_storage(request: Request):
    """返回动态的存储空间占用卡片 HTML 片段。"""
    from lumina.config import get_config
    from lumina.engine import request_history
    
    recorder = request_history.get_recorder()
    with recorder._lock:
        total_bytes = recorder._total_bytes_locked()
        
    cfg = get_config()
    max_mb = cfg.request_history.max_total_mb
    used_mb = total_bytes / (1024 * 1024)
    pct = min(100, int((used_mb / max_mb) * 100)) if max_mb > 0 else 0
    
    html = f"""
          <div class="flex justify-between items-end mb-3">
              <div>
                  <p class="text-[10px] text-zinc-400 uppercase tracking-widest font-extrabold">本地存储空间</p>
                  <p class="text-xl font-black mt-1">{used_mb:.1f} <span class="text-xs font-normal text-zinc-500">/ {max_mb} MB</span></p>
              </div>
              <button class="text-indigo-500 text-xs font-bold hover:underline" onclick="pruneRequestHistory(this)">立即清理</button>
          </div>
          <div class="w-full h-3 bg-zinc-100 dark:bg-zinc-800 rounded-full overflow-hidden p-[2px]">
              <div class="h-full bg-gradient-to-r from-indigo-500 to-purple-500 rounded-full transition-all duration-1000" style="width: {pct}%;"></div>
          </div>
    """
    return HTMLResponse(html)

@router.get("/digest/sources", response_class=HTMLResponse)
async def fragment_digest_sources(request: Request):
    """返回数据来源图标行 HTML 片段。"""
    from lumina.services.digest.core import get_debug_info

    debug = get_debug_info()
    sources = collector_sources(debug.get("collectors", {}))
    active_sources = [item for item in sources if item.get("active")]
    total_chars = sum(int(item.get("chars", 0) or 0) for item in sources)
    top_sources = sorted(active_sources, key=lambda item: int(item.get("chars", 0) or 0), reverse=True)[:3]

    return templates.TemplateResponse(
        "digest_sources.html",
        {
            "request": request,
            "sources": sources,
            "active_count": len(active_sources),
            "total_count": len(sources),
            "total_chars_label": _format_chars_label(total_chars),
            "top_sources": top_sources,
        },
    )


# ── PDF 翻译任务状态 ────────────────────────────────────────────────────────────

@router.get("/pdf/status/{job_id}", response_class=HTMLResponse)
async def fragment_pdf_status(job_id: str, request: Request):
    """返回 PDF 翻译进度 HTML 片段。

    任务进行中：返回带 hx-trigger="every 2s" 的进度条（HTMX 继续轮询）。
    任务完成：返回下载按钮（不含 hx-trigger，轮询自动停止）。
    """
    manager = request.app.state.pdf_manager
    job = manager.get_status(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    status = job["status"]
    progress = job.get("progress", 0)

    if status == "done":
        mono_url = f"/v1/pdf/download/{job_id}/mono"
        dual_url = f"/v1/pdf/download/{job_id}/dual"
        return templates.TemplateResponse(
            "pdf_result.html",
            {
                "request": request,
                "job_id": job_id,
                "mono_url": mono_url,
                "dual_url": dual_url,
            },
        )
    elif status == "error":
        error_msg = job.get("error", "未知错误")
        return templates.TemplateResponse(
            "pdf_error.html",
            {"request": request, "error": error_msg},
        )
    else:
        # running / pending：返回带轮询的进度条
        return templates.TemplateResponse(
            "pdf_progress.html",
            {
                "request": request,
                "job_id": job_id,
                "progress": progress,
                "status_text": "翻译中…" if status == "running" else "等待中…",
            },
        )


# ── 设置表单 ────────────────────────────────────────────────────────────────────

@router.get("/config", response_class=HTMLResponse)
async def fragment_config(request: Request):
    """返回设置表单 HTML 片段（含当前配置值）。"""
    from lumina.config import get_config
    from lumina.api.ui_meta import HOME_TAB_DEFS, IMAGE_TASK_DEFS

    cfg = get_config()
    return templates.TemplateResponse(
        "config_form.html",
        {
            "request": request,
            "cfg": cfg,
            "system_prompt_items": _system_prompt_items(getattr(cfg, "system_prompts", {})),
            "home_tab_defs": HOME_TAB_DEFS,
            "image_task_defs": IMAGE_TASK_DEFS,
        },
    )


# ── 报告内容 ────────────────────────────────────────────────────────────────────

@router.get("/report/{report_type}", response_class=HTMLResponse)
async def fragment_report(report_type: str, request: Request, key: str = "latest"):
    """返回指定报告的 HTML 内容片段，默认显示最新一期。"""
    if report_type not in ("daily", "weekly", "monthly"):
        raise HTTPException(400, "Invalid report type")
    context = _load_report_fragment_context(report_type, key)
    return templates.TemplateResponse(
        "report_content.html",
        {
            "request": request,
            **context,
        },
    )


@router.get("/report/{report_type}/{key}", response_class=HTMLResponse)
async def fragment_report_legacy(report_type: str, key: str, request: Request):
    """兼容旧路径：/fragments/report/{type}/{key}。"""
    return await fragment_report(report_type=report_type, request=request, key=key)
