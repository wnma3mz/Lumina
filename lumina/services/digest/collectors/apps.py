"""
lumina/digest/collectors/apps.py — 应用程序数据源采集

包含：浏览器历史、备忘录（Notes.app）、日历（Calendar.app）、AI 对话（Claude/Codex/Cursor/Gemini）。
"""
import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from lumina.services.digest.config import get_cfg
from lumina.platform_support.paths import (
    calendar_db_path,
    chromium_history_candidates,
    firefox_profile_dirs,
    notes_db_path,
    safari_history_db,
)

logger = logging.getLogger("lumina.services.digest")

_CALENDAR_CORE_OFFSET = 978307200  # CoreData epoch = Unix epoch - 978307200


def collect_browser_history(n: int = 50) -> str:
    """采集 Chrome / Firefox / Safari 浏览历史。

    始终按 history_hours 窗口全量查询，无增量状态。
    """
    cfg = get_cfg()
    cutoff_unix = time.time() - cfg.history_hours * 3600
    try:
        results: list[tuple[float, str]] = []  # (ts_unix, title_or_url)

        # ── Chromium 系浏览器（Chrome / Edge / Brave / Chromium）──────────────
        seen_db_paths: set[Path] = set()
        for history_db in chromium_history_candidates():
            if history_db in seen_db_paths:
                continue
            seen_db_paths.add(history_db)
            try:
                chrome_offset = 11644473600 * 1_000_000
                cutoff_chrome = int(cutoff_unix * 1_000_000 + chrome_offset)
                uri = history_db.as_uri() + "?mode=ro&immutable=1"
                with sqlite3.connect(uri, uri=True, timeout=3) as conn:
                    rows = conn.execute(
                        "SELECT title, url, last_visit_time FROM urls "
                        "WHERE last_visit_time > ? "
                        "ORDER BY last_visit_time DESC LIMIT ?",
                        (cutoff_chrome, n)
                    ).fetchall()
                for title, url, lv_time in rows:
                    ts_unix = (lv_time - chrome_offset) / 1_000_000
                    results.append((ts_unix, title or url))
            except Exception as e:
                logger.debug("chromium history %s: %s", history_db, e)

        # ── Firefox ───────────────────────────────────────────────────────────
        for profile_dir in firefox_profile_dirs():
            places_db = profile_dir / "places.sqlite"
            if not places_db.exists():
                continue
            try:
                cutoff_ff = int(cutoff_unix * 1_000_000)
                uri = places_db.as_uri() + "?mode=ro&immutable=1"
                with sqlite3.connect(uri, uri=True, timeout=3) as conn:
                    rows = conn.execute(
                        "SELECT title, url, last_visit_date FROM moz_places "
                        "WHERE last_visit_date > ? "
                        "ORDER BY last_visit_date DESC LIMIT ?",
                        (cutoff_ff, n)
                    ).fetchall()
                for title, url, lv_date in rows:
                    if lv_date:
                        ts_unix = lv_date / 1_000_000
                        results.append((ts_unix, title or url))
            except Exception as e:
                logger.debug("firefox history %s: %s", places_db, e)

        # ── Safari（仅 macOS）────────────────────────────────────────────────
        safari_db = safari_history_db()
        if safari_db is not None:
            try:
                safari_offset = 978307200  # CoreData epoch
                cutoff_safari = cutoff_unix - safari_offset
                uri = safari_db.as_uri() + "?mode=ro&immutable=1"
                with sqlite3.connect(uri, uri=True, timeout=3) as conn:
                    rows = conn.execute(
                        "SELECT hi.url, hv.title, hv.visit_time "
                        "FROM history_visits hv "
                        "JOIN history_items hi ON hv.history_item = hi.id "
                        "WHERE hv.visit_time > ? "
                        "ORDER BY hv.visit_time DESC LIMIT ?",
                        (cutoff_safari, n)
                    ).fetchall()
                for url, title, vt in rows:
                    ts_unix = vt + safari_offset
                    results.append((ts_unix, title or url))
            except Exception as e:
                logger.debug("safari history: %s", e)

        if not results:
            return ""

        # 按时间倒序、去重
        results.sort(key=lambda x: -x[0])
        seen: set[str] = set()
        deduped = []
        for _, label in results:
            if label and label not in seen:
                seen.add(label)
                deduped.append(f"  {label}")

        return "## 浏览器历史（过去 %.0fh）\n" % cfg.history_hours + "\n".join(deduped[:n])
    except Exception as e:
        logger.debug("browser history: %s", e)
        return ""


def collect_notes_app() -> str:
    """读取 Notes NoteStore.sqlite（仅 macOS）。

    macOS TCC 限制：打包后的 .app 需要「完整磁盘访问」才能读取备忘录数据库。
    若权限不足，返回特殊标记 '__PERMISSION_DENIED__'，由 core.py 转为提示信息。
    """
    import sqlite3 as _sqlite3
    cfg = get_cfg()
    cutoff = time.time() - cfg.history_hours * 3600
    try:
        db_path = notes_db_path()
        if db_path is None:
            return ""

        # CoreData epoch = Unix epoch - 978307200（2001-01-01 与 1970-01-01 的差值）
        cutoff_core = cutoff - 978307200

        uri = db_path.as_uri() + "?mode=ro&immutable=1"
        with _sqlite3.connect(uri, uri=True, timeout=3) as conn:
            rows = conn.execute(
                "SELECT ZTITLE1, ZSNIPPET, ZMODIFICATIONDATE1 FROM ZICCLOUDSYNCINGOBJECT "
                "WHERE ZMODIFICATIONDATE1 > ? AND ZTITLE1 IS NOT NULL "
                "ORDER BY ZMODIFICATIONDATE1 DESC LIMIT 20",
                (cutoff_core,),
            ).fetchall()

        if not rows:
            return ""

        entries = []
        for title, snippet, mod_date in rows:
            snippet_text = (snippet or "").strip()[:200]
            if snippet_text:
                entries.append(f"**{title}**:\n  {snippet_text}")
            else:
                entries.append(f"**{title}**")

        return f"## 备忘录（过去 {cfg.history_hours:.0f}h 修改）\n" + "\n\n".join(entries)
    except PermissionError:
        logger.warning("notes app: 权限不足，请在「系统设置 → 隐私与安全 → 完整磁盘访问」中授权 Lumina")
        return "__PERMISSION_DENIED__"
    except Exception as e:
        logger.debug("notes app sqlite: %s", e)
        return ""


def collect_calendar() -> str:
    """读取 macOS Calendar，返回今天及未来 history_hours 内的日程（仅 macOS）。

    无状态：日历事件按时间窗口查，每次全量读取当前窗口。
    需要完整磁盘访问权限（与 Notes.app 同理）。
    """
    cfg = get_cfg()
    try:
        cal_db = calendar_db_path()
        if cal_db is None:
            return ""

        now = time.time()
        now_core = now - _CALENDAR_CORE_OFFSET

        # 窗口：从今天 0 点到 history_hours 之后
        from datetime import date
        today_midnight = datetime.combine(date.today(), datetime.min.time()).timestamp()
        window_start = today_midnight - _CALENDAR_CORE_OFFSET
        window_end = now_core + cfg.history_hours * 3600

        uri = cal_db.as_uri() + "?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True, timeout=3) as conn:
            rows = conn.execute(
                """
                SELECT oc.occurrence_date, oc.occurrence_end_date,
                       ci.summary, ci.all_day, ci.description,
                       c.title as cal_title
                FROM OccurrenceCache oc
                JOIN CalendarItem ci ON oc.event_id = ci.ROWID
                LEFT JOIN Calendar c ON oc.calendar_id = c.ROWID
                WHERE oc.day >= ? AND oc.day <= ?
                  AND ci.hidden = 0
                ORDER BY oc.occurrence_date
                LIMIT 30
                """,
                (window_start, window_end),
            ).fetchall()

        if not rows:
            return ""

        entries = []
        for occ_date, end_date, summary, all_day, desc, cal_title in rows:
            if not summary:
                continue
            occ_ts = (occ_date or 0) + _CALENDAR_CORE_OFFSET
            end_ts = (end_date + _CALENDAR_CORE_OFFSET) if end_date else None
            if all_day:
                time_str = datetime.fromtimestamp(occ_ts).strftime("%m-%d 全天")
            else:
                start_fmt = datetime.fromtimestamp(occ_ts).strftime("%m-%d %H:%M")
                end_fmt = datetime.fromtimestamp(end_ts).strftime("%H:%M") if end_ts else ""
                time_str = f"{start_fmt}–{end_fmt}" if end_fmt else start_fmt
            cal_tag = f"[{cal_title}] " if cal_title else ""
            entry = f"- {time_str} {cal_tag}{summary}"
            if desc and len(desc.strip()) > 0:
                entry += f"\n  {desc.strip()[:100]}"
            entries.append(entry)

        return "## 日历事项\n" + "\n".join(entries)
    except PermissionError:
        logger.warning("calendar: 权限不足，请在「系统设置 → 隐私与安全 → 完整磁盘访问」中授权 Lumina")
        return ""
    except Exception as e:
        logger.debug("calendar sqlite: %s", e)
        return ""


_AI_QUERY_SKIP_PREFIXES = ("<", "[Previous conversation", "Summary:", "## ", "### ")
_AI_QUERY_SOURCE_ORDER = ("Claude Code", "Codex", "Cursor", "Gemini")


def _normalize_ai_query_text(text: str, *, max_chars: int) -> str:
    text = " ".join((text or "").split()).strip()
    if not text:
        return ""
    if any(text.startswith(prefix) for prefix in _AI_QUERY_SKIP_PREFIXES):
        return ""
    if len(text) > max_chars:
        return ""
    return text


def _coerce_query_ts(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            ts = float(raw)
        except ValueError:
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except Exception:
                return None
    else:
        return None

    magnitude = abs(ts)
    if magnitude > 1e14:
        ts /= 1_000_000
    elif magnitude > 1e11:
        ts /= 1_000
    return ts if ts > 0 else None


def _extract_cursor_transcript_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


def collect_ai_queries(n: int | None = None) -> str:
    """从 Claude Code、Codex、Cursor、Gemini 本地历史中提取用户最近的提问。"""
    cfg = get_cfg()
    cutoff = time.time() - cfg.history_hours * 3600
    max_source_chars = max(1, int(cfg.ai_queries_max_source_chars))
    try:
        source_queries: dict[str, list[tuple[float, str]]] = {
            source: [] for source in _AI_QUERY_SOURCE_ORDER
        }

        def _append_query(source: str, ts: Optional[float], text: str) -> None:
            normalized = _normalize_ai_query_text(text, max_chars=max_source_chars)
            if not normalized:
                return
            if ts is None or ts <= cutoff:
                return
            source_queries[source].append((ts, normalized))

        # ── Claude Code: history.jsonl（display 字段）─────────────────────────
        history_file = Path.home() / ".claude" / "history.jsonl"
        if history_file.exists():
            try:
                lines = history_file.read_text(errors="replace").splitlines()
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    ts_ms = obj.get("timestamp")
                    ts = ts_ms / 1000 if ts_ms else 0.0
                    if ts and ts <= cutoff:
                        break
                    text = obj.get("display", "").strip()
                    _append_query("Claude Code", ts, text)
            except Exception as e:
                logger.debug("claude history.jsonl: %s", e)

        # ── Claude Code: projects/**/*.jsonl（type=user 条目）────────────────
        projects_dir = Path.home() / ".claude" / "projects"
        if projects_dir.exists():
            try:
                jsonl_files = sorted(
                    projects_dir.rglob("*.jsonl"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for jf in jsonl_files:
                    if jf.stat().st_mtime <= cutoff:
                        continue
                    try:
                        lines = jf.read_text(errors="replace").splitlines()
                    except Exception:
                        continue
                    for line in reversed(lines):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        if obj.get("type") != "user":
                            continue
                        ts_str = obj.get("timestamp", "")
                        try:
                            ts = datetime.fromisoformat(
                                ts_str.replace("Z", "+00:00")
                            ).timestamp()
                        except Exception:
                            ts = 0.0
                        if ts and ts <= cutoff:
                            continue
                        content = obj.get("message", {}).get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                c.get("text", "") for c in content
                                if isinstance(c, dict) and c.get("type") == "text"
                            )
                        _append_query("Claude Code", ts, str(content))
            except Exception as e:
                logger.debug("claude projects jsonl: %s", e)

        # ── OpenAI Codex CLI: ~/.codex/history.jsonl（text 字段）──────────────
        codex_history = Path.home() / ".codex" / "history.jsonl"
        if codex_history.exists():
            try:
                lines = codex_history.read_text(errors="replace").splitlines()
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    ts = float(obj.get("ts", 0))
                    if ts and ts <= cutoff:
                        break
                    text = obj.get("text", "").strip()
                    _append_query("Codex", ts, text)
            except Exception as e:
                logger.debug("codex history.jsonl: %s", e)

        # ── Cursor Agent: ~/.cursor/projects/*/agent-transcripts/*.jsonl ─────
        cursor_projects_dir = Path.home() / ".cursor" / "projects"
        if cursor_projects_dir.exists():
            try:
                transcript_files = sorted(
                    cursor_projects_dir.glob("*/agent-transcripts/*/*.jsonl"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for transcript_file in transcript_files:
                    file_ts = transcript_file.stat().st_mtime
                    if file_ts <= cutoff:
                        continue
                    try:
                        lines = transcript_file.read_text(errors="replace").splitlines()
                    except Exception:
                        continue
                    for line in reversed(lines):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(obj, dict):
                            continue
                        if obj.get("role") != "user":
                            continue
                        ts = _coerce_query_ts(obj.get("timestamp")) or file_ts
                        if ts <= cutoff:
                            continue
                        text = _extract_cursor_transcript_text(obj.get("message"))
                        _append_query("Cursor", ts, text)
            except Exception as e:
                logger.debug("cursor agent transcripts: %s", e)

        # ── Gemini CLI: ~/.gemini/tmp/*/logs.json（message 字段）──────────────
        gemini_tmp_dir = Path.home() / ".gemini" / "tmp"
        if gemini_tmp_dir.exists():
            try:
                log_files = sorted(
                    gemini_tmp_dir.rglob("logs.json"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for log_file in log_files:
                    if log_file.stat().st_mtime <= cutoff:
                        continue
                    try:
                        entries = json.loads(log_file.read_text(errors="replace"))
                    except Exception:
                        continue
                    if not isinstance(entries, list):
                        continue
                    for entry in reversed(entries):
                        if not isinstance(entry, dict):
                            continue
                        if entry.get("type") != "user":
                            continue
                        ts = _coerce_query_ts(entry.get("timestamp"))
                        if ts and ts <= cutoff:
                            break
                        text = entry.get("message", "")
                        _append_query("Gemini", ts, str(text).strip())
            except Exception as e:
                logger.debug("gemini logs.json: %s", e)

        if not any(source_queries.values()):
            return ""

        blocks: list[str] = []
        for source in _AI_QUERY_SOURCE_ORDER:
            items = sorted(source_queries[source], key=lambda x: x[0], reverse=True)
            lines = [text for _, text in items]
            if not lines:
                continue
            rendered = "\n".join(f"  {text}" for text in lines)
            blocks.append(f"### {source}\n{rendered}")
        if not blocks:
            return ""
        return "## AI 对话提问（过去 %.0fh）\n" % cfg.history_hours + "\n\n".join(blocks)
    except Exception as e:
        logger.debug("ai queries: %s", e)
        return ""
