"""
Digest 模块单元测试：config、cursor_store、collectors 的核心逻辑。
不依赖真实 LLM，不触发真实采集（对系统文件只做 mock）。
"""
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


# ── DigestConfig ─────────────────────────────────────────────────────────────

def test_digest_config_defaults():
    from lumina.digest.config import DigestConfig
    cfg = DigestConfig()
    assert cfg.history_hours == 24.0
    assert cfg.refresh_hours == 1.0
    assert cfg.notify_time == "20:00"
    assert cfg.enabled_collectors is None


def test_digest_config_configure():
    from lumina.digest.config import configure, get_cfg
    configure({"digest": {
        "history_hours": 12,
        "refresh_hours": 0.5,
        "notify_time": "09:00",
        "enabled_collectors": ["collect_shell_history", "collect_git_logs"],
    }})
    cfg = get_cfg()
    assert cfg.history_hours == 12.0
    assert cfg.refresh_hours == 0.5
    assert cfg.notify_time == "09:00"
    assert cfg.enabled_collectors == ["collect_shell_history", "collect_git_logs"]


def test_digest_config_enabled_collectors_null():
    from lumina.digest.config import configure, get_cfg
    configure({"digest": {"enabled_collectors": None}})
    assert get_cfg().enabled_collectors is None


def test_digest_config_scan_dirs_empty_uses_defaults():
    from lumina.digest.config import configure, get_cfg, DigestConfig
    configure({"digest": {"scan_dirs": []}})
    assert get_cfg().scan_dirs == DigestConfig().scan_dirs


# ── cursor_store ──────────────────────────────────────────────────────────────

def test_cursor_store_roundtrip(tmp_path):
    from lumina.digest import cursor_store
    original_path = cursor_store.CURSOR_PATH
    cursor_store.CURSOR_PATH = tmp_path / "cursors.json"
    try:
        data = {"collect_shell_history": 1700000000.0, "collect_git_logs": 1700001000.0}
        cursor_store.save_cursors(data)
        loaded = cursor_store.load_cursors()
        assert loaded == data
    finally:
        cursor_store.CURSOR_PATH = original_path


def test_cursor_store_missing_file_returns_empty(tmp_path):
    from lumina.digest import cursor_store
    original_path = cursor_store.CURSOR_PATH
    cursor_store.CURSOR_PATH = tmp_path / "nonexistent.json"
    try:
        assert cursor_store.load_cursors() == {}
    finally:
        cursor_store.CURSOR_PATH = original_path


def test_cursor_store_corrupted_file_returns_empty(tmp_path):
    from lumina.digest import cursor_store
    p = tmp_path / "cursors.json"
    p.write_text("not json")
    original_path = cursor_store.CURSOR_PATH
    cursor_store.CURSOR_PATH = p
    try:
        assert cursor_store.load_cursors() == {}
    finally:
        cursor_store.CURSOR_PATH = original_path


def test_cursor_store_atomic_write(tmp_path):
    """原子写入：tmp 文件写完后 rename，不应留下 .tmp 文件。"""
    from lumina.digest import cursor_store
    original_path = cursor_store.CURSOR_PATH
    cursor_store.CURSOR_PATH = tmp_path / "cursors.json"
    try:
        cursor_store.save_cursors({"x": 1.0})
        assert cursor_store.CURSOR_PATH.exists()
        assert not cursor_store.CURSOR_PATH.with_suffix(".tmp").exists()
    finally:
        cursor_store.CURSOR_PATH = original_path


# ── md5_of_file ───────────────────────────────────────────────────────────────

def test_md5_of_file(tmp_path):
    from lumina.digest.cursor_store import md5_of_file
    f = tmp_path / "note.md"
    f.write_text("hello world")
    h1 = md5_of_file(f)
    assert len(h1) == 32
    # 内容不变，hash 不变
    assert md5_of_file(f) == h1
    # 内容改变，hash 改变
    f.write_text("hello world!")
    assert md5_of_file(f) != h1


def test_md5_of_file_missing_returns_stable_value(tmp_path):
    """文件不存在时，md5_of_file 返回固定值（空内容的 md5），不抛异常。"""
    from lumina.digest.cursor_store import md5_of_file
    result = md5_of_file(tmp_path / "ghost.md")
    # 返回值稳定（不随调用变化），且与有内容的文件不同
    assert result == md5_of_file(tmp_path / "ghost.md")
    real = tmp_path / "real.md"
    real.write_text("content")
    assert result != md5_of_file(real)


# ── collectors._get_cursor / _set_cursor ─────────────────────────────────────

def test_get_cursor_uses_fallback():
    import lumina.digest.collectors as c
    now = time.time()
    c._CURSORS = {"_fallback": now - 3600}
    ts = c._get_cursor("collect_shell_history")
    assert abs(ts - (now - 3600)) < 1


def test_get_cursor_uses_own_cursor():
    import lumina.digest.collectors as c
    ts_own = time.time() - 100
    c._CURSORS = {"collect_shell_history": ts_own, "_fallback": time.time() - 3600}
    assert c._get_cursor("collect_shell_history") == ts_own


def test_set_cursor_updates_dict():
    import lumina.digest.collectors as c
    c._CURSORS = {}
    ts = time.time()
    c._set_cursor("collect_git_logs", ts)
    assert c._CURSORS["collect_git_logs"] == ts


def test_set_cursor_ignores_zero():
    import lumina.digest.collectors as c
    c._CURSORS = {}
    c._set_cursor("collect_git_logs", 0)
    assert "collect_git_logs" not in c._CURSORS


# ── enabled_collectors 过滤 ────────────────────────────────────────────────────

def test_enabled_collectors_filters_active():
    """_collect_all 应只运行 enabled_collectors 里的 collector。"""
    from lumina.digest.config import configure
    configure({"digest": {"enabled_collectors": ["collect_shell_history"]}})

    called = []

    def collect_shell_history():  # 函数名必须与 enabled_collectors 条目一致
        called.append("collect_shell_history")
        return ""

    def collect_git_logs():
        called.append("collect_git_logs")
        return ""

    import lumina.digest.core as core
    original = core._COLLECTORS[:]
    core._COLLECTORS = [collect_shell_history, collect_git_logs]

    try:
        import asyncio
        with patch("lumina.digest.cursor_store.load_cursors", return_value={}), \
             patch("lumina.digest.cursor_store.save_cursors"):
            asyncio.run(core._collect_all())
        assert called == ["collect_shell_history"]  # git_logs 被过滤掉
    finally:
        core._COLLECTORS = original
        configure({"digest": {}})


# ── config reset_config ────────────────────────────────────────────────────────

def test_reset_config(tmp_path):
    from lumina.config import get_config, reset_config
    import os

    # 写一个临时 config
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "provider": {"type": "local", "model_path": None, "openai": {}},
        "host": "127.0.0.1",
        "port": 19999,
        "log_level": "INFO",
        "digest": {},
        "system_prompts": {},
    }))

    reset_config()
    cfg = get_config(str(cfg_file))
    assert cfg.port == 19999

    reset_config()
    cfg2 = get_config(str(cfg_file))
    assert cfg2.port == 19999

    reset_config()  # 清理，不污染其他测试


def test_get_status_recovers_generated_at_from_existing_digest(tmp_path):
    import lumina.digest.core as core

    digest_path = tmp_path / "digest.md"
    digest_path.write_text("# existing digest\n", encoding="utf-8")
    mtime = time.time() - 123
    os.utime(digest_path, (mtime, mtime))

    with patch.object(core, "_DIGEST_PATH", digest_path), \
         patch.object(core, "_generated_at", None), \
         patch.object(core, "_last_generated_ts", None):
        status = core.get_status()

    assert status["generating"] is False
    assert status["generated_at"] == datetime.fromtimestamp(mtime).isoformat()


@pytest.mark.asyncio
async def test_maybe_generate_digest_clears_orphan_lock_from_previous_process(tmp_path):
    import lumina.digest.core as core

    digest_path = tmp_path / "digest.md"
    lock_path = tmp_path / "digest.lock"
    lock_path.write_text("", encoding="utf-8")

    now = time.time()
    lock_mtime = now - 5
    os.utime(lock_path, (lock_mtime, lock_mtime))

    class FakeLLM:
        generate = AsyncMock(return_value="digest body")

    with patch.object(core, "_DIGEST_PATH", digest_path), \
         patch.object(core, "_LOCK_PATH", lock_path), \
         patch.object(core, "_PROCESS_STARTED_TS", now), \
         patch.object(core, "_generated_at", None), \
         patch.object(core, "_last_generated_ts", None), \
         patch.object(core, "_collect_all", AsyncMock(return_value="mocked context")):
        await core.maybe_generate_digest(FakeLLM(), force_full=True)

    assert digest_path.exists()
    assert not lock_path.exists()
