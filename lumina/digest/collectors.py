"""
lumina/digest/collectors.py — 各数据源采集函数

每个函数独立、失败静默返回空字符串。
新增数据源：在此文件追加函数，并在 core.py 的 _COLLECTORS 列表中注册。

──────────────────────────────────────────────────────────────────
当前已支持的数据来源
──────────────────────────────────────────────────────────────────
【终端历史】collect_shell_history
  └─ ~/.zsh_history 或 ~/.bash_history
     解析 zsh 扩展格式（`: ts:0;cmd`），按 cutoff 过滤，自动去重
     兜底：文件无时间戳（bash history）→ 取最近 n=100 条

【Git 提交】collect_git_logs
  └─ 扫描 scan_dirs 下深度 ≤3 的所有 .git 目录
     --since= 使用 cutoff 时间戳

【剪贴板】collect_clipboard
  └─ macOS pbpaste，截断至 500 字符，无状态

【浏览器历史】collect_browser_history
  ├─ Google Chrome  ~/Library/Application Support/Google/Chrome/Default/History
  ├─ Firefox        ~/Library/Application Support/Firefox/Profiles/*/places.sqlite
  └─ Safari         ~/Library/Safari/History.db

【备忘录（Notes.app）】collect_notes_app
  └─ 直接读取 NoteStore.sqlite
     cutoff 转换为 CoreData epoch（cutoff - 978307200）

【日历事项】collect_calendar
  └─ ~/Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb
     读取 OccurrenceCache + CalendarItem，采集今天及未来 cfg.history_hours 内的事件

【本地 Markdown 笔记】collect_markdown_notes
  └─ 扫描 scan_dirs 目录
     mtime > cutoff + md5 去重（过滤 Cursor/iCloud 等 mtime-only 误触发）

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

# 上次 collect_markdown_notes 扫到的文件列表，供 debug 面板展示
_last_md_files: list[dict] = []


# ── Collectors ────────────────────────────────────────────────────────────────

def collect_shell_history(n: int = 100) -> str:
    cfg = get_cfg()
    cutoff = time.time() - cfg.history_hours * 3600
    try:
        zsh  = Path.home() / ".zsh_history"
        bash = Path.home() / ".bash_history"
        src  = zsh if zsh.exists() else (bash if bash.exists() else None)
        if not src:
            return ""
        raw = src.read_text(errors="replace").splitlines()

        cmds: list[str] = []
        seen: set[str] = set()
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
                # 倒序迭代：遇到早于 cutoff 的记录即可停止
                if ts_val <= cutoff:
                    break

            cmd = cmd.strip()
            if not cmd or cmd in seen:
                continue
            seen.add(cmd)
            cmds.append(cmd)
            if len(cmds) >= n:
                break

        # 兜底：整个文件无可解析时间戳（bash history 或纯文本格式）
        # → 回退到原来的取最近 n 条逻辑
        if not has_timestamps:
            cmds, seen = [], set()
            for line in reversed(raw):
                if line.startswith(": ") and ";" in line:
                    line = line.split(";", 1)[1]
                line = line.strip()
                # 跳过 bash HISTTIMEFORMAT 产生的 "#<unix_ts>" 行
                if line.startswith("#") and line[1:].isdigit():
                    continue
                if not line or line in seen:
                    continue
                seen.add(line)
                cmds.append(line)
                if len(cmds) >= n:
                    break

        if not cmds:
            return ""
        return "## 终端历史（最近命令）\n" + "\n".join(f"  {c}" for c in reversed(cmds))
    except Exception as e:
        logger.debug("shell history: %s", e)
        return ""


def collect_git_logs(n: int = 20) -> str:
    cfg = get_cfg()
    cutoff = time.time() - cfg.history_hours * 3600
    since = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d %H:%M")
    try:
        entries, seen_repos = [], set()

        for root_str in cfg.scan_dirs:
            root = Path(root_str).expanduser()
            if not root.exists():
                continue
            for git_dir in _walk_git_dirs(root, max_depth=4):
                repo_dir = git_dir.parent
                if repo_dir in seen_repos:
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
                                _, hash_part, subject = parts
                                display_lines.append(f"  {hash_part[:7]} {subject}")
                            else:
                                display_lines.append(f"  {raw_line}")
                        entries.append(f"**{repo_dir.name}**:\n" +
                                       "\n".join(display_lines))
                except Exception:
                    continue

        if not entries:
            return ""
        return "## Git 提交（过去 %.0fh）\n" % cfg.history_hours + "\n\n".join(entries)
    except Exception as e:
        logger.debug("git logs: %s", e)
        return ""


def collect_clipboard() -> str:
    # 无状态，不使用 cutoff
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
    """采集 Chrome / Firefox / Safari 浏览历史。

    始终按 history_hours 窗口全量查询，无增量状态。
    """
    import sys as _sys
    cfg = get_cfg()
    cutoff_unix = time.time() - cfg.history_hours * 3600
    try:
        results: list[tuple[float, str]] = []  # (ts_unix, title_or_url)

        # ── Chrome ────────────────────────────────────────────────────────────
        if _sys.platform == "win32":
            chrome_db = (Path.home() / "AppData" / "Local" /
                         "Google" / "Chrome" / "User Data" / "Default" / "History")
        elif _sys.platform == "darwin":
            chrome_db = (Path.home() / "Library" / "Application Support" /
                         "Google" / "Chrome" / "Default" / "History")
        else:  # Linux
            chrome_db = Path.home() / ".config" / "google-chrome" / "Default" / "History"
        if chrome_db.exists():
            try:
                chrome_offset = 11644473600 * 1_000_000
                cutoff_chrome = int(cutoff_unix * 1_000_000 + chrome_offset)
                uri = chrome_db.as_uri() + "?mode=ro&immutable=1"
                with sqlite3.connect(uri, uri=True) as conn:
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
                logger.debug("chrome history: %s", e)

        # ── Firefox ───────────────────────────────────────────────────────────
        if _sys.platform == "win32":
            ff_profiles = Path.home() / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"
        elif _sys.platform == "darwin":
            ff_profiles = Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles"
        else:  # Linux
            ff_profiles = Path.home() / ".mozilla" / "firefox"
        if ff_profiles.exists():
            for profile_dir in ff_profiles.iterdir():
                places_db = profile_dir / "places.sqlite"
                if not places_db.exists():
                    continue
                try:
                    cutoff_ff = int(cutoff_unix * 1_000_000)
                    uri = places_db.as_uri() + "?mode=ro&immutable=1"
                    with sqlite3.connect(uri, uri=True) as conn:
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
                    logger.debug("firefox history: %s", e)

        # ── Safari（仅 macOS）────────────────────────────────────────────────
        if _sys.platform == "darwin":
            safari_db = Path.home() / "Library" / "Safari" / "History.db"
            if safari_db.exists():
                try:
                    safari_offset = 978307200  # CoreData epoch
                    cutoff_safari = cutoff_unix - safari_offset
                    uri = safari_db.as_uri() + "?mode=ro&immutable=1"
                    with sqlite3.connect(uri, uri=True) as conn:
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
    import sys as _sys
    if _sys.platform != "darwin":
        return ""
    import sqlite3 as _sqlite3
    cfg = get_cfg()
    cutoff = time.time() - cfg.history_hours * 3600
    try:
        db_path = Path.home() / "Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
        if not db_path.exists():
            return ""

        # CoreData epoch = Unix epoch - 978307200（2001-01-01 与 1970-01-01 的差值）
        cutoff_core = cutoff - 978307200

        uri = db_path.as_uri() + "?mode=ro&immutable=1"
        with _sqlite3.connect(uri, uri=True) as conn:
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


_CALENDAR_DB = Path.home() / "Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb"
_CALENDAR_CORE_OFFSET = 978307200  # CoreData epoch = Unix epoch - 978307200


def collect_calendar() -> str:
    """读取 macOS Calendar，返回今天及未来 history_hours 内的日程（仅 macOS）。

    无状态：日历事件按时间窗口查，每次全量读取当前窗口。
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

_GIT_SKIP_DIRS = {".git", ".venv", "node_modules", "build", "dist", "__pycache__", ".app"}


def _walk_git_dirs(root: Path, max_depth: int = 4):
    """yield 深度 ≤ max_depth 的 .git 目录父路径（即仓库根），不进入忽略目录。"""
    def _recurse(path: Path, depth: int):
        if depth > max_depth:
            return
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except (PermissionError, OSError):
            return
        for entry in entries:
            if entry.name == ".git" and entry.is_dir(follow_symlinks=False):
                yield Path(entry.path)
            elif entry.is_dir(follow_symlinks=False) and entry.name not in _GIT_SKIP_DIRS:
                yield from _recurse(Path(entry.path), depth + 1)
    yield from _recurse(root, 0)


def _walk_md_files(root: Path, max_depth: int = 4):
    """yield 深度 ≤ max_depth 的 .md 文件，不进入忽略目录及隐藏目录。"""
    root_str = str(root)
    root_depth = root_str.count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root_str):
        cur_depth = dirpath.count(os.sep) - root_depth
        if cur_depth >= max_depth:
            dirnames.clear()
        else:
            dirnames[:] = [
                d for d in dirnames
                if d not in _MD_SKIP_PARTS and not d.startswith(".")
            ]
        for fname in filenames:
            if fname.endswith(".md"):
                yield Path(dirpath) / fname


def collect_markdown_notes() -> str:
    """扫描 scan_dirs 下最近修改的 .md 文件。

    两级过滤：
    1. mtime > cutoff（快速跳过明显旧文件）
    2. md5(前4KB) 与上次采集不同（过滤 Cursor/iCloud 等 mtime-only 误触发）
    """
    cfg = get_cfg()
    cutoff = time.time() - cfg.history_hours * 3600
    try:
        hashes = load_md_hashes()
        candidates: list[tuple[float, Path]] = []

        for root_str in cfg.scan_dirs:
            root = Path(root_str).expanduser()
            if not root.exists():
                continue
            for md in _walk_md_files(root, max_depth=4):
                try:
                    mtime = md.stat().st_mtime
                    if mtime <= cutoff:
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

        entries = []
        succeeded: set = set()
        for mtime, md, current_hash in candidates[:10]:
            try:
                with md.open(errors="replace") as _f:
                    content = _f.read(200).strip()
                if content:
                    entries.append(f"**{md.name}**:\n  {content}")
                    succeeded.add(md)
            except Exception:
                logger.debug("markdown notes: failed to read %s", md)
                continue

        # 只更新成功读出内容的文件 hash，读取失败或超出前 10 名的文件保留旧 hash，
        # 确保下次运行仍可重新采集
        if entries:
            for mtime, md, current_hash in candidates[:10]:
                if md in succeeded:
                    hashes[str(md)] = current_hash
            save_md_hashes(hashes)

        if not entries:
            return ""
        return "## 本地 Markdown 笔记\n" + "\n\n".join(entries)
    except Exception as e:
        logger.debug("markdown notes: %s", e)
        return ""


def collect_ai_queries(n: int = 50) -> str:
    """从 Claude Code、Codex、Cursor 本地历史中提取用户最近的提问。"""
    cfg = get_cfg()
    cutoff = time.time() - cfg.history_hours * 3600
    try:
        queries: list[tuple[float, str]] = []

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
                    if text:
                        queries.append((ts, text))
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
                    if jf.stat().st_mtime <= cutoff:
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
                        if ts and ts <= cutoff:
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
                    if text:
                        queries.append((ts, text))
                    if len(queries) >= n:
                        break
            except Exception as e:
                logger.debug("codex history.jsonl: %s", e)

        # ── Cursor: state.vscdb（bubbleId:* 条目）────────────────────────────
        # Cursor IDE 气泡无可靠时间戳；用 DB 文件 mtime 作为代理。
        # 若 mtime <= cutoff，说明 DB 自 cutoff 时间窗口内未更新，跳过。
        cursor_db = (Path.home() / "Library" / "Application Support" /
                     "Cursor" / "User" / "globalStorage" / "state.vscdb")
        if cursor_db.exists():
            db_mtime = cursor_db.stat().st_mtime
            if db_mtime > cutoff:
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
                except Exception as e:
                    logger.debug("cursor state.vscdb: %s", e)
                finally:
                    tmp.unlink(missing_ok=True)

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
