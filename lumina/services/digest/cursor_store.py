"""
lumina/digest/cursor_store.py — Markdown file hash persistence.

Manages md_hashes.json: md5 digests of markdown files, used by
collect_markdown_notes to detect real content changes vs mtime-only updates
(e.g. Cursor editor touching files on startup, iCloud re-sync).

Usage:
    hashes = load_md_hashes()
    hashes[str(path)] = "abc123"
    save_md_hashes(hashes)
"""
import hashlib
import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger("lumina.services.digest")

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
