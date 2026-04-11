"""
lumina/digest/collectors.py — 各数据源采集函数

每个函数独立、失败静默返回空字符串。
新增数据源：在此文件追加函数，并在 core.py 的 _COLLECTORS 列表中注册。

──────────────────────────────────────────────────────────────────
Per-Collector Cursor 机制
──────────────────────────────────────────────────────────────────
每个 collector 自己记住「上次采集到的最新记录时间戳」（Unix 秒），
下次只读新数据，各来源完全独立。

_CURSORS 由 core.py 在 ThreadPoolExecutor 启动前注入：
  _CURSORS["collect_xxx"] = 上次该 collector 最新记录的 Unix 时间戳
  _CURSORS["_fallback"]   = now - effective_hours（全局兜底时间戳）

每个 collector：
  1. _get_cursor(name)   读自己的 cursor（无则用 _fallback）
  2. 执行增量查询
  3. _set_cursor(name, newest_ts)  写回本次最新时间戳

cursor 存储在 ~/.lumina/collector_cursors.json，由 cursor_store.py 管理。

──────────────────────────────────────────────────────────────────
当前已支持的数据来源
──────────────────────────────────────────────────────────────────
【终端历史】collect_shell_history
  └─ ~/.zsh_history 或 ~/.bash_history
     解析 zsh 扩展格式（`: ts:0;cmd`），cursor 过滤，自动去重
     兜底：文件无时间戳（bash history）→ 取最近 n=100 条

【Git 提交】collect_git_logs
  └─ 扫描 scan_dirs 下深度 ≤3 的所有 .git 目录
     cursor 对应 `git log --since=` 时间戳

【剪贴板】collect_clipboard
  └─ macOS pbpaste，截断至 500 字符，无 cursor（无状态）

【浏览器历史】collect_browser_history
  ├─ Google Chrome  ~/Library/Application Support/Google/Chrome/Default/History
  └─ Firefox        ~/Library/Application Support/Firefox/Profiles/*/places.sqlite
     cursor 存 Unix 秒，查询时转换为各浏览器原生 epoch

【备忘录（Notes.app）】collect_notes_app
  └─ 直接读取 NoteStore.sqlite
     cursor 存 Unix 秒，查询时转换为 CoreData epoch（cursor - 978307200）

【日历事项】collect_calendar
  └─ ~/Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb
     读取 OccurrenceCache + CalendarItem，采集今天及未来 cfg.history_hours 内的事件
     无 cursor（日历事件按时间窗口查询，每次全量读取当前窗口）

【本地 Markdown 笔记】collect_markdown_notes
  └─ 扫描 scan_dirs 前两个目录（默认 ~/Documents, ~/Desktop）
     cursor 直接与 st_mtime 比较（均为 Unix 秒）

【AI 对话提问】collect_ai_queries
  ├─ Claude Code  ~/.claude/history.jsonl + projects/**/*.jsonl
  ├─ OpenAI Codex CLI  ~/.codex/history.jsonl
  └─ Cursor  state.vscdb（无可靠时间戳，用 db mtime 作为代理）
──────────────────────────────────────────────────────────────────
"""
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from lumina.digest.config import get_cfg
from lumina.digest.cursor_store import load_md_hashes, md5_of_file, save_md_hashes

logger = logging.getLogger("lumina.digest")

# ── Per-Collector Cursor ──────────────────────────────────────────────────────
# 由 core.py 在 ThreadPoolExecutor 启动前注入，各 collector 只读自己的 key。
# "_fallback" key = now - effective_hours（全局兜底时间戳）。
_CURSORS: dict = {}

# 上次 collect_markdown_notes 扫到的文件列表，供 debug 面板展示
_last_md_files: list[dict] = []


def _get_cursor(name: str) -> float:
    """返回该 collector 的 since 时间戳（Unix 秒）。
    无 cursor 或 cursor <= 0 时使用 _fallback，再无则用 24h 前。
    """
    ts = _CURSORS.get(name)
    if not ts or ts <= 0:
        ts = _CURSORS.get("_fallback", time.time() - 24 * 3600)
    return float(ts)


def _set_cursor(name: str, newest_ts: Optional[float]) -> None:
    """记录本次采集到的最新时间戳，供下次增量使用。"""
    if newest_ts and newest_ts > 0:
        _CURSORS[name] = float(newest_ts)


# ── Collectors ────────────────────────────────────────────────────────────────

def collect_shell_history(n: int = 100) -> str:
    name = "collect_shell_history"
    cursor = _get_cursor(name)
    try:
        zsh  = Path.home() / ".zsh_history"
        bash = Path.home() / ".bash_history"
        src  = zsh if zsh.exists() else (bash if bash.exists() else None)
        if not src:
            return ""
        raw = src.read_text(errors="replace").splitlines()

        cmds: list[str] = []
        seen: set[str] = set()
        newest_ts: Optional[float] = None
        has_timestamps = False

        for line in reversed(raw):
            ts_val: Optional[float] = None
            cmd = line

            # 解析 zsh 扩展格式：": <unix_ts>:<elapsed>;<command>"
            if line.startswith(": ") and ";" in line:
                try:
                    meta, cmd = line.split(";", 1)
                    # meta = ": 1712500000:0"  →  parts[1] 是时间戳
                    ts_str = meta.split(":")[1].strip()
                    ts_val = float(ts_str)
                    has_timestamps = True
                except (ValueError, IndexError):
                    pass

            if ts_val is not None:
                # 倒序迭代：遇到早于 cursor 的记录即可停止
                if ts_val <= cursor:
                    break
                if newest_ts is None or ts_val > newest_ts:
                    newest_ts = ts_val

            cmd = cmd.strip()
            if not cmd or cmd in seen:
                continue
            seen.add(cmd)
            cmds.append(cmd)
            if len(cmds) >= n:
                break

        # 兜底：整个文件无可解析时间戳（bash history 或纯文本格式）
        # → 回退到原来的取最近 n 条逻辑，不更新 cursor
        if not has_timestamps:
            cmds, seen = [], set()
            for line in reversed(raw):
                if line.startswith(": ") and ";" in line:
                    line = line.split(";", 1)[1]
                line = line.strip()
                if not line or line in seen:
                    continue
                seen.add(line)
                cmds.append(line)
                if len(cmds) >= n:
                    break
        else:
            if newest_ts is not None:
                _set_cursor(name, newest_ts - 1)

        if not cmds:
            return ""
        return "## 终端历史（最近命令）\n" + "\n".join(f"  {c}" for c in reversed(cmds))
    except Exception as e:
        logger.debug("shell history: %s", e)
        return ""


def collect_git_logs(n: int = 20) -> str:
    name = "collect_git_logs"
    cursor = _get_cursor(name)
    cfg = get_cfg()
    try:
        since = datetime.fromtimestamp(cursor).strftime("%Y-%m-%d %H:%M")
        entries, seen_repos = [], set()
        newest_ts: Optional[float] = None

        for root_str in cfg.scan_dirs:
            root = Path(root_str)
            if not root.exists():
                continue
            for git_dir in root.rglob(".git"):
                if not git_dir.is_dir():
                    continue
                repo_dir = git_dir.parent
                if repo_dir in seen_repos:
                    continue
                try:
                    rel = repo_dir.relative_to(root)
                    if len(rel.parts) > 3:
                        continue
                except ValueError:
                    continue
                seen_repos.add(repo_dir)
                try:
                    # "%ct %H %s"：commit Unix 时间戳 + hash + subject
                    result = subprocess.run(
                        ["git", "log", "--format=%ct %H %s",
                         f"--since={since}", f"-{n}"],
                        cwd=str(repo_dir), capture_output=True, text=True, timeout=5
                    )
                    lines = result.stdout.strip().splitlines()
                    if lines:
                        display_lines = []
                        for raw_line in lines:
                            parts = raw_line.split(" ", 2)
                            if len(parts) == 3:
                                ts_part, hash_part, subject = parts
                                try:
                                    ts_val = float(ts_part)
                                    if newest_ts is None or ts_val > newest_ts:
                                        newest_ts = ts_val
                                except ValueError:
                                    pass
                                display_lines.append(f"  {hash_part[:7]} {subject}")
                            else:
                                display_lines.append(f"  {raw_line}")
                        entries.append(f"**{repo_dir.name}**:\n" +
                                       "\n".join(display_lines))
                except Exception:
                    continue

        if newest_ts is not None:
            _set_cursor(name, newest_ts - 1)

        if not entries:
            return ""
        return "## Git 提交（过去 %.0fh）\n" % cfg.history_hours + "\n\n".join(entries)
    except Exception as e:
        logger.debug("git logs: %s", e)
        return ""


def collect_clipboard() -> str:
    # 无状态，不使用 cursor
    try:
        from lumina.platform_utils import clipboard_get
        content = clipboard_get().strip()
        if not content:
            return ""
        if len(content) > 500:
            content = content[:500] + "…（已截断）"
        return f"## 剪贴板内容\n{content}"
    except Exception as e:
        logger.debug("clipboard: %s", e)
        return ""


def collect_browser_history(n: int = 50) -> str:
    import sys as _sys
    name = "collect_browser_history"
    cursor = _get_cursor(name)   # Unix 秒
    cfg = get_cfg()
    try:
        results = []
        newest_ts: Optional[float] = None

        # Chrome — macOS / Windows 路径
        if _sys.platform == "win32":
            chrome_db = (Path.home() / "AppData" / "Local" /
                         "Google" / "Chrome" / "User Data" / "Default" / "History")
        else:
            chrome_db = (Path.home() / "Library" / "Application Support" /
                         "Google" / "Chrome" / "Default" / "History")
        if chrome_db.exists():
            try:
                uri = chrome_db.as_uri() + "?mode=ro&immutable=1"
                with sqlite3.connect(uri, uri=True) as conn:
                    chrome_offset = 11644473600 * 1_000_000
                    cutoff_chrome = int(cursor * 1_000_000 + chrome_offset)
                    rows = conn.execute(
                        "SELECT title, url, last_visit_time FROM urls "
                        "WHERE last_visit_time > ? "
                        "ORDER BY last_visit_time DESC LIMIT ?",
                        (cutoff_chrome, n)
                    ).fetchall()
                for title, url, lv_time in rows:
                    results.append(title or url)
                    ts_unix = (lv_time - chrome_offset) / 1_000_000
                    if newest_ts is None or ts_unix > newest_ts:
                        newest_ts = ts_unix
            except Exception as e:
                logger.debug("chrome history: %s", e)

        # Firefox — macOS / Windows 路径
        if _sys.platform == "win32":
            ff_profiles = Path.home() / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"
        else:
            ff_profiles = Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles"
        if ff_profiles.exists():
            for profile_dir in ff_profiles.iterdir():
                places_db = profile_dir / "places.sqlite"
                if not places_db.exists():
                    continue
                try:
                    uri = places_db.as_uri() + "?mode=ro&immutable=1"
                    with sqlite3.connect(uri, uri=True) as conn:
                        cutoff_ff = int(cursor * 1_000_000)
                        rows = conn.execute(
                            "SELECT title, url, last_visit_date FROM moz_places "
                            "WHERE last_visit_date > ? "
                            "ORDER BY last_visit_date DESC LIMIT ?",
                            (cutoff_ff, n)
                        ).fetchall()
                    for title, url, lv_date in rows:
                        results.append(title or url)
                        if lv_date:
                            ts_unix = lv_date / 1_000_000
                            if newest_ts is None or ts_unix > newest_ts:
                                newest_ts = ts_unix
                except Exception as e:
                    logger.debug("firefox history: %s", e)

        if newest_ts is not None:
            _set_cursor(name, newest_ts - 1)

        if not results:
            return ""
        seen, deduped = set(), []
        for r in results:
            if r and r not in seen:
                seen.add(r)
                deduped.append(f"  {r}")
        return "## 浏览器历史（过去 %.0fh）\n" % cfg.history_hours + "\n".join(deduped[:n])
    except Exception as e:
        logger.debug("browser history: %s", e)
        return ""


def collect_notes_app() -> str:
    """读取 Notes NoteStore.sqlite（仅 macOS）。

    macOS TCC 限制：打包后的 .app 需要「完整磁盘访问」才能读取备忘录数据库。
    若权限不足，返回特殊标记 '__PERMISSION_DENIED__'，由 core.py 转为提示信息。

    cursor 存 Unix 秒；查询时转换为 CoreData epoch（cursor - 978307200）。
    """
    import sys as _sys
    if _sys.platform != "darwin":
        return ""
    name = "collect_notes_app"
    cursor = _get_cursor(name)   # Unix 秒
    import sqlite3 as _sqlite3
    cfg = get_cfg()
    try:
        db_path = Path.home() / "Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
        if not db_path.exists():
            return ""

        # CoreData epoch = Unix epoch - 978307200（2001-01-01 与 1970-01-01 的差值）
        cursor_core = cursor - 978307200

        uri = db_path.as_uri() + "?mode=ro&immutable=1"
        with _sqlite3.connect(uri, uri=True) as conn:
            rows = conn.execute(
                "SELECT ZTITLE1, ZSNIPPET, ZMODIFICATIONDATE1 FROM ZICCLOUDSYNCINGOBJECT "
                "WHERE ZMODIFICATIONDATE1 > ? AND ZTITLE1 IS NOT NULL "
                "ORDER BY ZMODIFICATIONDATE1 DESC LIMIT 20",
                (cursor_core,),
            ).fetchall()

        if not rows:
            return ""

        newest_ts: Optional[float] = None
        entries = []
        for title, snippet, mod_date in rows:
            if mod_date is not None:
                ts_unix = float(mod_date) + 978307200
                if newest_ts is None or ts_unix > newest_ts:
                    newest_ts = ts_unix
            snippet_text = (snippet or "").strip()[:200]
            if snippet_text:
                entries.append(f"**{title}**:\n  {snippet_text}")
            else:
                entries.append(f"**{title}**")

        # cursor 退 1 秒，防止同一秒内其他修改因 mtime == cursor 被严格大于过滤掉
        if newest_ts is not None:
            _set_cursor(name, newest_ts - 1)

        return f"## 备忘录（过去 {cfg.history_hours:.0f}h 修改）\n" + "\n\n".join(entries)
    except PermissionError:
        logger.warning("notes app: 权限不足，请在「系统设置 → 隐私与安全 → 完整磁盘访问」中授权 Lumina")
        return "__PERMISSION_DENIED__"
    except Exception as e:
        logger.debug("notes app sqlite: %s", e)
        return ""


_CALENDAR_DB = Path.home() / "Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb"
_CALENDAR_CORE_OFFSET = 978307200  # CoreData epoch = Unix epoch - 978307200


def collect_calendar() -> str:
    """读取 macOS Calendar，返回今天及未来 history_hours 内的日程（仅 macOS）。

    无 cursor：日历事件按时间窗口查，不存在「只读新事件」的语义。
    需要完整磁盘访问权限（与 Notes.app 同理）。
    """
    import sys as _sys
    if _sys.platform != "darwin":
        return ""
    cfg = get_cfg()
    try:
        if not _CALENDAR_DB.exists():
            return ""

        now = time.time()
        now_core = now - _CALENDAR_CORE_OFFSET

        # 窗口：从今天 0 点到 history_hours 之后
        from datetime import date
        today_midnight = datetime.combine(date.today(), datetime.min.time()).timestamp()
        window_start = today_midnight - _CALENDAR_CORE_OFFSET
        window_end = now_core + cfg.history_hours * 3600

        uri = _CALENDAR_DB.as_uri() + "?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as conn:
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


_MD_SKIP_PARTS = {".app", "build", "dist", "node_modules", ".git", ".venv", "__pycache__"}


def collect_markdown_notes() -> str:
    """扫描 scan_dirs 下最近修改的 .md 文件。

    两级过滤：
    1. mtime > cursor（快速跳过明显旧文件）
    2. md5(前4KB) 与上次采集不同（过滤 Cursor/iCloud 等 mtime-only 误触发）
    """
    name = "collect_markdown_notes"
    cursor = _get_cursor(name)
    cfg = get_cfg()
    try:
        hashes = load_md_hashes()
        candidates: list[tuple[float, Path]] = []

        for root_str in cfg.scan_dirs:
            root = Path(root_str)
            if not root.exists():
                continue
            for md in root.rglob("*.md"):
                # 跳过构建产物、依赖包、隐藏目录等无关路径
                if any(part in _MD_SKIP_PARTS for part in md.parts):
                    continue
                try:
                    mtime = md.stat().st_mtime
                    if mtime <= cursor:
                        continue
                    # mtime 有变化，再用 md5 确认内容是否真的改了
                    key = str(md)
                    current_hash = md5_of_file(md)
                    if hashes.get(key) == current_hash:
                        # 内容未变（编辑器扫描/同步等误触发），更新 hash 记录但不采集
                        hashes[key] = current_hash
                        continue
                    candidates.append((mtime, md, current_hash))
                except Exception:
                    continue

        global _last_md_files
        _last_md_files = [
            {"path": str(md), "mtime": mtime}
            for mtime, md, _ in sorted(candidates, key=lambda x: -x[0])
        ]

        if not candidates:
            return ""

        candidates.sort(key=lambda x: -x[0])
        newest_ts = candidates[0][0]

        entries = []
        for mtime, md, _ in candidates[:10]:
            try:
                content = md.read_text(errors="replace")[:200].strip()
                if content:
                    entries.append(f"**{md.name}**:\n  {content}")
            except Exception:
                continue

        # 只有真正产出内容后才推进 hash 和 cursor，避免临时 IO 失败导致文件被永久跳过
        if entries:
            for _, md, current_hash in candidates:
                hashes[str(md)] = current_hash
            save_md_hashes(hashes)
            # cursor 退 1 秒，防止同一秒内其他文件在下次采集时因 mtime == cursor 被过滤
            _set_cursor(name, newest_ts - 1)

        if not entries:
            return ""
        return "## 本地 Markdown 笔记\n" + "\n\n".join(entries)
    except Exception as e:
        logger.debug("markdown notes: %s", e)
        return ""


def collect_ai_queries(n: int = 50) -> str:
    """从 Claude Code、Codex、Cursor 本地历史中提取用户最近的提问。"""
    name = "collect_ai_queries"
    cursor = _get_cursor(name)   # Unix 秒
    cfg = get_cfg()
    try:
        queries: list[tuple[float, str]] = []
        newest_ts: Optional[float] = None

        def _update_newest(ts: float) -> None:
            nonlocal newest_ts
            if ts and ts > 0 and (newest_ts is None or ts > newest_ts):
                newest_ts = ts

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
                    if ts and ts <= cursor:
                        break
                    text = obj.get("display", "").strip()
                    if text:
                        queries.append((ts, text))
                        _update_newest(ts)
                    if len(queries) >= n:
                        break
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
                )[:20]
                for jf in jsonl_files:
                    if jf.stat().st_mtime <= cursor:
                        continue
                    try:
                        lines = jf.read_text(errors="replace").splitlines()
                    except Exception:
                        continue
                    for line in lines:
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
                        if ts and ts <= cursor:
                            continue
                        content = obj.get("message", {}).get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                c.get("text", "") for c in content
                                if isinstance(c, dict) and c.get("type") == "text"
                            )
                        content = content.strip()
                        _skip_prefixes = ("<", "[Previous conversation", "Summary:", "## ", "### ")
                        if (content and len(content) < 2000
                                and not any(content.startswith(p) for p in _skip_prefixes)):
                            queries.append((ts, content))
                            _update_newest(ts)
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
                    if ts and ts <= cursor:
                        break
                    text = obj.get("text", "").strip()
                    if text:
                        queries.append((ts, text))
                        _update_newest(ts)
                    if len(queries) >= n:
                        break
            except Exception as e:
                logger.debug("codex history.jsonl: %s", e)

        # ── Cursor: state.vscdb（bubbleId:* 条目）────────────────────────────
        # Cursor IDE 气泡无可靠时间戳；用 DB 文件 mtime 作为代理。
        # 若 mtime <= cursor，说明 DB 自上次采集后未更新，跳过。
        cursor_db = (Path.home() / "Library" / "Application Support" /
                     "Cursor" / "User" / "globalStorage" / "state.vscdb")
        if cursor_db.exists():
            db_mtime = cursor_db.stat().st_mtime
            if db_mtime > cursor:
                tmp_fd3, tmp_str3 = tempfile.mkstemp(suffix=".db", prefix="lumina_cursor_")
                tmp = Path(tmp_str3)
                try:
                    os.close(tmp_fd3)
                except OSError:
                    pass
                try:
                    shutil.copy2(str(cursor_db), str(tmp))
                    with sqlite3.connect(str(tmp)) as conn:
                        rows = conn.execute(
                            "SELECT value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
                            " AND length(value) < 4000"
                        ).fetchall()
                    for (value,) in rows:
                        try:
                            val = (bytes(value).decode("utf-8", errors="replace")
                                   if isinstance(value, (bytes, bytearray)) else str(value))
                            obj = json.loads(val)
                            if not isinstance(obj, dict):
                                continue
                            if "humanChanges" not in obj or len(obj) > 15:
                                continue
                            text = obj.get("text", "").strip()
                            if text and len(text) < 2000:
                                queries.append((db_mtime, text))
                        except Exception:
                            continue
                    _update_newest(db_mtime)
                except Exception as e:
                    logger.debug("cursor state.vscdb: %s", e)
                finally:
                    tmp.unlink(missing_ok=True)

        if newest_ts is not None:
            _set_cursor(name, newest_ts - 1)

        if not queries:
            return ""

        seen, deduped = set(), []
        for ts, text in sorted(queries, key=lambda x: x[0], reverse=True):
            key = text[:120]
            if key not in seen:
                seen.add(key)
                deduped.append(text)
            if len(deduped) >= n:
                break

        lines_out = [f"  {q[:200]}" for q in reversed(deduped)]
        return "## AI 对话提问（过去 %.0fh）\n" % cfg.history_hours + "\n".join(lines_out)
    except Exception as e:
        logger.debug("ai queries: %s", e)
        return ""
