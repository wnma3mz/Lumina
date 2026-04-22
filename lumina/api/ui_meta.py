"""
lumina/ui_meta.py — 统一维护前端入口、图片任务、Prompt 与 collector 元数据。
"""
from __future__ import annotations

from typing import Any, Optional

HOME_TAB_DEFS: list[dict[str, str]] = [
    {"key": "digest", "label": "回顾"},
    {"key": "document", "label": "文档"},
    {"key": "image", "label": "图像"},
    {"key": "audio", "label": "音频"},
    {"key": "settings", "label": "设置"},
]
HOME_TAB_KEYS = tuple(item["key"] for item in HOME_TAB_DEFS)
LEGACY_HOME_TAB_MAP = {
    "translate": "document",
    "summarize": "document",
    "lab": "image",
}

IMAGE_TASK_DEFS: list[dict[str, Any]] = [
    {
        "key": "image_ocr",
        "label": "图片 OCR",
        "short_label": "OCR 提取",
        "description": "支持图片文件或图片直链 URL，提取图片里的文字。",
        "modes": ["url", "file"],
        "file_accept": "image/*",
        "file_label": "点击选择、粘贴或拖入图片",
        "button": "开始 OCR",
        "prompt_text": "请提取这张图片中的所有可识别文字。",
        "config_label": "图片 OCR",
    },
    {
        "key": "image_caption",
        "label": "图片 Caption",
        "short_label": "Caption 描述",
        "description": "支持图片文件或图片直链 URL，生成简洁描述。",
        "modes": ["url", "file"],
        "file_accept": "image/*",
        "file_label": "点击选择、粘贴或拖入图片",
        "button": "生成 Caption",
        "prompt_text": "请描述这张图片。",
        "config_label": "图片 Caption",
    },
]
IMAGE_TASK_KEYS = tuple(item["key"] for item in IMAGE_TASK_DEFS)

AUDIO_TASK_DEFS: list[dict[str, Any]] = [
    {
        "key": "audio_live",
        "label": "实时同传 (Beta)",
        "short_label": "实时同传",
        "description": "捕获系统音频（如视频、会议）并实时转写翻译。需安装 BlackHole (macOS) 等回路设备。",
        "modes": ["live"],
        "button": "开启同传",
        "config_label": "实时同传",
    },
]
AUDIO_TASK_KEYS = tuple(item["key"] for item in AUDIO_TASK_DEFS)

SYSTEM_PROMPT_DEFS: list[dict[str, str]] = [
    {"key": "translate_to_zh", "label": "翻译为中文"},
    {"key": "translate_to_en", "label": "翻译为英文"},
    {"key": "summarize", "label": "摘要"},
    {"key": "polish_zh", "label": "中文润色"},
    {"key": "polish_en", "label": "英文润色"},
    {"key": "chat", "label": "对话（默认）"},
    {"key": "digest", "label": "活动摘要"},
    {"key": "daily_report", "label": "日报"},
    {"key": "weekly_report", "label": "周报"},
    {"key": "monthly_report", "label": "月报"},
    {"key": "image_ocr", "label": "图片 OCR"},
    {"key": "image_caption", "label": "图片 Caption"},
    {"key": "live_translate", "label": "实时同传（润色指令）"},
    {"key": "asr_zh", "label": "语音识别提示词（中文）"},
    {"key": "asr_en", "label": "语音识别提示词（英文）"},
]
SYSTEM_PROMPT_LABELS = {item["key"]: item["label"] for item in SYSTEM_PROMPT_DEFS}
SYSTEM_PROMPT_ORDER = [item["key"] for item in SYSTEM_PROMPT_DEFS]

COLLECTOR_DEFS: list[dict[str, str]] = [
    {"key": "collect_shell_history", "label": "Shell", "icon": "🖥", "filter_key": "shell"},
    {"key": "collect_git_logs", "label": "Git", "icon": "📁", "filter_key": "git"},
    {"key": "collect_clipboard", "label": "剪贴板", "icon": "📌", "filter_key": "clipboard"},
    {"key": "collect_browser_history", "label": "浏览器", "icon": "🌐", "filter_key": "browser"},
    {"key": "collect_notes_app", "label": "备忘录", "icon": "📝", "filter_key": "notes"},
    {"key": "collect_calendar", "label": "日历", "icon": "📅", "filter_key": "calendar"},
    {"key": "collect_markdown_notes", "label": "Markdown", "icon": "📄", "filter_key": "markdown"},
    {"key": "collect_ai_queries", "label": "AI", "icon": "🤖", "filter_key": "ai"},
    {"key": "collect_recent_file_activities", "label": "最近文件", "icon": "🗂", "filter_key": "files"},
]
COLLECTOR_META = {item["key"]: item for item in COLLECTOR_DEFS}
TIMELINE_COLOR_META = {
    "shell": "bg-indigo-500",
    "git": "bg-emerald-500",
    "clipboard": "bg-amber-500",
    "browser": "bg-blue-500",
    "notes": "bg-purple-500",
    "calendar": "bg-rose-500",
    "markdown": "bg-cyan-500",
    "ai": "bg-fuchsia-500",
    "files": "bg-orange-500",
}


def _runtime_collector_keys() -> list[str]:
    from lumina.services.digest.collectors import COLLECTORS

    return [fn.__name__ for fn in COLLECTORS]


def _default_collector_label(key: str) -> str:
    base = key.removeprefix("collect_").replace("_", " ").strip()
    return base.title() if base else key


def _default_collector_filter_key(key: str) -> str:
    return key.removeprefix("collect_").replace("_", "-").strip("-") or "collector"


def resolve_collector_meta(key: str) -> dict[str, str]:
    item = COLLECTOR_META.get(key)
    if item is not None:
        return item
    return {
        "key": key,
        "label": _default_collector_label(key),
        "icon": "📦",
        "filter_key": _default_collector_filter_key(key),
    }


def collector_timeline_class(filter_key: Optional[str]) -> str:
    if not filter_key:
        return "bg-zinc-400"
    return TIMELINE_COLOR_META.get(filter_key, "bg-zinc-400")


def _collector_match_terms(meta: dict[str, str]) -> list[str]:
    terms = [
        meta.get("filter_key", ""),
        meta.get("label", ""),
        meta.get("key", "").removeprefix("collect_"),
    ]
    return [term.lower() for term in terms if term]


def list_runtime_collectors(collectors: Optional[dict[str, Any]] = None) -> list[str]:
    keys = _runtime_collector_keys()
    seen = set(keys)
    if isinstance(collectors, dict):
        for key in collectors:
            if isinstance(key, str) and key not in seen:
                keys.append(key)
                seen.add(key)
    return keys


def system_prompt_items(prompts: Optional[dict]) -> list[dict[str, str]]:
    if not isinstance(prompts, dict):
        return []
    keys = [key for key in prompts.keys() if isinstance(key, str) and not key.startswith("_")]
    ordered_keys = [key for key in SYSTEM_PROMPT_ORDER if key in keys] + [key for key in keys if key not in SYSTEM_PROMPT_ORDER]
    return [
        {
            "key": key,
            "label": SYSTEM_PROMPT_LABELS.get(key, key),
            "value": str(prompts.get(key, "")),
        }
        for key in ordered_keys
    ]


def collector_sources(collectors: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    collectors = collectors if isinstance(collectors, dict) else {}
    sources: list[dict[str, Any]] = []
    for key in list_runtime_collectors(collectors):
        item = resolve_collector_meta(key)
        info = collectors.get(key, {})
        chars = info.get("chars", 0) if isinstance(info, dict) else 0
        active = chars > 0
        sources.append(
            {
                "key": key,
                "name": item["label"],
                "icon": item["icon"],
                "filter_key": item["filter_key"],
                "active": active,
                "chars": chars,
                "detail": (
                    f"最近 24 小时采集了 {chars} 字符"
                    if active else
                    "最近 24 小时无活动"
                ),
            }
        )
    return sources


def digest_icon_for_text(text: str) -> tuple[str, Optional[str]]:
    lowered = text.lower()
    for key in list_runtime_collectors():
        meta = resolve_collector_meta(key)
        if any(term in lowered for term in _collector_match_terms(meta)):
            return meta["icon"], meta["filter_key"]
    return "📋", None
