"""
lumina/digest/collectors.py — 各数据源采集函数

每个函数独立、失败静默返回空字符串。
新增数据源：在此文件追加函数，并在 core.py 的 _COLLECTORS 列表中注册。

──────────────────────────────────────────────────────────────────
当前已支持的数据来源
──────────────────────────────────────────────────────────────────
【终端历史】collect_shell_history
  └─ ~/.zsh_history 或 ~/.bash_history
     支持 zsh 扩展格式（`: ts:0;cmd`），自动去重，取最近 n 条

【Git 提交】collect_git_logs
  └─ 扫描 scan_dirs 下深度 ≤3 的所有 .git 目录
     仅收集 history_hours 内的 `git log --oneline` 记录
     默认 scan_dirs：~/Documents, ~/Desktop, ~/Projects, ~/code, ~/dev
     可在 config.json["digest"]["scan_dirs"] 中覆盖

【剪贴板】collect_clipboard
  └─ macOS pbpaste，截断至 500 字符

【浏览器历史】collect_browser_history
  ├─ Google Chrome  ~/Library/Application Support/Google/Chrome/Default/History
  └─ Firefox        ~/Library/Application Support/Firefox/Profiles/*/places.sqlite
     先 cp 到 /tmp 再用 sqlite3 读取（规避文件锁），取 history_hours 内访问记录

【备忘录（Notes.app）】collect_notes_app
  └─ 通过 AppleScript 读取 Notes.app，取 history_hours 内修改的笔记前 200 字

【本地 Markdown 笔记】collect_markdown_notes
  └─ 扫描 scan_dirs 前两个目录（默认 ~/Documents, ~/Desktop），
     取 history_hours 内 mtime 变更的 *.md 文件前 200 字

【AI 对话提问】collect_ai_queries
  ├─ Claude Code  ~/.claude/history.jsonl（`display` 字段）
  │              ~/.claude/projects/**/*.jsonl（`type=user` 纯文本 content）
  ├─ OpenAI Codex CLI  ~/.codex/history.jsonl（`text` 字段，unix 时间戳）
  └─ Cursor       ~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
                  cursorDiskKV 表，key 前缀 `bubbleId:`
                  通过 `humanChanges` 键 + key 数量 ≤15 识别人类气泡
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

from lumina.digest.config import get_cfg

logger = logging.getLogger("lumina.digest")


def collect_shell_history(n: int = 100) -> str:
    try:
        zsh  = Path.home() / ".zsh_history"
        bash = Path.home() / ".bash_history"
        src  = zsh if zsh.exists() else (bash if bash.exists() else None)
        if not src:
            return ""
        raw = src.read_text(errors="replace").splitlines()
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
        if not cmds:
            return ""
        return "## 终端历史（最近命令）\n" + "\n".join(f"  {c}" for c in reversed(cmds))
    except Exception as e:
        logger.debug("shell history: %s", e)
        return ""


def collect_git_logs(n: int = 20) -> str:
    cfg = get_cfg()
    try:
        since = (datetime.now() - timedelta(hours=cfg.history_hours)).strftime("%Y-%m-%d %H:%M")
        entries, seen_repos = [], set()
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
                    result = subprocess.run(
                        ["git", "log", "--oneline", f"--since={since}", f"-{n}"],
                        cwd=str(repo_dir), capture_output=True, text=True, timeout=5
                    )
                    lines = result.stdout.strip().splitlines()
                    if lines:
                        entries.append(f"**{repo_dir.name}**:\n" +
                                       "\n".join(f"  {l}" for l in lines))
                except Exception:
                    continue
        if not entries:
            return ""
        return "## Git 提交（过去 %.0fh）\n" % cfg.history_hours + "\n\n".join(entries)
    except Exception as e:
        logger.debug("git logs: %s", e)
        return ""


def collect_clipboard() -> str:
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
    cfg = get_cfg()
    try:
        cutoff_ts = time.time() - cfg.history_hours * 3600
        results = []

        # Chrome
        chrome_db = (Path.home() / "Library" / "Application Support" /
                     "Google" / "Chrome" / "Default" / "History")
        if chrome_db.exists():
            tmp = Path("/tmp/lumina_chrome_history.db")
            shutil.copy2(str(chrome_db), str(tmp))
            try:
                conn = sqlite3.connect(str(tmp))
                chrome_offset = 11644473600 * 1_000_000
                cutoff = int(cutoff_ts * 1_000_000 + chrome_offset)
                rows = conn.execute(
                    "SELECT title, url FROM urls WHERE last_visit_time > ? "
                    "ORDER BY last_visit_time DESC LIMIT ?",
                    (cutoff, n)
                ).fetchall()
                conn.close()
                for title, url in rows:
                    results.append(title or url)
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
                    cutoff_ff = int(cutoff_ts * 1_000_000)
                    rows = conn.execute(
                        "SELECT title, url FROM moz_places WHERE last_visit_date > ? "
                        "ORDER BY last_visit_date DESC LIMIT ?",
                        (cutoff_ff, n)
                    ).fetchall()
                    conn.close()
                    for title, url in rows:
                        results.append(title or url)
                except Exception as e:
                    logger.debug("firefox history: %s", e)
                finally:
                    tmp.unlink(missing_ok=True)
                break

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
    """通过 AppleScript 读取 Notes.app 内容（最近 history_hours 修改的笔记）。"""
    cfg = get_cfg()
    try:
        script = f'''
tell application "Notes"
    set cutoff to (current date) - ({int(cfg.history_hours * 3600)} * seconds)
    set result to ""
    repeat with n in every note
        if modification date of n > cutoff then
            set noteTitle to name of n
            set noteBody to text 1 thru (min(200, (count characters of (body of n)))) of (body of n)
            set result to result & "**" & noteTitle & "**:\\n  " & noteBody & "\\n\\n"
        end if
    end repeat
    return result
end tell
'''
        out = subprocess.check_output(
            ["osascript", "-e", script],
            timeout=10, text=True, stderr=subprocess.DEVNULL
        ).strip()
        if not out:
            return ""
        return f"## 备忘录（过去 {cfg.history_hours:.0f}h 修改）\n{out}"
    except Exception as e:
        logger.debug("notes app: %s", e)
        return ""


def collect_markdown_notes() -> str:
    """扫描 scan_dirs 下最近 history_hours 内修改的 .md 文件。"""
    cfg = get_cfg()
    try:
        cutoff = time.time() - cfg.history_hours * 3600
        entries = []
        for root_str in cfg.scan_dirs[:2]:  # 只扫前两个目录（Documents/Desktop）
            root = Path(root_str)
            if not root.exists():
                continue
            for md in root.rglob("*.md"):
                try:
                    if md.stat().st_mtime < cutoff:
                        continue
                    content = md.read_text(errors="replace")[:200].strip()
                    if content:
                        entries.append(f"**{md.name}**:\n  {content}")
                except Exception:
                    continue
                if len(entries) >= 10:
                    break
        if not entries:
            return ""
        return "## 本地 Markdown 笔记\n" + "\n\n".join(entries)
    except Exception as e:
        logger.debug("markdown notes: %s", e)
        return ""


def collect_ai_queries(n: int = 50) -> str:
    """从 Claude Code、Codex、Cursor 本地历史中提取用户最近的提问。"""
    cfg = get_cfg()
    try:
        cutoff = time.time() - cfg.history_hours * 3600
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
                    if ts_ms and ts_ms / 1000 < cutoff:
                        break
                    text = obj.get("display", "").strip()
                    if text:
                        ts = ts_ms / 1000 if ts_ms else 0.0
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
                    if jf.stat().st_mtime < cutoff:
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
                        if ts and ts < cutoff:
                            continue
                        content = obj.get("message", {}).get("content", "")
                        if isinstance(content, list):
                            # Only extract plain text parts; skip tool_result / tool_use
                            content = " ".join(
                                c.get("text", "") for c in content
                                if isinstance(c, dict) and c.get("type") == "text"
                            )
                        content = content.strip()
                        # Skip system-injected summaries and tool outputs
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
                    ts = obj.get("ts", 0)
                    if ts and ts < cutoff:
                        break
                    text = obj.get("text", "").strip()
                    if text:
                        queries.append((float(ts), text))
                    if len(queries) >= n:
                        break
            except Exception as e:
                logger.debug("codex history.jsonl: %s", e)

        # ── Cursor: state.vscdb（bubbleId:* 条目中 type=1 的 text 字段）────────
        cursor_db = (Path.home() / "Library" / "Application Support" /
                     "Cursor" / "User" / "globalStorage" / "state.vscdb")
        if cursor_db.exists():
            tmp = Path("/tmp/lumina_cursor_state.db")
            try:
                shutil.copy2(str(cursor_db), str(tmp))
                conn = sqlite3.connect(str(tmp))
                # bubbleId entries with short values are likely human messages
                rows = conn.execute(
                    "SELECT value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
                    " AND length(value) < 4000"
                ).fetchall()
                conn.close()
                for (value,) in rows:
                    try:
                        val = bytes(value).decode("utf-8", errors="replace") if isinstance(value, (bytes, bytearray)) else str(value)
                        obj = json.loads(val)
                        if not isinstance(obj, dict):
                            continue
                        # Human bubbles: have 'humanChanges' key AND ≤10 total keys
                        # AI response bubbles: also have 'humanChanges' but have 60+ keys
                        if "humanChanges" not in obj or len(obj) > 15:
                            continue
                        text = obj.get("text", "").strip()
                        if not text:
                            continue
                        # No reliable timestamp in bubble — use 0 (recent enough if DB mtime ok)
                        if len(text) < 2000:
                            queries.append((cursor_db.stat().st_mtime, text))
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
