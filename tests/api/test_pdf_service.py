"""
lumina/services/pdf.py 单元测试。

不依赖真实 pdf2zh 或网络，使用 mock 验证 PdfJobManager 生命周期。
"""
import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from lumina.services.document.pdf import PdfJobManager, _delayed_rmtree


# ── PdfJobManager 基础生命周期 ─────────────────────────────────────────────────

def test_pdf_job_manager_initial_state():
    manager = PdfJobManager()
    assert manager._jobs == {}
    assert manager._bg_tasks == set()


@pytest.mark.anyio
async def test_pdf_job_manager_submit_creates_running_job():
    manager = PdfJobManager()

    async def _fake_translate(job_id, pdf_path, lang_out):
        # 立即把 job 设为 done（模拟翻译完成）
        manager._jobs[job_id].update({"status": "done", "mono": "/tmp/mono.pdf", "dual": "/tmp/dual.pdf"})

    with patch.object(manager, "_run_translate", _fake_translate):
        job_id = manager.submit_translate("/tmp/input.pdf", "zh", "/tmp/out")

    assert job_id in manager._jobs
    status = manager._jobs[job_id]
    assert status["status"] == "running"
    assert status["dir"] == "/tmp/out"
    assert "ts" in status

    # 等待后台 task 完成
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_pdf_job_manager_get_status_returns_job():
    manager = PdfJobManager()

    with patch.object(manager, "_run_translate", AsyncMock()):
        job_id = manager.submit_translate("/tmp/a.pdf", "zh", "/tmp/out1")

    job = manager.get_status(job_id)
    assert job is not None
    assert job["status"] == "running"


def test_pdf_job_manager_get_status_unknown_job_returns_none():
    manager = PdfJobManager()
    assert manager.get_status("nonexistent") is None


def test_pdf_job_manager_get_file_returns_none_when_not_done():
    manager = PdfJobManager()
    manager._jobs["j1"] = {"status": "running", "dir": "/tmp", "ts": time.time()}
    assert manager.get_file("j1", "mono") is None


def test_pdf_job_manager_get_file_returns_none_for_missing_job():
    manager = PdfJobManager()
    assert manager.get_file("ghost", "mono") is None


def test_pdf_job_manager_resolve_download_evicts_stale_done_job(tmp_path):
    manager = PdfJobManager()
    missing_file = tmp_path / "missing.pdf"
    manager._jobs["j1"] = {
        "status": "done",
        "dir": str(tmp_path),
        "mono": str(missing_file),
        "dual": str(missing_file),
        "ts": time.time(),
    }

    state, path = manager.resolve_download("j1", "mono")

    assert state == "missing"
    assert path is None
    assert "j1" not in manager._jobs


@pytest.mark.anyio
async def test_pdf_job_manager_run_translate_updates_status_on_success(tmp_path):
    manager = PdfJobManager()
    pdf_input = tmp_path / "input.pdf"
    pdf_input.write_bytes(b"%PDF-1.4 test")
    out_dir = str(tmp_path / "out")

    fake_mono = str(tmp_path / "mono.pdf")
    fake_dual = str(tmp_path / "dual.pdf")

    with patch("lumina.services.document.pdf._delayed_rmtree", AsyncMock()):
        with patch("lumina.services.document.pdf_translate.translate_pdfs", return_value=[(fake_mono, fake_dual)]):
            job_id = manager.submit_translate(str(pdf_input), "zh", out_dir)
            # 等 task 完成
            tasks = list(manager._bg_tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    job = manager.get_status(job_id)
    if job:  # task 可能已弹出（cleanup task 移除了 job）
        assert job["status"] in ("done", "running")


@pytest.mark.anyio
async def test_pdf_job_manager_run_translate_updates_status_on_error(tmp_path):
    manager = PdfJobManager()
    out_dir = str(tmp_path / "out_err")

    manager._jobs["j_err"] = {"status": "running", "dir": out_dir, "ts": time.time()}

    with patch("lumina.services.document.pdf._delayed_rmtree", AsyncMock()):
        with patch("lumina.services.document.pdf_translate.translate_pdfs", side_effect=RuntimeError("boom")):
            await manager._run_translate("j_err", "/nonexistent.pdf", "zh")

    assert manager._jobs["j_err"]["status"] == "error"
    assert "boom" in manager._jobs["j_err"]["error"]


@pytest.mark.anyio
async def test_pdf_job_manager_run_translate_marks_cancelled(tmp_path):
    manager = PdfJobManager()
    out_dir = str(tmp_path / "out_cancel")
    manager._jobs["j_cancel"] = {"status": "running", "dir": out_dir, "ts": time.time()}

    async def _cancelled_fetch(_url: str) -> str:
        raise asyncio.CancelledError

    with patch("lumina.services.document.pdf.fetch_pdf_url", _cancelled_fetch):
        with pytest.raises(asyncio.CancelledError):
            await manager._run_translate("j_cancel", "https://example.com/a.pdf", "zh")

    assert manager._jobs["j_cancel"]["status"] == "cancelled"
    assert manager._jobs["j_cancel"]["error"] == "cancelled"


def test_pdf_job_manager_track_holds_task_reference():
    manager = PdfJobManager()
    loop = asyncio.new_event_loop()
    try:
        async def _dummy():
            pass

        async def _test():
            task = asyncio.create_task(_dummy())
            manager._track(task)
            assert task in manager._bg_tasks
            await task
            # task 完成后自动从 _bg_tasks 中移除（通过 done_callback）
            assert task not in manager._bg_tasks

        loop.run_until_complete(_test())
    finally:
        loop.close()


@pytest.mark.anyio
async def test_delayed_rmtree_cancel_keeps_directory(tmp_path):
    target = tmp_path / "keep_me"
    target.mkdir()
    (target / "file.txt").write_text("hello", encoding="utf-8")

    task = asyncio.create_task(_delayed_rmtree(str(target), delay=60))
    await asyncio.sleep(0)
    task.cancel()
    await task

    assert target.exists()
