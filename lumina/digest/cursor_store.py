"""
lumina/digest/cursor_store.py — Per-collector cursor persistence.

Cursors are Unix timestamps (float seconds since epoch), keyed by collector
function name. Stored in ~/.lumina/collector_cursors.json.

Also manages md_hashes.json: md5 digests of markdown files, used by
collect_markdown_notes to detect real content changes vs mtime-only updates
(e.g. Cursor editor touching files on startup, iCloud re-sync).

Usage:
    cursors = load_cursors()          # {} on any error
    cursors["collect_git_logs"] = ts
    save_cursors(cursors)             # atomic write, errors are swallowed

    hashes = load_md_hashes()
    hashes[str(path)] = "abc123"
    save_md_hashes(hashes)
"""
import hashlib
import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger("lumina.digest")

CURSOR_PATH = Path.home() / ".lumina" / "collector_cursors.json"
MD_HASHES_PATH = Path.home() / ".lumina" / "md_hashes.json"


def md5_of_file(path: Path) -> str:
    """Return md5 hex of the first 4KB of a file (fast, sufficient for change detection)."""
    h = hashlib.md5()
    try:
        with path.open("rb") as f:
            h.update(f.read(4096))
    except Exception:
        pass
    return h.hexdigest()


def load_md_hashes() -> Dict[str, str]:
    """Load path→md5 map. Returns {} on any error."""
    try:
        data = json.loads(MD_HASHES_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug("md_hashes load error: %s", e)
    return {}


def save_md_hashes(hashes: Dict[str, str]) -> None:
    """Persist path→md5 map atomically."""
    try:
        MD_HASHES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = MD_HASHES_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(hashes, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(MD_HASHES_PATH)
    except Exception as e:
        logger.warning("md_hashes save error: %s", e)


def load_cursors() -> Dict[str, float]:
    """Load per-collector cursors from disk.

    Returns an empty dict on any read/parse error — all collectors will
    fall back to cfg.history_hours as their initial window.
    """
    try:
        data = json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.debug("cursor store: unexpected type %s, resetting", type(data))
            return {}
        return {
            k: float(v)
            for k, v in data.items()
            if isinstance(k, str) and isinstance(v, (int, float)) and float(v) > 0
        }
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.debug("cursor store load error: %s", e)
        return {}


def save_cursors(cursors: Dict[str, float]) -> None:
    """Persist cursors atomically. Errors are logged and swallowed."""
    try:
        CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CURSOR_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cursors, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(CURSOR_PATH)
    except Exception as e:
        logger.warning("cursor store save error: %s", e)
