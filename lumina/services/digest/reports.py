"""
lumina/digest/reports.py — 日报/周报/月报的读写工具。

数据层级：
  活动摘要（每小时快照）→ 日报（每天）→ 周报（每周）→ 月报（每月）

存储路径：
  ~/.lumina/snapshots/YYYY-MM-DDTHH-MM.md   每次活动摘要快照
  ~/.lumina/reports/daily/YYYY-MM-DD.md     日报
  ~/.lumina/reports/weekly/YYYY-Www.md      周报（ISO 8601 周）
  ~/.lumina/reports/monthly/YYYY-MM.md      月报
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from lumina.config import (
    DIGEST_SNAPSHOTS_DIR,
    REPORTS_DAILY_DIR,
    REPORTS_MONTHLY_DIR,
    REPORTS_WEEKLY_DIR,
)

logger = logging.getLogger("lumina.services.digest")


# ── 快照工具 ──────────────────────────────────────────────────────────────────

def save_snapshot(content: str, ts: datetime) -> Path:
    """保存活动摘要快照到 ~/.lumina/snapshots/YYYY-MM-DDTHH-MM-SS.md。"""
    import uuid
    DIGEST_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    fname = ts.strftime("%Y-%m-%dT%H-%M-%S") + ".md"
    path = DIGEST_SNAPSHOTS_DIR / fname
    tmp = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return path


def load_snapshots_for_date(d: date) -> List[str]:
    """读取某天所有活动摘要快照，按时间正序返回文本列表。"""
    if not DIGEST_SNAPSHOTS_DIR.exists():
        return []
    prefix = d.strftime("%Y-%m-%d")
    files = sorted(DIGEST_SNAPSHOTS_DIR.glob(f"{prefix}T*.md"))
    return [f.read_text(encoding="utf-8") for f in files]


def prune_snapshots(keep_days: int = 35) -> int:
    """删除超过 keep_days 天的快照文件，返回删除数量。"""
    if not DIGEST_SNAPSHOTS_DIR.exists():
        return 0
    cutoff = datetime.now().timestamp() - keep_days * 86400
    removed = 0
    for f in DIGEST_SNAPSHOTS_DIR.glob("*.md"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
    return removed


# ── 报告读写 ──────────────────────────────────────────────────────────────────

def _report_path(report_type: str, key: str) -> Path:
    dirs = {"daily": REPORTS_DAILY_DIR, "weekly": REPORTS_WEEKLY_DIR, "monthly": REPORTS_MONTHLY_DIR}
    return dirs[report_type] / f"{key}.md"


def save_report(report_type: str, key: str, content: str) -> Path:
    import uuid
    path = _report_path(report_type, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return path


def load_report(report_type: str, key: str) -> Optional[str]:
    path = _report_path(report_type, key)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def list_report_keys(report_type: str) -> List[str]:
    """返回已生成的报告 key 列表，降序（最新在前）。"""
    dirs = {"daily": REPORTS_DAILY_DIR, "weekly": REPORTS_WEEKLY_DIR, "monthly": REPORTS_MONTHLY_DIR}
    d = dirs.get(report_type)
    if not d or not d.exists():
        return []
    return sorted([f.stem for f in d.glob("*.md")], reverse=True)


def list_snapshot_dates() -> List[date]:
    """返回已有快照覆盖到的日期列表，升序。"""
    if not DIGEST_SNAPSHOTS_DIR.exists():
        return []
    seen = set()
    for path in DIGEST_SNAPSHOTS_DIR.glob("*.md"):
        day_text = path.stem.split("T", 1)[0]
        try:
            seen.add(date.fromisoformat(day_text))
        except ValueError:
            logger.debug("Digest: skip malformed snapshot filename %s", path.name)
    return sorted(seen)


# ── 日期 key 工具 ─────────────────────────────────────────────────────────────

def daily_key(d: Optional[date] = None) -> str:
    """返回日报 key，格式 YYYY-MM-DD。"""
    return (d or date.today()).strftime("%Y-%m-%d")


def weekly_key(d: Optional[date] = None) -> str:
    """返回周报 key，格式 YYYY-Www（ISO 8601）。"""
    target = d or date.today()
    return f"{target.isocalendar()[0]}-W{target.isocalendar()[1]:02d}"


def monthly_key(d: Optional[date] = None) -> str:
    """返回月报 key，格式 YYYY-MM。"""
    return (d or date.today()).strftime("%Y-%m")


def adjacent_keys(report_type: str, key: str) -> Tuple[Optional[str], Optional[str]]:
    """返回 (prev_key, next_key)，不存在则为 None（仅基于已生成文件）。"""
    keys = list_report_keys(report_type)
    if key not in keys:
        return None, None
    idx = keys.index(key)
    prev_key = keys[idx + 1] if idx + 1 < len(keys) else None
    next_key = keys[idx - 1] if idx > 0 else None
    return prev_key, next_key


def _existing_daily_report_dates() -> List[date]:
    dates = []
    for key in list_report_keys("daily"):
        try:
            dates.append(date.fromisoformat(key))
        except ValueError:
            logger.debug("Digest: skip malformed daily report key %s", key)
    return sorted(set(dates))


def find_missing_daily_report_keys(
    now: Optional[datetime] = None,
    notify_time: str = "20:00",
) -> List[str]:
    """根据快照找缺失日报，只补已经到达生成时机的日期。"""
    now = now or datetime.now()
    today = now.date()
    try:
        notify_hour, notify_minute = map(int, notify_time.split(":"))
    except Exception:
        notify_hour, notify_minute = 20, 0
    notify_passed = (now.hour, now.minute) >= (notify_hour, notify_minute)
    existing = set(list_report_keys("daily"))

    missing = []
    for d in list_snapshot_dates():
        if d > today:
            continue
        if d == today and not notify_passed:
            continue
        key = daily_key(d)
        if key not in existing:
            missing.append(key)
    return missing


def find_missing_weekly_report_keys(today: Optional[date] = None) -> List[str]:
    """根据已有日报找缺失周报，跳过当前仍在进行中的这一周。"""
    today = today or date.today()
    current_week = weekly_key(today)
    existing = set(list_report_keys("weekly"))
    week_keys = sorted({weekly_key(d) for d in _existing_daily_report_dates() if weekly_key(d) != current_week})
    return [wk for wk in week_keys if wk not in existing]


def find_missing_monthly_report_keys(today: Optional[date] = None) -> List[str]:
    """根据已有日报找缺失月报，跳过当前仍在进行中的这个月。"""
    today = today or date.today()
    current_month = monthly_key(today)
    existing = set(list_report_keys("monthly"))
    month_keys = sorted(
        {monthly_key(d) for d in _existing_daily_report_dates() if monthly_key(d) != current_month}
    )
    return [mk for mk in month_keys if mk not in existing]


# ── 输入文本构建 ──────────────────────────────────────────────────────────────

def build_daily_input(d: date) -> Optional[str]:
    """拼接当天所有活动摘要快照，作为日报生成的输入。"""
    snapshots = load_snapshots_for_date(d)
    if not snapshots:
        return None
    parts = [f"=== 活动快照 {i+1}/{len(snapshots)} ===\n{s.strip()}" for i, s in enumerate(snapshots)]
    return (
        f"以下是 {d.strftime('%Y年%m月%d日')} 的 {len(snapshots)} 份活动摘要快照，"
        f"请基于这些内容生成当日工作日报：\n\n" + "\n\n".join(parts)
    )


def build_weekly_input(week_key: str) -> Optional[str]:
    """拼接该周每天的日报，作为周报生成的输入。"""
    # 解析 ISO 周：YYYY-Www → 该周 Mon~Sun
    year, w = week_key.split("-W")
    monday = datetime.strptime(f"{year}-W{w}-1", "%G-W%V-%u").date()
    days = [monday + timedelta(days=i) for i in range(7)]
    reports = []
    for d in days:
        content = load_report("daily", daily_key(d))
        if content:
            reports.append(f"=== {d.strftime('%Y-%m-%d')} 日报 ===\n{content.strip()}")
    if not reports:
        return None
    return (
        f"以下是 {week_key} 本周每天的日报，请基于这些内容生成工作周报：\n\n"
        + "\n\n".join(reports)
    )


def build_monthly_input(month_key: str) -> Optional[str]:
    """拼接该月所有周报，作为月报生成的输入。"""
    year, month = map(int, month_key.split("-"))
    # 找出该月包含的所有 ISO 周
    first_day = date(year, month, 1)
    last_day = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    week_keys_seen = set()
    d = first_day
    while d <= last_day:
        wk = weekly_key(d)
        week_keys_seen.add(wk)
        d += timedelta(days=7)
    week_keys_seen.add(weekly_key(last_day))

    reports = []
    for wk in sorted(week_keys_seen):
        content = load_report("weekly", wk)
        if content:
            reports.append(f"=== {wk} 周报 ===\n{content.strip()}")
    if not reports:
        return None
    return (
        f"以下是 {year}年{month:02d}月 各周的周报，请基于这些内容生成工作月报：\n\n"
        + "\n\n".join(reports)
    )
