"""
lumina/digest/collectors/system.py — 系统级数据源采集

包含：终端历史、Git 提交、剪贴板、以及辅助的 git 目录遍历函数。
"""
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from lumina.services.digest.config import get_cfg
from lumina.platform_support.paths import shell_history_candidates

logger = logging.getLogger("lumina.services.digest")

_GIT_SKIP_DIRS = {".git", ".venv", "node_modules", "build", "dist", "__pycache__", ".app"}


def _normalize_history_command(cmd: str) -> str:
    """将多行 shell 历史压成单行，避免续行污染摘要展示。"""
    return " ".join(part.strip() for part in cmd.splitlines() if part.strip()).strip()


def _parse_shell_history_lines(raw: list[str]) -> list[tuple[Optional[float], str]]:
    """解析 zsh/bash 普通历史文件，按"一条命令"而不是"一行文本"产出。"""
    entries: list[tuple[Optional[float], str]] = []
    pending_ts: Optional[float] = None
    current_ts: Optional[float] = None
    current_cmd: Optional[str] = None

    def _flush() -> None:
        nonlocal current_ts, current_cmd
        if current_cmd is None:
            return
        normalized = _normalize_history_command(current_cmd)
        if normalized:
            entries.append((current_ts, normalized))
        current_ts = None
        current_cmd = None

    for raw_line in raw:
        line = raw_line.rstrip("\n")
        if line.startswith(": ") and ";" in line:
            _flush()
            try:
                meta, cmd = line.split(";", 1)
                ts_str = meta.split(":")[1].strip()
                current_ts = float(ts_str)
            except (ValueError, IndexError):
                current_ts = None
                cmd = line
            current_cmd = cmd
            pending_ts = None
            continue

        if line.startswith("#") and line[1:].isdigit():
            _flush()
            try:
                pending_ts = float(line[1:])
            except ValueError:
                pending_ts = None
            continue

        stripped = line.strip()
        if not stripped:
            continue

        if current_cmd is not None:
            current_cmd += "\n" + stripped
            continue

        current_ts = pending_ts
        current_cmd = stripped
        pending_ts = None

    _flush()
    return entries


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


def collect_shell_history(n: int = 100) -> str:
    cfg = get_cfg()
    cutoff = time.time() - cfg.history_hours * 3600
    try:
        sources = [p for p in shell_history_candidates() if p.exists()]
        if not sources:
            return ""

        entries: list[tuple[Optional[float], str]] = []
        has_timestamps = False

        for src in sources:
            raw = src.read_text(errors="replace").splitlines()
            if src.name == "fish_history":
                pending_cmd: Optional[str] = None
                for line in raw:
                    if line.startswith("- cmd:"):
                        pending_cmd = line.split(":", 1)[1].strip()
                    elif pending_cmd and "when:" in line:
                        try:
                            ts_val = float(line.split("when:", 1)[1].strip())
                            entries.append((ts_val, pending_cmd))
                            has_timestamps = True
                        except ValueError:
                            entries.append((None, pending_cmd))
                        pending_cmd = None
                if pending_cmd:
                    entries.append((None, pending_cmd))
                continue

            parsed_entries = _parse_shell_history_lines(raw)
            if any(ts is not None for ts, _ in parsed_entries):
                has_timestamps = True
            entries.extend(parsed_entries)

        if not entries:
            return ""

        if has_timestamps:
            entries.sort(key=lambda item: item[0] or 0, reverse=True)
            filtered = [cmd for ts, cmd in entries if ts is not None and ts > cutoff]
        else:
            filtered = [cmd for _, cmd in reversed(entries)]

        cmds: list[str] = []
        seen: set[str] = set()
        for cmd in filtered:
            if cmd in seen:
                continue
            seen.add(cmd)
            cmds.append(cmd)
            if len(cmds) >= n:
                break

        if not cmds:
            return ""
        return "## 终端历史（最近命令）\n" + "\n".join(f"  {c}" for c in cmds)
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
        from lumina.platform_support.platform_utils import clipboard_get
        content = clipboard_get().strip()
        if not content:
            return ""
        if len(content) > 500:
            content = content[:500] + "…（已截断）"
        return f"## 剪贴板内容\n{content}"
    except Exception as e:
        logger.debug("clipboard: %s", e)
        return ""
