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
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from lumina.digest.config import get_cfg

logger = logging.getLogger("lumina.digest")

# ── Per-Collector Cursor ──────────────────────────────────────────────────────
# 由 core.py 在 ThreadPoolExecutor 启动前注入，各 collector 只读自己的 key。
# "_fallback" key = now - effective_hours（全局兜底时间戳）。
_CURSORS: dict = {}


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
            _set_cursor(name, newest_ts)

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

        _set_cursor(name, newest_ts)

        if not entries:
            return ""
        return "## Git 提交（过去 %.0fh）\n" % cfg.history_hours + "\n\n".join(entries)
    except Exception as e:
        logger.debug("git logs: %s", e)
        return ""


def collect_clipboard() -> str:
    # 无状态，不使用 cursor
    try:
        result = subprocess.check_output(["pbpaste"], timeout=3, text=True)
        content = result.strip()
        if not content:
            return ""
        if len(content) > 500:
            content = content[:500] + "…（已截断）"
        return f"## 剪贴板内容\n{content}"
    except Exception as e:
        logger.debug("clipboard: %s", e)
        return ""


def collect_browser_history(n: int = 50) -> str:
    name = "collect_browser_history"
    cursor = _get_cursor(name)   # Unix 秒
    cfg = get_cfg()
    try:
        results = []
        newest_ts: Optional[float] = None

        # Chrome
        chrome_db = (Path.home() / "Library" / "Application Support" /
                     "Google" / "Chrome" / "Default" / "History")
        if chrome_db.exists():
            tmp = Path("/tmp/lumina_chrome_history.db")
            shutil.copy2(str(chrome_db), str(tmp))
            try:
                conn = sqlite3.connect(str(tmp))
                chrome_offset = 11644473600 * 1_000_000
                cutoff_chrome = int(cursor * 1_000_000 + chrome_offset)
                rows = conn.execute(
                    "SELECT title, url, last_visit_time FROM urls "
                    "WHERE last_visit_time > ? "
                    "ORDER BY last_visit_time DESC LIMIT ?",
                    (cutoff_chrome, n)
                ).fetchall()
                conn.close()
                for title, url, lv_time in rows:
                    results.append(title or url)
                    ts_unix = (lv_time - chrome_offset) / 1_000_000
                    if newest_ts is None or ts_unix > newest_ts:
                        newest_ts = ts_unix
            except Exception as e:
                logger.debug("chrome history: %s", e)
            finally:
                tmp.unlink(missing_ok=True)

        # Firefox
        ff_profiles = Path.home() / "Library" / "Application Support" / "Firefox" / "Profiles"
        if ff_profiles.exists():
            for profile_dir in ff_profiles.iterdir():
                places_db = profile_dir / "places.sqlite"
                if not places_db.exists():
                    continue
                tmp = Path("/tmp/lumina_ff_places.db")
                shutil.copy2(str(places_db), str(tmp))
                try:
                    conn = sqlite3.connect(str(tmp))
                    cutoff_ff = int(cursor * 1_000_000)
                    rows = conn.execute(
                        "SELECT title, url, last_visit_date FROM moz_places "
                        "WHERE last_visit_date > ? "
                        "ORDER BY last_visit_date DESC LIMIT ?",
                        (cutoff_ff, n)
                    ).fetchall()
                    conn.close()
                    for title, url, lv_date in rows:
                        results.append(title or url)
                        if lv_date:
                            ts_unix = lv_date / 1_000_000
                            if newest_ts is None or ts_unix > newest_ts:
                                newest_ts = ts_unix
                except Exception as e:
                    logger.debug("firefox history: %s", e)
                finally:
                    tmp.unlink(missing_ok=True)
                break  # 只处理第一个 profile

        _set_cursor(name, newest_ts)

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
    """读取 Notes NoteStore.sqlite。

    macOS TCC 限制：打包后的 .app 需要「完整磁盘访问」才能读取备忘录数据库。
    若权限不足，返回特殊标记 '__PERMISSION_DENIED__'，由 core.py 转为提示信息。

    cursor 存 Unix 秒；查询时转换为 CoreData epoch（cursor - 978307200）。
    """
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

        import shutil as _shutil
        tmp_db = Path("/tmp/lumina_notes.db")
        _shutil.copy2(str(db_path), str(tmp_db))
        # 必须同时复制 WAL / SHM，否则 Notes.app 写入的未 checkpoint 数据会丢失
        for suffix in ("-wal", "-shm"):
            src = db_path.with_name(db_path.name + suffix)
            if src.exists():
                try:
                    _shutil.copy2(str(src), str(tmp_db.with_name(tmp_db.name + suffix)))
                except Exception:
                    pass
        conn = _sqlite3.connect(str(tmp_db))
        cur = conn.cursor()
        cur.execute(
            "SELECT ZTITLE1, ZSNIPPET, ZMODIFICATIONDATE1 FROM ZICCLOUDSYNCINGOBJECT "
            "WHERE ZMODIFICATIONDATE1 > ? AND ZTITLE1 IS NOT NULL "
            "ORDER BY ZMODIFICATIONDATE1 DESC LIMIT 20",
            (cursor_core,),
        )
        rows = cur.fetchall()
        conn.close()
        tmp_db.unlink(missing_ok=True)

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

        _set_cursor(name, newest_ts)

        return f"## 备忘录（过去 {cfg.history_hours:.0f}h 修改）\n" + "\n\n".join(entries)
    except PermissionError:
        logger.warning("notes app: 权限不足，请在「系统设置 → 隐私与安全 → 完整磁盘访问」中授权 Lumina")
        return "__PERMISSION_DENIED__"
    except Exception as e:
        logger.debug("notes app sqlite: %s", e)
        return ""


_MD_SKIP_PARTS = {".app", "build", "dist", "node_modules", ".git", ".venv", "__pycache__"}


def collect_markdown_notes() -> str:
    """扫描 scan_dirs 下最近修改的 .md 文件。cursor 直接与 st_mtime 比较（均为 Unix 秒）。"""
    name = "collect_markdown_notes"
    cursor = _get_cursor(name)
    cfg = get_cfg()
    try:
        # 先收集所有候选文件，按 mtime 降序排，避免遍历顺序不定导致漏掉新文件
        candidates: list[tuple[float, Path]] = []

        for root_str in cfg.scan_dirs[:2]:  # 只扫前两个目录（Documents/Desktop）
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
                    candidates.append((mtime, md))
                except Exception:
                    continue

        if not candidates:
            return ""

        candidates.sort(key=lambda x: -x[0])
        newest_ts = candidates[0][0]

        entries = []
        for mtime, md in candidates[:10]:
            try:
                content = md.read_text(errors="replace")[:200].strip()
                if content:
                    entries.append(f"**{md.name}**:\n  {content}")
            except Exception:
                continue

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
                tmp = Path("/tmp/lumina_cursor_state.db")
                try:
                    shutil.copy2(str(cursor_db), str(tmp))
                    conn = sqlite3.connect(str(tmp))
                    rows = conn.execute(
                        "SELECT value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
                        " AND length(value) < 4000"
                    ).fetchall()
                    conn.close()
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

        _set_cursor(name, newest_ts)

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
