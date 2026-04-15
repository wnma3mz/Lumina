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

import markdown as md
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/fragments", tags=["fragments"])

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ── 图标映射（与前端 ICON_MAP 保持一致） ─────────────────────────────────────
_ICON_MAP: dict[str, str] = {
    "shell": "🖥",
    "git": "📁",
    "clipboard": "📌",
    "browser": "🌐",
    "notes": "📝",
    "calendar": "📅",
    "markdown": "📄",
    "ai": "🤖",
}
_ICON_KEYS = list(_ICON_MAP.keys())

_PROMPT_LABELS: dict[str, str] = {
    "translate_to_zh": "翻译为中文",
    "translate_to_en": "翻译为英文",
    "summarize": "摘要",
    "polish_zh": "中文润色",
    "polish_en": "英文润色",
    "chat": "对话（默认）",
    "digest": "活动摘要",
    "daily_report": "日报",
    "weekly_report": "周报",
    "monthly_report": "月报",
    "asr_zh": "语音识别提示词（中文）",
    "asr_en": "语音识别提示词（英文）",
}
_PROMPT_ORDER = list(_PROMPT_LABELS.keys())


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
        lc = (title + " " + s).lower()
        filter_key: Optional[str] = next((k for k in _ICON_KEYS if k in lc), None)
        icon = _ICON_MAP.get(filter_key, "📋") if filter_key else "📋"
        # 去掉 HTML 注释再渲染 Markdown
        cleaned = re.sub(r"<!--.*?-->", "", s, flags=re.DOTALL)
        html_body = md.markdown(cleaned, extensions=["fenced_code", "tables"])
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
    if not isinstance(prompts, dict):
        return []
    keys = [k for k in prompts.keys() if isinstance(k, str) and not k.startswith("_")]
    ordered_keys = [k for k in _PROMPT_ORDER if k in keys] + [k for k in keys if k not in _PROMPT_ORDER]
    return [
        {
            "key": key,
            "label": _PROMPT_LABELS.get(key, key),
            "value": str(prompts.get(key, "")),
        }
        for key in ordered_keys
    ]


# ── Digest 内容区 ──────────────────────────────────────────────────────────────

@router.get("/digest", response_class=HTMLResponse)
async def fragment_digest(request: Request):
    """返回日报时间轴 HTML 片段，包含 Hero Card 和各 section。"""
    from lumina.digest import load_digest, get_status

    status = get_status()
    content = load_digest() or ""

    generating = status.get("generating", False)
    generated_at = status.get("generated_at")
    if generated_at:
        try:
            # generated_at 可能是 ISO 字符串或 Unix 时间戳
            if isinstance(generated_at, (int, float)):
                d = datetime.fromtimestamp(generated_at)
            else:
                d = datetime.fromisoformat(str(generated_at))
            time_label = "生成于 " + d.strftime("%-m月%-d日 %H:%M")
        except Exception:
            time_label = "已生成"
    else:
        time_label = "尚未生成"

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
    from lumina import request_history
    
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
    from lumina.digest.core import get_debug_info

    debug = get_debug_info()
    collectors = debug.get("collectors", {})
    names = {
        "collect_shell_history": "Shell",
        "collect_git_logs": "Git",
        "collect_clipboard": "剪贴板",
        "collect_browser_history": "浏览器",
        "collect_notes_app": "备忘录",
        "collect_calendar": "日历",
        "collect_markdown_notes": "Markdown",
        "collect_ai_queries": "AI",
    }
    icons = {
        "collect_shell_history": "🖥",
        "collect_git_logs": "📁",
        "collect_clipboard": "📌",
        "collect_browser_history": "🌐",
        "collect_notes_app": "📝",
        "collect_calendar": "📅",
        "collect_markdown_notes": "📄",
        "collect_ai_queries": "🤖",
    }
    sources = []
    for key, name in names.items():
        info = collectors.get(key, {})
        chars = info.get("chars", 0) if isinstance(info, dict) else 0
        active = chars > 0
        sources.append({"key": key, "name": name, "icon": icons[key], "active": active, "chars": chars})

    return templates.TemplateResponse(
        "digest_sources.html",
        {"request": request, "sources": sources},
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

    cfg = get_config()
    return templates.TemplateResponse(
        "config_form.html",
        {
            "request": request,
            "cfg": cfg,
            "system_prompt_items": _system_prompt_items(getattr(cfg, "system_prompts", {})),
        },
    )


# ── 报告内容 ────────────────────────────────────────────────────────────────────

@router.get("/report/{report_type}/{key}", response_class=HTMLResponse)
async def fragment_report(report_type: str, key: str, request: Request):
    """返回指定报告的 HTML 内容片段。"""
    if report_type not in ("daily", "weekly", "monthly"):
        raise HTTPException(400, "Invalid report type")

    from lumina.config import REPORTS_DAILY_DIR, REPORTS_WEEKLY_DIR, REPORTS_MONTHLY_DIR

    dirs = {
        "daily": REPORTS_DAILY_DIR,
        "weekly": REPORTS_WEEKLY_DIR,
        "monthly": REPORTS_MONTHLY_DIR,
    }
    report_dir = dirs[report_type]

    if key == "latest":
        # 找目录里最新的 .md 文件
        candidates = sorted(report_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if report_dir.is_dir() else []
        if not candidates:
            return HTMLResponse('<div style="text-align:center;padding:32px;color:var(--sub);">暂无报告</div>')
        report_path = candidates[0]
    else:
        report_path = report_dir / f"{key}.md"
        if not report_path.exists():
            raise HTTPException(404, "Report not found")

    content = report_path.read_text(encoding="utf-8")

    # 从文件名推断日期标题（文件名格式如 2026-04-15.md 或 2026-W16.md）
    stem = report_path.stem
    date_header = ""
    type_labels = {"daily": "日报", "weekly": "周报", "monthly": "月报"}
    label = type_labels[report_type]
    try:
        if report_type == "daily":
            d = datetime.strptime(stem, "%Y-%m-%d")
            date_header = f"{d.year}年{d.month}月{d.day}日 {label}"
        elif report_type == "weekly":
            # 格式如 2026-W16
            date_header = f"{stem} {label}"
        elif report_type == "monthly":
            d = datetime.strptime(stem, "%Y-%m")
            date_header = f"{d.year}年{d.month}月 {label}"
    except ValueError:
        date_header = f"{stem} {label}"

    html_content = md.markdown(content, extensions=["fenced_code", "tables"])
    return templates.TemplateResponse(
        "report_content.html",
        {
            "request": request,
            "html_content": html_content,
            "date_header": date_header,
            "key": key,
            "report_type": report_type,
        },
    )
